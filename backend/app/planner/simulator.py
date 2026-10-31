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
from app.models import Battery

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


class PriceBook(Protocol):
    """Pence per kWh for the half hour containing a moment; `app.tariffs.TariffSchedule`."""

    def rate_at(self, when: datetime, direction: str) -> float: ...


@dataclass(frozen=True)
class CyclePrices:
    """Pence per kWh for each half hour of a cycle."""

    night_import: np.ndarray
    night_export: np.ndarray
    day_import: np.ndarray
    day_export: np.ndarray

    @classmethod
    def of(cls, book: PriceBook, night: list[datetime], day: list[datetime]) -> "CyclePrices":
        def rates(starts, direction):
            return np.array([book.rate_at(s, direction) for s in starts], dtype=float)

        return cls(
            night_import=rates(night, "import"),
            night_export=rates(night, "export"),
            day_import=rates(day, "import"),
            day_export=rates(day, "export"),
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


# The charge window is looked for between these local times, overnight into the day.
SEARCH_FROM = time(18, 0)
SEARCH_TO = time(12, 0)
# Rates within this of the cheapest count as the same rate.
RATE_TOLERANCE = 0.01


def charge_window(day: date, book: PriceBook, tz: str) -> tuple[datetime, datetime] | None:
    """The overnight charge window ending on local `day`, from the import rates.

    It is the longest unbroken run of the cheapest import rate between 18:00 the evening
    before and noon (the later one on a tie, as charging closer to morning loses less while
    the battery waits). None when nothing overnight is cheaper than the rest of the day, as
    on a single-rate tariff, so charging from the grid never pays.
    """
    zone = ZoneInfo(tz)
    start = datetime.combine(day - timedelta(days=1), SEARCH_FROM, zone).astimezone(timezone.utc)
    end = datetime.combine(day, SEARCH_TO, zone).astimezone(timezone.utc)
    starts = _half_hours(start, end)
    rates = np.array([book.rate_at(s, "import") for s in starts])
    day_end = datetime.combine(day + timedelta(days=1), time(), zone).astimezone(timezone.utc)
    later = [book.rate_at(s, "import") for s in _half_hours(end, day_end)]
    cheapest = rates.min()
    if cheapest >= max(rates.max(), *later) - RATE_TOLERANCE:
        return None
    best: tuple[int, int] | None = None
    run_start = None
    for i, cheap in enumerate(list(rates <= cheapest + RATE_TOLERANCE) + [False]):
        if cheap and run_start is None:
            run_start = i
        elif not cheap and run_start is not None:
            if best is None or i - run_start >= best[1] - best[0]:
                best = (run_start, i)
            run_start = None
    assert best is not None
    return starts[best[0]], starts[best[1] - 1] + INTERVAL


def cycle_for(day: date, book: PriceBook, tz: str) -> Cycle:
    """The charge window that ends on local `day`, and the day until the next one opens.

    Without a cheaper overnight rate, the cycle is the local day itself with no charge
    window.
    """
    zone = ZoneInfo(tz)
    window = charge_window(day, book, tz)
    following = charge_window(day + timedelta(days=1), book, tz)
    if window is None:
        midnight = datetime.combine(day, time(), zone).astimezone(timezone.utc)
        window = (midnight, midnight)
    if following is None or following[0] <= window[1]:
        day_end = datetime.combine(day + timedelta(days=1), time(), zone).astimezone(timezone.utc)
    else:
        day_end = following[0]
    return Cycle(window[0], window[1], day_end)


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
