"""Replay past days through the simulator, from the charge that was actually made.

This does two jobs. Comparing the simulated import, export and state of charge with what the
meter recorded shows whether the simulator (and the battery settings it uses) can be trusted.
And simulating every other target on the same day shows, with hindsight, what the best charge
would have been and what the actual decision saved or cost.
"""

from datetime import date, datetime, timedelta, timezone
from statistics import mean
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.importers.base import INTERVAL
from app.models import PlanningMode, System, UsageRecord
from app.planner.base import Replay, ReplayDay, Review
from app.planner.charging import learn_rate_from_history, reachable_kwh
from app.planner.optimiser import (
    best_index,
    candidate_targets,
    effective_cost,
    pence,
    rate,
)
from app.planner.simulator import (
    BatterySpec,
    Cycle,
    CyclePrices,
    costs,
    cycle_for,
    simulate,
)
from app.tariffs import TariffSchedule

DEFAULT_DAYS = 30
MAX_DAYS = 120
# Days of earlier history the review compares the latest day with.
TYPICAL_DAYS = 28
# A charge within this of the hindsight best counts as about right.
ABOUT_RIGHT_PENCE = 1.0
FULL_SOC_PERCENT = 99


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc)


def _percent_error(simulated: float, actual: float) -> float | None:
    return abs(simulated - actual) / actual * 100 if actual > 0.5 else None


class Replayer:
    def __init__(self, db: Session, system: System) -> None:
        self.db = db
        self.system = system
        self.battery = BatterySpec.of(system.battery)
        self.book = TariffSchedule.from_system(system)
        self.rate = learn_rate_from_history(db, system)
        self.zone = ZoneInfo(system.timezone)

    def _records(self, days: int) -> dict[datetime, UsageRecord]:
        latest = self.db.scalar(
            select(func.max(UsageRecord.start)).where(UsageRecord.system_id == self.system.id)
        )
        if latest is None:
            return {}
        since = latest - timedelta(days=days + TYPICAL_DAYS + 3)
        rows = self.db.scalars(
            select(UsageRecord).where(
                UsageRecord.system_id == self.system.id, UsageRecord.start >= since
            )
        )
        return {_utc(r.start): r for r in rows}

    def _replay_day(
        self, day: date, cycle: Cycle, records: dict[datetime, UsageRecord], mode: PlanningMode
    ) -> ReplayDay | None:
        night, daytime = cycle.night_starts(), cycle.day_starts()
        if not daytime or any(s not in records for s in night + daytime):
            return None
        before = records.get(cycle.night_start - INTERVAL)
        # With no charge window (a single-rate tariff) the day starts where the night left it.
        after = records[night[-1]] if night else before
        if before is None or before.battery_soc_percent is None:
            return None
        if after is None or after.battery_soc_percent is None:
            return None

        b = self.battery
        start_kwh = float(b.kwh(before.battery_soc_percent))
        actual_kwh = float(b.kwh(after.battery_soc_percent))
        reachable = reachable_kwh(self.rate, b, start_kwh, cycle.window)
        targets = np.unique(
            np.concatenate(
                [
                    candidate_targets(b, start_kwh, reachable),
                    [actual_kwh],
                ]
            )
        )
        actual = int(np.flatnonzero(targets == actual_kwh)[0])

        def series(starts, field):
            return np.array([getattr(records[s], field) for s in starts], dtype=float)

        sim = simulate(
            b,
            start_kwh,
            targets,
            series(night, "load_kwh"),
            series(night, "solar_kwh"),
            series(daytime, "load_kwh"),
            series(daytime, "solar_kwh"),
        )
        prices = CyclePrices.of(self.book, night, daytime)
        c = costs(sim, b, prices, start_kwh)
        scores = effective_cost(c, mode)
        best = best_index(scores, tolerance=0.0)

        cycle_starts = night + daytime
        actual_import = float(series(cycle_starts, "grid_import_kwh").sum())
        actual_export = float(series(cycle_starts, "grid_export_kwh").sum())
        actual_cost = (
            series(night, "grid_import_kwh") @ prices.night_import
            + series(daytime, "grid_import_kwh") @ prices.day_import
            - series(night, "grid_export_kwh") @ prices.night_export
            - series(daytime, "grid_export_kwh") @ prices.day_export
        )
        metered_soc = series(daytime, "battery_soc_percent")
        known = ~np.isnan(metered_soc)
        simulated_soc = b.percent(sim.stored_kwh[actual, 1:])
        exports = sim.day_export_kwh.sum(axis=1) + sim.night_export_kwh
        day_imports = sim.day_import_kwh.sum(axis=1)

        return ReplayDay(
            day=day,
            load_kwh=round(float(series(cycle_starts, "load_kwh").sum()), 2),
            solar_kwh=round(float(series(cycle_starts, "solar_kwh").sum()), 2),
            soc_before_percent=round(before.battery_soc_percent, 1),
            soc_after_charge_percent=round(after.battery_soc_percent, 1),
            actual_import_kwh=round(actual_import, 2),
            actual_export_kwh=round(actual_export, 2),
            actual_net_cost_pence=round(float(actual_cost), 1),
            simulated_import_kwh=round(
                float(sim.night_import_kwh[actual] + day_imports[actual]), 2
            ),
            simulated_export_kwh=round(float(exports[actual]), 2),
            soc_error_percent=round(
                float(np.abs(simulated_soc[known] - metered_soc[known]).mean()), 1
            )
            if known.any()
            else None,
            best_soc_percent=round(float(b.percent(targets[best])), 1),
            missed_saving_pence=round(float(max(scores[actual] - scores[best], 0.0)), 1),
            wasted_export_kwh=round(float(max(exports[actual] - exports[best], 0.0)), 2),
            shortfall_import_kwh=round(float(max(day_imports[actual] - day_imports[best], 0.0)), 2),
        )

    def replay(self, mode: PlanningMode, days: int = DEFAULT_DAYS) -> Replay:
        records = self._records(days)
        replayed: list[ReplayDay] = []
        if records:
            latest_day = max(records).astimezone(self.zone).date()
            first_day = min(records).astimezone(self.zone).date()
            day = latest_day
            while day >= first_day and len(replayed) < days + TYPICAL_DAYS:
                cycle = cycle_for(day, self.book, self.system.timezone)
                result = self._replay_day(day, cycle, records, mode)
                if result is not None:
                    replayed.append(result)
                day -= timedelta(days=1)
        replayed.reverse()
        shown = replayed[-days:]

        def total_error(sim_field: str, actual_field: str) -> float | None:
            if not shown:
                return None
            return _percent_error(
                sum(getattr(d, sim_field) for d in shown),
                sum(getattr(d, actual_field) for d in shown),
            )

        soc_errors = [d.soc_error_percent for d in shown if d.soc_error_percent is not None]
        review = None
        if shown:
            latest = shown[-1]
            earlier = [d for d in replayed if d.day < latest.day][-TYPICAL_DAYS:]
            cycle = cycle_for(latest.day, self.book, self.system.timezone)
            review = Review(latest, self.comments(latest, earlier, cycle, records, mode))
        return Replay(
            mode=mode,
            days=shown,
            import_error_percent=_round(total_error("simulated_import_kwh", "actual_import_kwh")),
            export_error_percent=_round(total_error("simulated_export_kwh", "actual_export_kwh")),
            soc_error_percent=round(mean(soc_errors), 1) if soc_errors else None,
            missed_saving_pence=round(sum(d.missed_saving_pence for d in shown), 1),
            review=review,
        )

    def comments(
        self,
        d: ReplayDay,
        earlier: list[ReplayDay],
        cycle: Cycle,
        records: dict[datetime, UsageRecord],
        mode: PlanningMode,
    ) -> list[str]:
        """What happened on the latest replayed day and what the charge decision was worth."""
        prices = CyclePrices.of(self.book, cycle.night_starts(), cycle.day_starts())
        label = f"{d.day:%A} {d.day.day} {d.day:%B}"  # %-d isn't supported on Windows
        usage = (
            f"On {label} your home used {d.load_kwh:.1f} kWh "
            f"and the panels made {d.solar_kwh:.1f} kWh"
        )
        if earlier:
            usage += (
                f" (the {len(earlier)} days before averaged "
                f"{mean(e.load_kwh for e in earlier):.1f} kWh used and "
                f"{mean(e.solar_kwh for e in earlier):.1f} kWh solar)"
            )
        lines = [usage + "."]

        added = d.soc_after_charge_percent - d.soc_before_percent
        night_slots = cycle.night_starts()
        window = (
            f"{cycle.night_start.astimezone(self.zone):%H:%M}–"
            f"{cycle.night_end.astimezone(self.zone):%H:%M}"
        )
        if not night_slots:
            lines.append(
                "Your tariff has no cheaper overnight rate, so there was no grid charge to judge; "
                f"the battery started the day at {d.soc_after_charge_percent:.0f}%."
            )
        elif added > 0.5:
            lines.append(
                f"The battery went into the cheap window ({window}) at "
                f"{d.soc_before_percent:.0f}% and was charged to {d.soc_after_charge_percent:.0f}%."
            )
        else:
            lines.append(
                f"The battery wasn't charged in the cheap window ({window}): it was at "
                f"{d.soc_after_charge_percent:.0f}% when the day started."
            )

        best = f"{d.best_soc_percent:.0f}%"
        if d.missed_saving_pence < ABOUT_RIGHT_PENCE:
            lines.append(
                "That was about right: with hindsight no other charge level would have saved "
                f"more than {pence(ABOUT_RIGHT_PENCE)}."
            )
        elif d.best_soc_percent < d.soc_after_charge_percent:
            full = next(
                (
                    s + INTERVAL
                    for s in cycle.day_starts()
                    if (records[s].battery_soc_percent or 0) >= FULL_SOC_PERCENT
                ),
                None,
            )
            when = (
                f" the battery was full by {full.astimezone(self.zone):%H:%M} and" if full else ""
            )
            lines.append(
                f"That was more than the day needed:{when} about {d.wasted_export_kwh:.1f} kWh "
                f"more solar went to the grid at {rate(prices.day_export_mean)}p than if you'd "
                f"charged to {best}. "
                f"Charging to {best} would have saved {pence(d.missed_saving_pence)}."
            )
        else:
            lines.append(
                f"That was less than the day needed: about {d.shortfall_import_kwh:.1f} kWh more "
                f"was imported at the {rate(prices.day_import_mean)}p day rate than if you'd "
                f"charged to {best}. "
                f"Charging to {best} would have saved {pence(d.missed_saving_pence)}."
            )
        if mode is PlanningMode.maximise_own_usage:
            lines.append(
                "Savings are judged on what you pay for imports, as in maximise own usage mode."
            )
        net = d.actual_net_cost_pence
        lines.append(
            f"Metered, the cycle cost {pence(net)} net of exports"
            if net >= 0
            else f"Metered, the cycle earned {pence(-net)} net of imports"
        )
        lines[-1] += (
            f"; the simulator, replaying the same day, put imports at "
            f"{d.simulated_import_kwh:.1f} kWh (metered {d.actual_import_kwh:.1f}) and exports at "
            f"{d.simulated_export_kwh:.1f} kWh (metered {d.actual_export_kwh:.1f})."
        )
        return lines


def _round(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None
