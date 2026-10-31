"""The learned half-hourly load and PV models.

Both are ridge regressions shrunk towards a prior rather than towards zero, so they behave
sensibly with any amount of history: with none they return the prior (a typical UK home's
usage, and the physics PV estimate at a standard performance ratio), and as data arrives the
user's own pattern takes over. Each prior is worth `PRIOR_DAYS` days of data.

Load: one coefficient per local half-hour slot for weekdays and another set for weekends,
plus heating-degree terms for four blocks of the day (so a heat pump's morning and evening
peaks can grow on cold days independently). Recent days count for more.

PV: one multiplier per array on the physics estimate, which soaks up shading, inverter
losses, soiling and any error in the stated kWp or orientation.
"""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from app.forecasting.solar import DEFAULT_PERFORMANCE_RATIO

SLOTS = 48
PRIOR_DAYS = 3.0
RECENCY_HALF_LIFE_DAYS = 60.0
HEATING_BASE_C = 15.5
HEATING_BLOCKS = 4
# Ofgem's typical domestic electricity use (medium, profile class 1): 2,700 kWh a year.
TYPICAL_DAILY_KWH = 2700 / 365

# Relative half-hourly shape of an average UK home from 00:00 local time, after Elexon's
# domestic unrestricted profile (overnight trough, morning shoulder, evening peak).
TYPICAL_SHAPE = np.array(
    [
        0.75, 0.68, 0.62, 0.58, 0.55, 0.53, 0.52, 0.52, 0.52, 0.53, 0.56, 0.62,  # 00:00-05:30
        0.72, 0.88, 1.02, 1.08, 1.08, 1.04, 1.00, 0.98, 0.97, 0.97, 0.98, 1.00,  # 06:00-11:30
        1.02, 1.01, 0.98, 0.95, 0.94, 0.96, 1.02, 1.14, 1.32, 1.50, 1.62, 1.66,  # 12:00-17:30
        1.64, 1.58, 1.50, 1.42, 1.36, 1.30, 1.24, 1.18, 1.10, 1.02, 0.92, 0.83,  # 18:00-23:30
    ]
)  # fmt: skip


@dataclass(frozen=True)
class Calendar:
    """Local-time facts about each half hour the models need."""

    slot: np.ndarray  # 0..47, local half-hour of the day
    weekend: np.ndarray  # bool
    local_day: list  # date of each half hour in local time

    @classmethod
    def of(cls, starts: list[datetime], tz: str) -> "Calendar":
        zone = ZoneInfo(tz)
        local = [s.astimezone(zone) for s in starts]
        return cls(
            slot=np.array([lt.hour * 2 + lt.minute // 30 for lt in local], dtype=int),
            weekend=np.array([lt.weekday() >= 5 for lt in local], dtype=bool),
            local_day=[lt.date() for lt in local],
        )


def typical_profile(daily_kwh: float = TYPICAL_DAILY_KWH) -> np.ndarray:
    return TYPICAL_SHAPE / TYPICAL_SHAPE.sum() * daily_kwh


def daily_heating_degrees(cal: Calendar, temperature_c: np.ndarray) -> np.ndarray:
    """Degrees below the heating base, from each local day's mean temperature."""
    days = np.array([d.toordinal() for d in cal.local_day])
    out = np.zeros(len(days))
    for d in np.unique(days):
        mask = days == d
        out[mask] = max(0.0, HEATING_BASE_C - float(temperature_c[mask].mean()))
    return out


def _ridge_to_prior(
    x: np.ndarray, y: np.ndarray, w: np.ndarray, prior: np.ndarray, strength: np.ndarray
) -> np.ndarray:
    """argmin_b  sum w (y - x b)^2 + sum strength (b - prior)^2."""
    xtw = x.T * w
    a = xtw @ x + np.diag(strength)
    return np.linalg.solve(a, xtw @ y + strength * prior)


class LoadModel:
    def __init__(self, coef: np.ndarray, trained_days: int) -> None:
        self.coef = coef
        self.trained_days = trained_days

    @staticmethod
    def design(cal: Calendar, heating: np.ndarray) -> np.ndarray:
        n = len(cal.slot)
        x = np.zeros((n, 2 * SLOTS + HEATING_BLOCKS))
        rows = np.arange(n)
        x[rows, cal.slot + SLOTS * cal.weekend] = 1.0
        x[rows, 2 * SLOTS + cal.slot * HEATING_BLOCKS // SLOTS] = heating
        return x

    @classmethod
    def fit(
        cls,
        cal: Calendar,
        heating: np.ndarray,
        load_kwh: np.ndarray,
        newest_day_ordinal: int | None = None,
    ) -> "LoadModel":
        days = np.array([d.toordinal() for d in cal.local_day])
        trained_days = len(np.unique(days))
        # The prior keeps the typical shape but is scaled to this home's average if known.
        daily = load_kwh.sum() / trained_days if trained_days else TYPICAL_DAILY_KWH
        base = typical_profile(daily if daily > 0 else TYPICAL_DAILY_KWH)
        prior = np.concatenate([base, base, np.zeros(HEATING_BLOCKS)])
        if not trained_days:
            return cls(prior, 0)

        newest = newest_day_ordinal if newest_day_ordinal is not None else days.max()
        w = 0.5 ** ((newest - days) / RECENCY_HALF_LIFE_DAYS)
        x = cls.design(cal, heating)
        # Each slot coefficient sees about one observation per day, so PRIOR_DAYS of prior
        # weight per slot. Heating terms see a whole block of a day per observation.
        slot_strength = np.full(2 * SLOTS, PRIOR_DAYS)
        heat_strength = np.full(HEATING_BLOCKS, PRIOR_DAYS * SLOTS / HEATING_BLOCKS * 25.0)
        coef = _ridge_to_prior(
            x, load_kwh, w, prior, np.concatenate([slot_strength, heat_strength])
        )
        return cls(coef, trained_days)

    def predict(self, cal: Calendar, heating: np.ndarray) -> np.ndarray:
        return np.clip(self.design(cal, heating) @ self.coef, 0.0, None)


class SolarModel:
    def __init__(self, multipliers: np.ndarray, trained_days: int) -> None:
        self.multipliers = multipliers
        self.trained_days = trained_days

    @classmethod
    def physics(cls, arrays: int) -> "SolarModel":
        return cls(np.full(arrays, DEFAULT_PERFORMANCE_RATIO), 0)

    @classmethod
    def fit(cls, features: np.ndarray, solar_kwh: np.ndarray, trained_days: int) -> "SolarModel":
        arrays = features.shape[1]
        prior = np.full(arrays, DEFAULT_PERFORMANCE_RATIO)
        if not trained_days:
            return cls(prior, 0)
        # A day's worth of an array's ideal output squared, so PRIOR_DAYS means the same here as
        # for the load model whatever the array size.
        per_day = (features**2).sum(axis=0) / trained_days
        strength = PRIOR_DAYS * np.maximum(per_day, 1e-6)
        coef = _ridge_to_prior(features, solar_kwh, np.ones(len(solar_kwh)), prior, strength)
        # Arrays that face the same way are indistinguishable; the ridge splits them, and the
        # clip stops one going negative to offset the other.
        return cls(np.clip(coef, 0.0, 1.3), trained_days)

    def predict(self, features: np.ndarray) -> np.ndarray:
        return np.clip(features @ self.multipliers, 0.0, None)
