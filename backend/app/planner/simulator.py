"""Half-hourly battery simulation for one charging cycle, for many overnight targets at once.

A cycle is the cheap window (the battery is charged from the grid to a target and holds; the
house runs off the grid at the cheap rate) followed by the day until the next cheap window (the
inverter is in self-use: solar feeds the house first, surplus fills the battery and the rest is
exported; shortfalls are met from the battery down to its reserve, then from the grid).

Every candidate target is simulated side by side as one numpy array, so comparing a hundred
targets costs about the same as one.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from math import sqrt
from typing import Protocol
from zoneinfo import ZoneInfo

import numpy as np

from app.importers.base import INTERVAL
from app.models import Battery, Tariff

HOURS_PER_INTERVAL = INTERVAL.total_seconds() / 3600


@dataclass(frozen=True)
class BatterySpec:
    """Energies are what is stored in the cells; losses are split evenly between in and out."""

    capacity_kwh: float
    reserve_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    charge_efficiency: float
    discharge_efficiency: float

    @classmethod
    def of(cls, battery: Battery) -> "BatterySpec":
        one_way = sqrt(battery.round_trip_efficiency_percent / 100)
        return cls(
            capacity_kwh=battery.capacity_kwh,
            reserve_kwh=battery.capacity_kwh * battery.min_soc_percent / 100,
            max_charge_kw=battery.max_charge_kw,
            max_discharge_kw=battery.max_discharge_kw,
            charge_efficiency=one_way,
            discharge_efficiency=one_way,
        )

    def percent(self, kwh):
        return kwh / self.capacity_kwh * 100

    def kwh(self, percent):
        return np.asarray(percent, dtype=float) * self.capacity_kwh / 100

    def grid_kwh_to_store(self, stored_kwh):
        """Grid energy needed to put `stored_kwh` into the cells."""
        return stored_kwh / self.charge_efficiency

    def max_target_kwh(self, start_kwh: float, window: timedelta) -> float:
        """Highest stored energy reachable by the end of a charging window."""
        hours = window.total_seconds() / 3600
        return min(
            self.capacity_kwh, start_kwh + self.max_charge_kw * hours * self.charge_efficiency
        )


class PriceBook(Protocol):
    """Import and export price, in pence per kWh, for the half hour starting at a UTC moment.

    All pricing goes through this, so tariffs with several rates through the day plug in by
    providing another implementation.
    """

    def import_rate(self, start: datetime) -> float: ...

    def export_rate(self, start: datetime) -> float: ...


def in_cheap_window(moment: time, tariff: Tariff) -> bool:
    start, end = tariff.cheap_start, tariff.cheap_end
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end


class TariffPrices:
    """A day rate with one cheap import window, and a flat export rate."""

    def __init__(self, tariff: Tariff, tz: str) -> None:
        self.tariff = tariff
        self.zone = ZoneInfo(tz)

    def import_rate(self, start: datetime) -> float:
        local = start.astimezone(self.zone).time()
        if in_cheap_window(local, self.tariff):
            return self.tariff.cheap_import_rate
        return self.tariff.day_import_rate

    def export_rate(self, start: datetime) -> float:
        return self.tariff.export_rate


@dataclass(frozen=True)
class CyclePrices:
    """Pence per kWh for each half hour of a cycle."""

    night_import: np.ndarray
    night_export: np.ndarray
    day_import: np.ndarray
    day_export: np.ndarray

    @classmethod
    def of(cls, book: PriceBook, night: list[datetime], day: list[datetime]) -> "CyclePrices":
        def rates(starts, fn):
            return np.array([fn(s) for s in starts], dtype=float)

        return cls(
            night_import=rates(night, book.import_rate),
            night_export=rates(night, book.export_rate),
            day_import=rates(day, book.import_rate),
            day_export=rates(day, book.export_rate),
        )

    @property
    def charge(self) -> float:
        """What charging costs per grid kWh: spread evenly across the window."""
        return float(self.night_import.mean()) if len(self.night_import) else 0.0

    @property
    def day_import_mean(self) -> float:
        return float(self.day_import.mean()) if len(self.day_import) else 0.0

    @property
    def day_export_mean(self) -> float:
        return float(self.day_export.mean()) if len(self.day_export) else 0.0


@dataclass(frozen=True)
class Cycle:
    """The charging window ending on a local day and the day that follows it, in UTC."""

    night_start: datetime
    night_end: datetime
    day_end: datetime

    @property
    def window(self) -> timedelta:
        return self.night_end - self.night_start

    def night_starts(self) -> list[datetime]:
        return _half_hours(self.night_start, self.night_end)

    def day_starts(self) -> list[datetime]:
        return _half_hours(self.night_end, self.day_end)


def _half_hours(start: datetime, end: datetime) -> list[datetime]:
    return [start + INTERVAL * i for i in range(int((end - start) / INTERVAL))]


def _floor_half_hour(moment: time) -> time:
    return moment.replace(minute=moment.minute - moment.minute % 30, second=0, microsecond=0)


def cycle_for(day: date, tariff: Tariff, tz: str) -> Cycle:
    """The cheap window that ends on local `day`, and the day until the next window opens.

    A window crossing midnight (23:30 to 05:30) opens the evening before; one after midnight
    (00:30 to 04:30) opens on the day, and the next one the night after.
    """
    zone = ZoneInfo(tz)
    start, end = _floor_half_hour(tariff.cheap_start), _floor_half_hour(tariff.cheap_end)

    def at(d: date, t: time) -> datetime:
        return datetime.combine(d, t, zone).astimezone(timezone.utc)

    if start > end:
        return Cycle(at(day - timedelta(days=1), start), at(day, end), at(day, start))
    return Cycle(at(day, start), at(day, end), at(day + timedelta(days=1), start))


@dataclass(frozen=True)
class Simulation:
    """Results for each candidate (rows) and day interval (columns), in kWh."""

    targets_kwh: np.ndarray
    # Stored energy at the start of each day interval and at the end of the last: (n, T + 1).
    stored_kwh: np.ndarray
    day_import_kwh: np.ndarray
    day_export_kwh: np.ndarray
    # Grid energy for the night: the house's net use plus charging (or less any discharge).
    night_import_kwh: np.ndarray
    night_export_kwh: np.ndarray
    # The house's own net use per night interval, and the grid energy for the charge per
    # candidate (negative when the battery covered some of the night instead).
    night_net_kwh: np.ndarray
    charge_grid_kwh: np.ndarray

    @property
    def end_kwh(self) -> np.ndarray:
        return self.stored_kwh[:, -1]


def simulate(
    battery: BatterySpec,
    start_kwh: float,
    targets_kwh: np.ndarray,
    night_load: np.ndarray,
    night_solar: np.ndarray,
    day_load: np.ndarray,
    day_solar: np.ndarray,
) -> Simulation:
    """Charge (or run down) from `start_kwh` to each target overnight, then run the day."""
    targets = np.asarray(targets_kwh, dtype=float)
    added = targets - start_kwh
    # Charging draws from the grid; a target below the start means the battery covered some
    # of the night's use instead.
    night_net = night_load - night_solar
    charge_grid = (
        battery.grid_kwh_to_store(np.clip(added, 0, None))
        - np.clip(-added, 0, None) * battery.discharge_efficiency
    )
    night_grid = np.clip(night_net, 0, None).sum() + charge_grid
    night_import = np.clip(night_grid, 0, None)
    night_export = np.clip(-night_grid, 0, None) + np.clip(-night_net, 0, None).sum()

    n, steps = len(targets), len(day_load)
    stored = np.empty((n, steps + 1))
    stored[:, 0] = targets
    imports = np.zeros((n, steps))
    exports = np.zeros((n, steps))
    charge_cap = battery.max_charge_kw * HOURS_PER_INTERVAL
    discharge_cap = battery.max_discharge_kw * HOURS_PER_INTERVAL
    for t in range(steps):
        level = stored[:, t]
        net = day_solar[t] - day_load[t]
        if net >= 0:
            room = (battery.capacity_kwh - level) / battery.charge_efficiency
            into = np.clip(np.minimum(net, np.minimum(charge_cap, room)), 0, None)
            stored[:, t + 1] = level + into * battery.charge_efficiency
            exports[:, t] = net - into
        else:
            available = (level - battery.reserve_kwh) * battery.discharge_efficiency
            out = np.clip(np.minimum(-net, np.minimum(discharge_cap, available)), 0, None)
            stored[:, t + 1] = level - out / battery.discharge_efficiency
            imports[:, t] = -net - out
    return Simulation(
        targets_kwh=targets,
        stored_kwh=stored,
        day_import_kwh=imports,
        day_export_kwh=exports,
        night_import_kwh=night_import,
        night_export_kwh=night_export,
        night_net_kwh=night_net,
        charge_grid_kwh=charge_grid,
    )


@dataclass(frozen=True)
class Costs:
    """Pence per candidate. `stored_value` credits energy left in (or taken from) the battery
    at what it costs to put back at the cheap rate, so a candidate isn't flattered by leaving
    the battery empty for tomorrow night, or penalised for leaving it full."""

    import_cost: np.ndarray
    export_revenue: np.ndarray
    stored_value: np.ndarray

    @property
    def net(self) -> np.ndarray:
        return self.import_cost - self.export_revenue


def costs(sim: Simulation, battery: BatterySpec, prices: CyclePrices, start_kwh: float) -> Costs:
    house_night = np.clip(sim.night_net_kwh, 0, None) @ prices.night_import
    night_cost = np.where(
        sim.night_import_kwh > 0,
        np.clip(house_night + sim.charge_grid_kwh * prices.charge, 0, None),
        0,
    )
    surplus_night = np.clip(-sim.night_net_kwh, 0, None) @ prices.night_export
    night_spill = (sim.night_export_kwh - np.clip(-sim.night_net_kwh, 0, None).sum()) * (
        float(prices.night_export.mean()) if len(prices.night_export) else 0.0
    )
    return Costs(
        import_cost=night_cost + sim.day_import_kwh @ prices.day_import,
        export_revenue=sim.day_export_kwh @ prices.day_export + surplus_night + night_spill,
        stored_value=battery.grid_kwh_to_store(sim.end_kwh - start_kwh) * prices.charge,
    )
