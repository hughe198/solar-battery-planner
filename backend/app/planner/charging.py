"""How long a forced charge takes, and when to start and stop it.

An inverter in forced charge keeps drawing from the grid until the schedule ends, whatever the
battery level, so the target percentage alone isn't enough: the user needs a start and stop
time too. The charge is timed to finish as the charge window closes, so the whole cheap period
is used and the battery starts the day at the target. Until the charge starts the battery is
held, so the house runs on the cheap (or free) grid rather than spending stored energy that
would only be bought back, with losses, before morning.

The charge rate is learned from history where it can be: half hours in which the grid charged
the battery, split at the level where batteries usually slow down near full.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Protocol

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.importers.base import INTERVAL
from app.models import System, UsageRecord
from app.planner.simulator import HOURS_PER_INTERVAL, BatterySpec

# Above this level most batteries charge more slowly.
TAPER_FROM_PERCENT = 90
# Half hours that end this full may have finished part way through, so their rate is low.
FULL_PERCENT = 98
LEARN_DAYS = 60
MIN_BULK_SAMPLES = 4
MIN_TAPER_SAMPLES = 3
# Grid import beyond the house's own net use that counts as grid charging, kWh per half hour.
GRID_CHARGE_MARGIN_KWH = 0.1
# Use a high percentile: a half hour where charging started or stopped part way is slower.
RATE_PERCENTILE = 75
SCHEDULE_STEP = timedelta(minutes=5)


@dataclass(frozen=True)
class ChargeRate:
    """Power into the cells during a forced charge, in kW, below and above the taper level."""

    bulk_kw: float
    taper_kw: float
    # Half hours of grid charging it was learned from; 0 means taken from the battery settings.
    samples: int

    @classmethod
    def rated(cls, battery: BatterySpec) -> "ChargeRate":
        kw = battery.max_charge_kw * battery.charge_efficiency
        return cls(kw, kw, 0)


def learn_rate(battery: BatterySpec, records: list[UsageRecord]) -> ChargeRate:
    """Charge rates seen when the grid was charging the battery, oldest records first."""
    rated = ChargeRate.rated(battery)
    bulk, taper = [], []
    for before, now in zip(records, records[1:]):
        if before.battery_soc_percent is None or now.battery_soc_percent is None:
            continue
        if now.start - before.start != INTERVAL:
            continue
        rise = now.battery_soc_percent - before.battery_soc_percent
        house = now.load_kwh - now.solar_kwh
        if rise <= 0 or now.grid_import_kwh < house + GRID_CHARGE_MARGIN_KWH:
            continue
        if now.battery_soc_percent >= FULL_PERCENT:
            continue
        kw = rise / 100 * battery.capacity_kwh / HOURS_PER_INTERVAL
        (taper if before.battery_soc_percent >= TAPER_FROM_PERCENT else bulk).append(kw)
    if len(bulk) < MIN_BULK_SAMPLES:
        return rated
    bulk_kw = float(np.percentile(bulk, RATE_PERCENTILE))
    taper_kw = (
        float(np.percentile(taper, RATE_PERCENTILE)) if len(taper) >= MIN_TAPER_SAMPLES else bulk_kw
    )
    return ChargeRate(bulk_kw, min(taper_kw, bulk_kw), len(bulk) + len(taper))


def learn_rate_from_history(db: Session, system: System) -> ChargeRate:
    battery = BatterySpec.of(system.battery)
    latest = db.scalar(
        select(func.max(UsageRecord.start)).where(UsageRecord.system_id == system.id)
    )
    if latest is None:
        return ChargeRate.rated(battery)
    records = db.scalars(
        select(UsageRecord)
        .where(
            UsageRecord.system_id == system.id,
            UsageRecord.start >= latest - timedelta(days=LEARN_DAYS),
        )
        .order_by(UsageRecord.start)
    ).all()
    return learn_rate(battery, list(records))


def charge_hours(rate: ChargeRate, battery: BatterySpec, from_kwh: float, to_kwh: float) -> float:
    """Hours to raise the stored energy from one level to another."""
    if to_kwh <= from_kwh:
        return 0.0
    knee = battery.capacity_kwh * TAPER_FROM_PERCENT / 100
    bulk = max(0.0, min(to_kwh, knee) - from_kwh)
    taper = max(0.0, to_kwh - max(from_kwh, knee))
    return bulk / rate.bulk_kw + taper / rate.taper_kw


def reachable_kwh(
    rate: ChargeRate, battery: BatterySpec, start_kwh: float, window: timedelta
) -> float:
    """Most the battery can hold by the end of a window, charging throughout it."""
    hours = window.total_seconds() / 3600
    knee = battery.capacity_kwh * TAPER_FROM_PERCENT / 100
    to_knee = charge_hours(rate, battery, start_kwh, knee)
    if hours <= to_knee:
        return min(battery.capacity_kwh, start_kwh + hours * rate.bulk_kw)
    level = max(start_kwh, knee)
    return min(battery.capacity_kwh, level + (hours - to_knee) * rate.taper_kw)


@dataclass(frozen=True)
class ChargeSchedule:
    """What the inverter does through the charge window, in UTC.

    From the window opening until the forced charge starts the battery is held: the house runs
    on the grid at the cheap (or free) rate instead of from the battery, so stored energy isn't
    spent overnight only to be bought back, with losses, before morning. With no charge needed
    the whole window is a hold. `start == stop` when there is no forced charge.
    """

    hold_start: datetime
    hold_stop: datetime
    start: datetime
    stop: datetime
    reaches_target: bool

    @property
    def minutes(self) -> int:
        return int((self.stop - self.start).total_seconds() // 60)

    @property
    def hold_minutes(self) -> int:
        return int((self.hold_stop - self.hold_start).total_seconds() // 60)


def _floor(moment: datetime, step: timedelta) -> datetime:
    epoch = datetime(2000, 1, 1, tzinfo=timezone.utc)
    return epoch + ((moment - epoch) // step) * step


def schedule(
    rate: ChargeRate,
    battery: BatterySpec,
    start_kwh: float,
    target_kwh: float,
    window_start: datetime,
    window_end: datetime,
) -> ChargeSchedule:
    """Hold the battery, then force charge late enough to finish as the window closes, so the
    whole cheap period is used. Start times are rounded to five minutes earlier."""
    if window_end <= window_start:
        return ChargeSchedule(window_end, window_end, window_end, window_end, True)
    if target_kwh <= start_kwh + 1e-6:
        return ChargeSchedule(window_start, window_end, window_end, window_end, True)
    hours = charge_hours(rate, battery, start_kwh, target_kwh)
    begin = max(window_start, _floor(window_end - timedelta(hours=hours), SCHEDULE_STEP))
    finish = begin + timedelta(hours=hours)
    stop = min(window_end, begin + ceil((finish - begin) / SCHEDULE_STEP) * SCHEDULE_STEP)
    return ChargeSchedule(window_start, begin, begin, stop, finish <= window_end)


# The longest forced discharge before a free window. Nightly control writes the plan from two
# hours before the window opens, so a discharge must start inside that.
DISCHARGE_MAX = timedelta(minutes=90)
# Don't bother draining less than this, kWh stored.
DISCHARGE_MIN_KWH = 0.1


class ExportRates(Protocol):
    def rate_at(self, when: datetime, direction: str) -> float: ...


@dataclass(frozen=True)
class Discharge:
    """A forced discharge (export) that ends as a free window opens, in UTC.

    Energy left in the battery when power turns free is worth only what it saves later, and
    the free window refills it for nothing, so it is better sold first. `stored_kwh` leaves the
    cells; `export_kwh` reaches the grid after losses (less whatever the house uses meanwhile,
    which the battery would have supplied anyway).
    """

    start: datetime
    stop: datetime
    stored_kwh: float
    export_kwh: float
    revenue_pence: float

    @property
    def minutes(self) -> int:
        return int((self.stop - self.start).total_seconds() // 60)


def discharge(
    battery: BatterySpec, start_kwh: float, window_start: datetime, book: ExportRates
) -> Discharge | None:
    """Export down to the reserve just before the window, if any of it pays. Starts are rounded
    to five minutes earlier and the discharge is capped at `DISCHARGE_MAX`."""
    stored = start_kwh - battery.reserve_kwh
    if stored < DISCHARGE_MIN_KWH or battery.max_discharge_kw <= 0:
        return None
    hours = stored * battery.discharge_efficiency / battery.max_discharge_kw
    length = min(DISCHARGE_MAX, ceil(timedelta(hours=hours) / SCHEDULE_STEP) * SCHEDULE_STEP)
    begin = _floor(window_start - length, SCHEDULE_STEP)
    exported = min(
        stored * battery.discharge_efficiency,
        battery.max_discharge_kw * (window_start - begin).total_seconds() / 3600,
    )
    # Price each five minutes of the discharge at the export rate, the earliest first.
    revenue, left, moment = 0.0, exported, begin
    step_kwh = battery.max_discharge_kw * SCHEDULE_STEP.total_seconds() / 3600
    while left > 1e-9 and moment < window_start:
        part = min(step_kwh, left)
        revenue += part * book.rate_at(moment, "export")
        left -= part
        moment += SCHEDULE_STEP
    # Refilling it wears the battery; only sell when the export beats that.
    if revenue <= exported / battery.discharge_efficiency * battery.wear_pence_per_kwh:
        return None
    return Discharge(
        start=begin,
        stop=window_start,
        stored_kwh=exported / battery.discharge_efficiency,
        export_kwh=exported,
        revenue_pence=revenue,
    )
