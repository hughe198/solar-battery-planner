"""Expected usage and big loads: what the user tells the forecast about a day, and the electric
car charging that is taken out of the history so the house model isn't skewed by it.

Everything here is plain arithmetic on the history, so each adjustment can be explained in a
sentence:

- *Away*: each half hour is capped at the home's base load, the typical 00:30-05:00 use over
  the last four weeks, when nothing but the fridge and standby loads run. A heat pump's share
  is kept on top, since the house is still heated while empty.
- *Low* and *High*: the day is scaled by how the home's quietest and busiest tenth of days
  compare with its median day.
- *Car*: on a night the user says the car will charge, its kWh are added at the charger's power
  from the moment the cheap window opens. The battery is held in the window, so the car runs on
  cheap grid power, as it does in the planner's simulation.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np

from app.forecasting.model import Calendar
from app.importers.base import INTERVAL
from app.models import UsageLevel

HOURS_PER_INTERVAL = INTERVAL.total_seconds() / 3600

# Base load: local half hours 00:30 to 05:00, over this many recent days.
BASE_SLOTS = np.arange(1, 10)
BASE_DAYS = 28
# Below this many unmarked days, fixed factors are used instead of the home's own.
LEARNED_DAYS = 14
FIXED_AWAY_FACTOR = 0.5
FIXED_LOW_FACTOR = 0.7
FIXED_HIGH_FACTOR = 1.35
LOW_FACTOR_RANGE = (0.5, 0.95)
HIGH_FACTOR_RANGE = (1.05, 2.0)
# An empty house is predictable: likely range of an Away half hour and day, against expected.
AWAY_INTERVAL_RANGE = (0.85, 1.25)
AWAY_DAY_RANGE = (0.9, 1.15)

# A half hour counts as car charging when it is this share of a full half hour at the charger's
# power above that slot's usual use, for at least CAR_MIN_RUN half hours in a row (an oven or a
# shower can match one half hour, but not an hour). Half hours either side of a session that are
# at least CAR_EDGE_SHARE above usual are its partial start and end.
CAR_CORE_SHARE = 0.6
CAR_EDGE_SHARE = 0.2
CAR_MIN_RUN = 2


@dataclass(frozen=True)
class Marks:
    """The user's non-normal days, by local date, and car charges by the day they lead into."""

    levels: dict[date, UsageLevel] = field(default_factory=dict)
    car_kwh: dict[date, float] = field(default_factory=dict)

    def level(self, day: date) -> UsageLevel:
        return self.levels.get(day, UsageLevel.normal)

    def marked(self, days: list[date]) -> np.ndarray:
        """Half hours on days marked other than normal, which the usage model mustn't learn
        from."""
        return np.array(
            [self.level(d) is not UsageLevel.normal for d in days], dtype=bool
        ).reshape(-1)


def detect_car_charging(
    starts: list[datetime], slot: np.ndarray, load_kwh: np.ndarray, charger_kw: float
) -> np.ndarray:
    """kWh of each half hour's load that looks like car charging (0 elsewhere).

    Usual use is the median for that half hour of the day across the history. A session is a
    run of consecutive half hours well above usual, near the charger's power."""
    n = len(load_kwh)
    car = np.zeros(n)
    if n == 0 or charger_kw <= 0:
        return car
    usual = np.zeros(48)
    for s in range(48):
        here = load_kwh[slot == s]
        if len(here):
            usual[s] = float(np.median(here))
    excess = load_kwh - usual[slot]
    full = charger_kw * HOURS_PER_INTERVAL
    core = excess >= CAR_CORE_SHARE * full
    follows = np.array(
        [i > 0 and starts[i] - starts[i - 1] == INTERVAL for i in range(n)], dtype=bool
    )

    flagged = np.zeros(n, dtype=bool)
    i = 0
    while i < n:
        if not core[i]:
            i += 1
            continue
        j = i + 1
        while j < n and core[j] and follows[j]:
            j += 1
        if j - i >= CAR_MIN_RUN:
            flagged[i:j] = True
            if i > 0 and follows[i] and excess[i - 1] >= CAR_EDGE_SHARE * full:
                flagged[i - 1] = True
            if j < n and follows[j] and excess[j] >= CAR_EDGE_SHARE * full:
                flagged[j] = True
        i = j
    car[flagged] = np.clip(excess[flagged], 0, full)
    return car


@dataclass(frozen=True)
class UsageProfile:
    """The home's own figures behind Away, Low and High, from recent unmarked days."""

    base_kwh_per_half_hour: float | None  # None: too little history, Away halves the forecast
    low_factor: float
    high_factor: float
    learned: bool

    @property
    def base_daily_kwh(self) -> float | None:
        if self.base_kwh_per_half_hour is None:
            return None
        return self.base_kwh_per_half_hour * 48

    def factor(self, level: UsageLevel) -> float:
        return {UsageLevel.low: self.low_factor, UsageLevel.high: self.high_factor}.get(level, 1.0)

    @classmethod
    def fixed(cls) -> "UsageProfile":
        return cls(None, FIXED_LOW_FACTOR, FIXED_HIGH_FACTOR, False)

    @classmethod
    def of(cls, calendar: Calendar, house_kwh: np.ndarray, heating_kwh: np.ndarray) -> "UsageProfile":
        """From unmarked history with car charging already taken out. The heat pump's share
        (`heating_kwh`, zero without one) is left out of the base load, as Away adds it back."""
        days = np.array([d.toordinal() for d in calendar.local_day], dtype=int)
        unique, counts = np.unique(days, return_counts=True)
        complete = unique[counts >= 46]
        if len(complete) < LEARNED_DAYS:
            return cls.fixed()
        totals = np.array([house_kwh[days == d].sum() for d in complete])
        median = float(np.median(totals))
        if median <= 0:
            return cls.fixed()
        low, high = np.percentile(totals, [10, 90])

        recent = complete[-BASE_DAYS:]
        nights = []
        for d in recent:
            m = (days == d) & np.isin(calendar.slot, BASE_SLOTS)
            if m.any():
                nights.append(float((house_kwh[m] - heating_kwh[m]).mean()))
        base = max(float(np.median(nights)), 0.0) if nights else None
        return cls(
            base_kwh_per_half_hour=base,
            low_factor=float(np.clip(low / median, *LOW_FACTOR_RANGE)),
            high_factor=float(np.clip(high / median, *HIGH_FACTOR_RANGE)),
            learned=True,
        )


@dataclass(frozen=True)
class Adjusted:
    """A forecast's load after the user's marks, with its likely range."""

    load: np.ndarray
    low: np.ndarray
    high: np.ndarray
    # Per half hour: the level applied, and kWh added for the car.
    levels: list[UsageLevel]
    car_kwh: np.ndarray


def adjust(
    load: np.ndarray,
    low: np.ndarray,
    high: np.ndarray,
    heating_kwh: np.ndarray,
    calendar: Calendar,
    marks: Marks,
    profile: UsageProfile,
    car: np.ndarray,
) -> Adjusted:
    """Apply each half hour's day level, then add the car's planned charging (`car`, kWh)."""
    load, low, high = load.copy(), low.copy(), high.copy()
    levels = [marks.level(d) for d in calendar.local_day]
    for i, level in enumerate(levels):
        if level is UsageLevel.away:
            if profile.base_kwh_per_half_hour is None:
                load[i] *= FIXED_AWAY_FACTOR
            else:
                house = max(load[i] - heating_kwh[i], 0.0)
                load[i] = min(house, profile.base_kwh_per_half_hour) + heating_kwh[i]
            low[i] = load[i] * AWAY_INTERVAL_RANGE[0]
            high[i] = load[i] * AWAY_INTERVAL_RANGE[1]
        elif level is not UsageLevel.normal:
            factor = profile.factor(level)
            load[i] *= factor
            low[i] *= factor
            high[i] *= factor
    return Adjusted(load + car, low + car, high + car, levels, car)


def car_load(
    starts: list[datetime], charges: list[tuple[datetime, float]], charger_kw: float
) -> np.ndarray:
    """kWh per half hour for car charges, each (start, kWh) drawing at the charger's power from
    its start until delivered."""
    out = np.zeros(len(starts))
    index = {s: i for i, s in enumerate(starts)}
    full = charger_kw * HOURS_PER_INTERVAL
    for begin, kwh in charges:
        left, moment = kwh, begin
        while left > 1e-9 and full > 0:
            take = min(full, left)
            if moment in index:
                out[index[moment]] += take
            left -= take
            moment += timedelta(minutes=30)
    return out
