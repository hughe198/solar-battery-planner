"""Predict the battery level when the charge window opens, from the latest imported reading.

The inverter is taken to be in self-use from the reading until the window opens. A reading from
before the previous charge window closed isn't used, because whatever that charge did to the
battery isn't known. Nor is one from after this window opened, because a charge may be under way.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.forecasting.base import IntervalForecast
from app.importers.base import INTERVAL
from app.models import System, UsageRecord
from app.planner.base import TimelinePoint
from app.planner.simulator import BatterySpec, Cycle, cycle_for, simulate
from app.tariffs import TariffSchedule


@dataclass(frozen=True)
class Reading:
    """A battery level at `at`, the end of the half hour it was metered in (aware UTC)."""

    at: datetime
    soc_percent: float

    def half_hours_until(self, end: datetime) -> list[datetime]:
        return [self.at + INTERVAL * i for i in range((end - self.at) // INTERVAL)]


def usable_reading(db: Session, system: System, day: date, cycle: Cycle) -> Reading | None:
    """The latest imported battery level, if it can predict the level when `cycle` opens."""
    latest = db.execute(
        select(UsageRecord.start, UsageRecord.battery_soc_percent)
        .where(UsageRecord.system_id == system.id, UsageRecord.battery_soc_percent.is_not(None))
        .order_by(UsageRecord.start.desc())
        .limit(1)
    ).first()
    if latest is None:
        return None
    at = latest.start.replace(tzinfo=timezone.utc) + INTERVAL
    book = TariffSchedule.from_system(system)
    previous = cycle_for(day - timedelta(days=1), book, system.timezone)
    if not previous.night_end <= at <= cycle.night_start:
        return None
    return Reading(at, latest.battery_soc_percent)


def simulate_lead_in(
    system: System, reading: Reading, intervals: list[IntervalForecast]
) -> list[TimelinePoint]:
    """Self-use from the reading through `intervals`, the half hours up to the window opening."""
    battery = BatterySpec.of(system.battery)
    level = battery.kwh(reading.soc_percent)
    empty = np.zeros(0)
    sim = simulate(
        battery,
        level,
        np.array([level]),
        empty,
        empty,
        np.array([i.load_kwh for i in intervals], dtype=float),
        np.array([i.solar_kwh for i in intervals], dtype=float),
    )
    return [
        TimelinePoint(
            start=i.start,
            load_kwh=i.load_kwh,
            solar_kwh=i.solar_kwh,
            import_kwh=round(float(sim.day_import_kwh[0, t]), 3),
            export_kwh=round(float(sim.day_export_kwh[0, t]), 3),
            soc_percent=round(float(battery.percent(sim.stored_kwh[0, t + 1])), 1),
            cheap=False,
        )
        for t, i in enumerate(intervals)
    ]
