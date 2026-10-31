"""Pick an overnight charge target by simulating every target on three versions of tomorrow.

Each target is played out half-hourly on the expected forecast, a dull day (usage at the top of
its likely range, solar at the bottom) and a bright day (the reverse). Its score is the
weighted average effective cost:

- *Maximise own usage*: what you pay for imports, net of the energy left in the battery.
  Exports earn nothing in this score, so charging from the grid only to push your own solar
  out to the grid never looks worthwhile.
- *Maximise export revenue*: the same, minus export income: the full money picture.

Over-charging is caught by the simulation itself: grid energy bought overnight that leaves the
battery full by mid-morning forces later solar out to the grid, so it cost the cheap rate plus
losses and earned only the export rate (or nothing, for own usage). The planner then takes the
lowest target whose average is within a couple of pence of the best, so it leans under when the
extra charge only pays off if the day turns out dull.
"""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from app.forecasting.base import DayForecast, IntervalForecast
from app.importers.base import INTERVAL
from app.models import PlanningMode, System
from app.planner.base import (
    Candidate,
    ChargePlan,
    Outcome,
    Recommendation,
    TimelinePoint,
)
from app.planner.simulator import (
    BatterySpec,
    Costs,
    CyclePrices,
    Simulation,
    costs,
    cycle_for,
    simulate,
)
from app.tariffs import TariffSchedule

SCENARIO_WEIGHTS = {"expected": 0.5, "dull": 0.25, "bright": 0.25}
# Don't pay for charge that saves less than this on average.
LEAN_UNDER_PENCE = 2.0
# Alternatives described in the explanation, in SoC percentage points either side.
COMPARE_STEP_PERCENT = 10
FULL_TOLERANCE_KWH = 0.01

MODES = (PlanningMode.maximise_own_usage, PlanningMode.maximise_export_revenue)


def effective_cost(c: Costs, mode: PlanningMode) -> np.ndarray:
    base = c.import_cost - c.stored_value
    if mode is PlanningMode.maximise_export_revenue:
        return base - c.export_revenue
    return base


def best_index(scores: np.ndarray, tolerance: float = LEAN_UNDER_PENCE) -> int:
    """Lowest candidate (they are in rising order) within `tolerance` of the best score."""
    return int(np.flatnonzero(scores <= scores.min() + tolerance)[0])


@dataclass(frozen=True)
class Scenario:
    night_load: np.ndarray
    night_solar: np.ndarray
    day_load: np.ndarray
    day_solar: np.ndarray

    def run(self, battery: BatterySpec, start_kwh: float, targets: np.ndarray) -> Simulation:
        return simulate(
            battery,
            start_kwh,
            targets,
            self.night_load,
            self.night_solar,
            self.day_load,
            self.day_solar,
        )


def _ratio(value: float, expected: float) -> float:
    return value / expected if expected > 0 else 1.0


def scenarios(
    forecast: DayForecast, night: list[IntervalForecast], day: list[IntervalForecast]
) -> dict[str, Scenario]:
    """Expected, dull and bright versions of the cycle, scaled to the day's likely totals."""

    def build(load_scale: float, solar_scale: float) -> Scenario:
        return Scenario(
            night_load=np.array([i.load_kwh for i in night]) * load_scale,
            night_solar=np.array([i.solar_kwh for i in night]) * solar_scale,
            day_load=np.array([i.load_kwh for i in day]) * load_scale,
            day_solar=np.array([i.solar_kwh for i in day]) * solar_scale,
        )

    load, solar = forecast.load, forecast.solar
    return {
        "expected": build(1.0, 1.0),
        "dull": build(_ratio(load.high, load.expected), _ratio(solar.low, solar.expected)),
        "bright": build(_ratio(load.low, load.expected), _ratio(solar.high, solar.expected)),
    }


def candidate_targets(battery: BatterySpec, start_kwh: float, max_kwh: float) -> np.ndarray:
    """Whole-percent targets from the starting level up to the most the window can reach."""
    low = battery.percent(start_kwh)
    high = battery.percent(max_kwh)
    steps = np.arange(np.ceil(low), np.floor(high) + 1)
    percents = np.unique(np.concatenate([[low], steps[(steps > low) & (steps < high)], [high]]))
    return battery.kwh(percents)


def _first_time(mask: np.ndarray, starts: list[datetime]) -> datetime | None:
    hits = np.flatnonzero(mask)
    return starts[hits[0]] + INTERVAL if len(hits) else None


def outcome(
    sim: Simulation,
    c: Costs,
    row: int,
    scenario: Scenario,
    battery: BatterySpec,
    day_starts: list[datetime],
) -> Outcome:
    after = sim.stored_kwh[row, 1:]
    return Outcome(
        load_kwh=round(float(scenario.night_load.sum() + scenario.day_load.sum()), 2),
        solar_kwh=round(float(scenario.night_solar.sum() + scenario.day_solar.sum()), 2),
        import_kwh=round(float(sim.night_import_kwh[row] + sim.day_import_kwh[row].sum()), 2),
        export_kwh=round(float(sim.night_export_kwh[row] + sim.day_export_kwh[row].sum()), 2),
        import_cost_pence=round(float(c.import_cost[row]), 1),
        export_revenue_pence=round(float(c.export_revenue[row]), 1),
        net_cost_pence=round(float(c.net[row]), 1),
        end_soc_percent=round(float(battery.percent(sim.end_kwh[row])), 1),
        full_at=_first_time(after >= battery.capacity_kwh - FULL_TOLERANCE_KWH, day_starts),
        reserve_at=_first_time(after <= battery.reserve_kwh + FULL_TOLERANCE_KWH, day_starts),
    )


def pence(value: float) -> str:
    value = round(value)
    if abs(value) >= 100:
        return f"£{value / 100:.2f}"
    return f"{value:.0f}p"


def rate(pence_per_kwh: float) -> str:
    return f"{pence_per_kwh:.2f}".rstrip("0").rstrip(".")


def net_phrase(net_pence: float) -> str:
    return f"costs {pence(net_pence)} net" if net_pence >= 0 else f"earns {pence(-net_pence)} net"


class Explainer:
    def __init__(self, system: System, battery: BatterySpec, prices: CyclePrices) -> None:
        self.zone = ZoneInfo(system.timezone)
        self.battery = battery
        self.prices = prices

    def clock(self, moment: datetime) -> str:
        return moment.astimezone(self.zone).strftime("%H:%M")

    def reasons(
        self,
        mode: PlanningMode,
        chosen: int,
        targets: np.ndarray,
        scores: np.ndarray,
        start_kwh: float,
        results: dict[str, Outcome],
        expected: Scenario,
        day_starts: list[datetime],
    ) -> list[str]:
        b, prices = self.battery, self.prices
        charge, day, export = (
            rate(prices.charge),
            rate(prices.day_import_mean),
            rate(prices.day_export_mean),
        )
        percent = b.percent(targets[chosen])
        added = targets[chosen] - start_kwh
        lines = []
        if added > 0.05:
            grid = b.grid_kwh_to_store(added)
            lines.append(
                f"Charge to {percent:.0f}%: {grid:.1f} kWh from the grid at {charge}p, "
                f"costing {pence(grid * prices.charge)}."
            )
        else:
            lines.append(
                f"No overnight charge: the battery's {b.percent(start_kwh):.0f}% is enough."
            )

        e = results["expected"]
        sunny = np.flatnonzero(expected.day_solar >= expected.day_load)
        takeover = day_starts[sunny[0]] if len(sunny) else None
        if e.reserve_at is not None and (takeover is None or e.reserve_at < takeover):
            lines.append(
                f"On the expected day the battery reaches its {b.percent(b.reserve_kwh):.0f}% "
                f"reserve at {self.clock(e.reserve_at)}, before solar covers the house, so "
                f"some daytime import at {day}p is cheaper than charging more."
                if added > 0.05
                else f"The house imports at {day}p from {self.clock(e.reserve_at)} "
                f"until solar takes over."
            )
        elif takeover is not None:
            lines.append(
                f"That covers the house until solar takes over at about {self.clock(takeover)}."
            )
        if e.full_at is not None:
            lines.append(
                f"Solar fills the battery by {self.clock(e.full_at)} and "
                f"{e.export_kwh:.1f} kWh is exported at {export}p."
            )

        def compare(other: int, label: str) -> None:
            delta = scores[other] - scores[chosen]
            if abs(delta) < 0.5:
                lines.append(f"{label} would make almost no difference on average.")
            elif delta > 0:
                lines.append(f"{label} would cost {pence(delta)} more on average.")
            else:
                lines.append(f"{label} would save only {pence(-delta)} on average.")

        step_up = np.flatnonzero(b.percent(targets) >= percent + COMPARE_STEP_PERCENT)
        if len(step_up):
            more = int(step_up[0])
            compare(more, f"Charging to {b.percent(targets[more]):.0f}%")
        step_down = np.flatnonzero(b.percent(targets) <= percent - COMPARE_STEP_PERCENT)
        if len(step_down):
            compare(
                int(step_down[-1]), f"Charging only to {b.percent(targets[step_down[-1]]):.0f}%"
            )

        if chosen > int(np.argmin(scores)):
            best = int(np.argmin(scores))
            lines.append(
                f"Charging to {b.percent(targets[best]):.0f}% scores marginally better, but by "
                f"under {pence(LEAN_UNDER_PENCE)}, so the plan leans under: extra charge mostly "
                f"pays off only if the day turns out dull."
            )

        d, br = results["dull"], results["bright"]
        if (d.solar_kwh, d.load_kwh) != (br.solar_kwh, br.load_kwh):
            lines.append(
                f"If the day is dull ({d.solar_kwh:.1f} kWh solar, {d.load_kwh:.1f} kWh use) "
                f"it {net_phrase(d.net_cost_pence)}; if bright ({br.solar_kwh:.1f} kWh solar, "
                f"{br.load_kwh:.1f} kWh use) it {net_phrase(br.net_cost_pence)}."
            )
        stored_cost = prices.charge / b.charge_efficiency
        if mode is PlanningMode.maximise_export_revenue and prices.day_export_mean > stored_cost:
            lines.append(
                f"Export pays {export}p while charging costs {stored_cost:.1f}p per "
                f"stored kWh, so filling up from the grid and exporting solar earns more."
            )
        elif (
            mode is PlanningMode.maximise_own_usage
            and added > 0.05
            and prices.day_export_mean > stored_cost
        ):
            lines.append(
                "Maximise export revenue would charge more and sell more solar; own usage only "
                "buys what the house needs."
            )
        return lines


def plan(system: System, forecast: DayForecast, start_soc_percent: float) -> ChargePlan:
    battery = BatterySpec.of(system.battery)
    schedule = TariffSchedule.from_system(system)
    cycle = cycle_for(forecast.day, schedule, system.timezone)
    by_start = {i.start: i for i in forecast.intervals}
    night = [by_start[s] for s in cycle.night_starts() if s in by_start]
    day = [by_start[s] for s in cycle.day_starts() if s in by_start]
    if not day:
        raise ValueError("The forecast doesn't cover the day after the cheap window")
    day_starts = [i.start for i in day]
    prices = CyclePrices.of(schedule, [i.start for i in night], day_starts)

    start_kwh = float(np.clip(battery.kwh(start_soc_percent), 0, battery.capacity_kwh))
    max_kwh = battery.max_target_kwh(start_kwh, cycle.window)
    targets = candidate_targets(battery, start_kwh, max_kwh)

    runs = {}
    for name, scenario in scenarios(forecast, night, day).items():
        sim = scenario.run(battery, start_kwh, targets)
        runs[name] = (scenario, sim, costs(sim, battery, prices, start_kwh))
    scores = {
        mode: sum(
            SCENARIO_WEIGHTS[name] * effective_cost(c, mode) for name, (_, _, c) in runs.items()
        )
        for mode in MODES
    }

    explainer = Explainer(system, battery, prices)
    recommendations = []
    for mode in sorted(MODES, key=lambda m: m is not system.planning_mode):
        chosen = best_index(scores[mode])
        results = {
            name: outcome(sim, c, chosen, scenario, battery, day_starts)
            for name, (scenario, sim, c) in runs.items()
        }
        added = targets[chosen] - start_kwh
        grid = float(battery.grid_kwh_to_store(max(added, 0.0)))
        recommendations.append(
            Recommendation(
                mode=mode,
                target_soc_percent=round(float(battery.percent(targets[chosen])), 1),
                stored_kwh=round(float(max(added, 0.0)), 2),
                charge_kwh=round(grid, 2),
                charge_cost_pence=round(grid * prices.charge, 1),
                expected=results["expected"],
                dull=results["dull"],
                bright=results["bright"],
                reasons=explainer.reasons(
                    mode,
                    chosen,
                    targets,
                    scores[mode],
                    start_kwh,
                    results,
                    runs["expected"][0],
                    day_starts,
                ),
            )
        )

    chosen = best_index(scores[system.planning_mode])
    scenario, sim, _ = runs["expected"]
    # The charge is shown spread evenly across the window, as most inverters schedule it.
    window = cycle.night_starts()
    added = targets[chosen] - start_kwh
    per_slot = float(sim.charge_grid_kwh[chosen]) / max(len(window), 1)

    def night_point(i: IntervalForecast) -> TimelinePoint:
        done = (window.index(i.start) + 1) / len(window)
        net = i.load_kwh - i.solar_kwh + per_slot
        return TimelinePoint(
            start=i.start,
            load_kwh=i.load_kwh,
            solar_kwh=i.solar_kwh,
            import_kwh=round(max(net, 0.0), 3),
            export_kwh=round(max(-net, 0.0), 3),
            soc_percent=round(float(battery.percent(start_kwh + added * done)), 1),
            cheap=True,
        )

    timeline = [night_point(i) for i in night] + [
        TimelinePoint(
            start=s,
            load_kwh=round(float(scenario.day_load[t]), 3),
            solar_kwh=round(float(scenario.day_solar[t]), 3),
            import_kwh=round(float(sim.day_import_kwh[chosen, t]), 3),
            export_kwh=round(float(sim.day_export_kwh[chosen, t]), 3),
            soc_percent=round(float(battery.percent(sim.stored_kwh[chosen, t + 1])), 1),
            cheap=False,
        )
        for t, s in enumerate(day_starts)
    ]
    return ChargePlan(
        day=forecast.day,
        mode=system.planning_mode,
        start_soc_percent=round(float(battery.percent(start_kwh)), 1),
        max_soc_percent=round(float(battery.percent(max_kwh)), 1),
        charge_start=cycle.night_start,
        charge_end=cycle.night_end,
        recommendations=recommendations,
        candidates=[
            Candidate(
                target_soc_percent=round(float(battery.percent(t)), 1),
                own_usage_pence=round(float(scores[PlanningMode.maximise_own_usage][k]), 1),
                export_revenue_pence=round(
                    float(scores[PlanningMode.maximise_export_revenue][k]), 1
                ),
            )
            for k, t in enumerate(targets)
        ],
        timeline=timeline,
        load=forecast.load,
        solar=forecast.solar,
        load_method=forecast.load_method,
        solar_method=forecast.solar_method,
        history_days=forecast.history_days,
    )
