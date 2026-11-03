"""Replay past days through the simulator, from the charge that was actually made.

This does two jobs. Comparing the simulated import, export and state of charge with what the
meter recorded shows whether the simulator (and the battery settings it uses) can be trusted.
And simulating every other target on the same day shows, with hindsight, what the best charge
would have been and what the actual decision saved or cost.
"""

from datetime import date, datetime, time, timedelta, timezone
from statistics import mean
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.importers.base import INTERVAL
from app.models import PlanningMode, System, UsageRecord
from app.planner.base import InProgress, Replay, ReplayDay, ReplayDetail, ReplayPoint, Review
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
from app.tariffs import TariffTimeline

DEFAULT_DAYS = 30
MAX_DAYS = 120
# Days of earlier history the review compares the latest day with.
TYPICAL_DAYS = 28
# A charge within this of the hindsight best counts as about right.
ABOUT_RIGHT_PENCE = 1.0
FULL_SOC_PERCENT = 99
# A day's chart opens this evening before the window, if the window opens later.
LEAD_UP_FROM = time(21, 0)
# Battery readings are whole percent and the house uses a little, so this much still counts as held.
HELD_POINTS = 1.5
# Enough of the house's use in the window coming from the grid to call it grid only.
GRID_ONLY_SHARE = 0.9


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc)


def _point(
    record: UsageRecord,
    start: datetime,
    cheap: bool = False,
    best: float | None = None,
    lead_up: bool = False,
) -> ReplayPoint:
    return ReplayPoint(
        start=start,
        load_kwh=record.load_kwh,
        solar_kwh=record.solar_kwh,
        import_kwh=record.grid_import_kwh,
        export_kwh=record.grid_export_kwh,
        soc_percent=record.battery_soc_percent,
        cheap=cheap,
        best_soc_percent=best,
        lead_up=lead_up,
    )


def _percent_error(simulated: float, actual: float) -> float | None:
    return abs(simulated - actual) / actual * 100 if actual > 0.5 else None


class Replayer:
    def __init__(self, db: Session, system: System) -> None:
        self.db = db
        self.system = system
        self.battery = BatterySpec.of(system.battery)
        # Past days cost what the tariff in force then charged.
        self.book = TariffTimeline.from_system(system)
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
            charge_start=cycle.night_start if night else None,
        )

    def _replayed(
        self, records: dict[datetime, UsageRecord], mode: PlanningMode, limit: int
    ) -> list[ReplayDay]:
        """The latest `limit` days that can be replayed, oldest first."""
        replayed: list[ReplayDay] = []
        if records:
            latest_day = max(records).astimezone(self.zone).date()
            first_day = min(records).astimezone(self.zone).date()
            day = latest_day
            while day >= first_day and len(replayed) < limit:
                cycle = cycle_for(day, self.book, self.system.timezone)
                result = self._replay_day(day, cycle, records, mode)
                if result is not None:
                    replayed.append(result)
                day -= timedelta(days=1)
        replayed.reverse()
        return replayed

    def detail(self, day: date, mode: PlanningMode) -> ReplayDetail | None:
        """One day's metered half hours and review; None if it can't be replayed."""
        records = self._records(MAX_DAYS)
        replayed = self._replayed(records, mode, MAX_DAYS + TYPICAL_DAYS)
        found = next((d for d in replayed if d.day == day), None)
        if found is None:
            return None
        cycle = cycle_for(day, self.book, self.system.timezone)
        earlier = [d for d in replayed if d.day < day][-TYPICAL_DAYS:]
        night, daytime = cycle.night_starts(), cycle.day_starts()

        def series(starts, field):
            return np.array([getattr(records[s], field) for s in starts], dtype=float)

        b = self.battery
        best = simulate(
            b,
            float(b.kwh(found.soc_before_percent)),
            np.array([float(b.kwh(found.best_soc_percent))]),
            series(night, "load_kwh"),
            series(night, "solar_kwh"),
            series(daytime, "load_kwh"),
            series(daytime, "solar_kwh"),
        )
        best_soc = {
            s: round(float(b.percent(best.stored_kwh[0, t + 1])), 1) for t, s in enumerate(daytime)
        }
        in_night = set(night)
        points = [
            *self._lead_up(cycle, records),
            *(
                _point(records[s], s, cheap=s in in_night, best=best_soc.get(s))
                for s in night + daytime
            ),
        ]
        return ReplayDetail(found, self.comments(found, earlier, cycle, records, mode), points)

    def _lead_up(self, cycle: Cycle, records: dict[datetime, UsageRecord]) -> list[ReplayPoint]:
        """The evening before the window opens, to show how the battery got there."""
        opens = cycle.night_start.astimezone(self.zone)
        evening = opens.date() if opens.hour >= 12 else opens.date() - timedelta(days=1)
        moment = datetime.combine(evening, LEAD_UP_FROM, self.zone).astimezone(timezone.utc)
        points = []
        while moment < cycle.night_start:
            if moment in records:
                points.append(_point(records[moment], moment, lead_up=True))
            moment += INTERVAL
        return points

    def _in_progress(self, records: dict[datetime, UsageRecord]) -> InProgress | None:
        """The newest cycle that has started but whose data doesn't reach its end yet."""
        if not records:
            return None
        latest = max(records)
        latest_day = latest.astimezone(self.zone).date()
        for day in (latest_day + timedelta(days=1), latest_day):
            cycle = cycle_for(day, self.book, self.system.timezone)
            night = cycle.night_starts()
            if night and cycle.night_start <= latest and latest + INTERVAL < cycle.day_end:
                return self._summarise_in_progress(day, cycle, records, latest)
        return None

    def _summarise_in_progress(
        self, day: date, cycle: Cycle, records: dict[datetime, UsageRecord], latest: datetime
    ) -> InProgress | None:
        before = records.get(cycle.night_start - INTERVAL)
        window = [records[s] for s in cycle.night_starts() if s in records]
        levels = [r.battery_soc_percent for r in window if r.battery_soc_percent is not None]
        if before is None or before.battery_soc_percent is None or not levels:
            return None
        start, after, peak = before.battery_soc_percent, levels[-1], max(levels)
        if peak - start > HELD_POINTS:
            outcome = "charged"
        elif start - after > HELD_POINTS:
            outcome = "discharged"
        else:
            outcome = "held"
        house = sum(max(r.load_kwh - r.solar_kwh, 0.0) for r in window)
        grid = sum(r.grid_import_kwh for r in window)
        in_night = set(cycle.night_starts())
        return InProgress(
            day=day,
            charge_start=cycle.night_start,
            charge_end=cycle.night_end,
            scored_after=cycle.day_end,
            window_finished=latest + INTERVAL >= cycle.night_end,
            soc_before_percent=round(start, 1),
            soc_peak_percent=round(peak, 1),
            soc_after_percent=round(after, 1),
            outcome=outcome,
            grid_only=outcome != "discharged" and grid >= GRID_ONLY_SHARE * house,
            latest_reading_at=latest + INTERVAL,
            points=[
                *self._lead_up(cycle, records),
                *(
                    _point(records[s], s, cheap=s in in_night)
                    for s in cycle.night_starts() + cycle.day_starts()
                    if s in records
                ),
            ],
        )

    def replay(self, mode: PlanningMode, days: int = DEFAULT_DAYS) -> Replay:
        records = self._records(days)
        replayed = self._replayed(records, mode, days + TYPICAL_DAYS)
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
            in_progress=self._in_progress(records),
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
