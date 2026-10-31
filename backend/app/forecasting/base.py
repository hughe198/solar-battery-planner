"""Prediction model (step 3): forecast a day's load and solar generation per
interval from weather forecasts, trained on imported usage matched to
historical weather."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol

from app.models import System

# How a forecast was made: from the user's history, from a generic prior (typical UK usage, or
# panel physics at a standard efficiency), or a mix while history is still short.
Method = Literal["learned", "blended", "typical", "physics"]


@dataclass(frozen=True)
class IntervalForecast:
    """Predicted energy for one interval, in kWh, with a 10th-90th percentile range."""

    start: datetime
    end: datetime
    load_kwh: float
    solar_kwh: float
    load_low_kwh: float = 0.0
    load_high_kwh: float = 0.0
    solar_low_kwh: float = 0.0
    solar_high_kwh: float = 0.0
    temperature_c: float | None = None
    cloud_cover_percent: float | None = None


@dataclass(frozen=True)
class Range:
    expected: float
    low: float
    high: float


@dataclass(frozen=True)
class BacktestDay:
    day: date
    load_actual_kwh: float
    load_predicted_kwh: float
    solar_actual_kwh: float
    solar_predicted_kwh: float
    # What the physics estimate alone would have said, to show what learning adds.
    solar_physics_kwh: float


@dataclass(frozen=True)
class Backtest:
    """Accuracy on the most recent days, predicted by a model trained only on earlier days.

    Uses the weather that actually happened, so tomorrow's forecast will do somewhat worse on
    solar when the weather forecast itself is wrong.
    """

    train_days: int
    test_days: int
    load_mae_kwh: float
    load_error_percent: float
    solar_mae_kwh: float
    solar_error_percent: float | None
    solar_physics_error_percent: float | None
    days: list[BacktestDay]


@dataclass(frozen=True)
class DayForecast:
    day: date
    intervals: list[IntervalForecast]
    load: Range
    solar: Range
    load_method: Method
    solar_method: Method
    history_days: int
    # Learned multiplier on each array's physics estimate, in array order.
    array_performance: list[float]
    backtest: Backtest | None


class Forecaster(Protocol):
    def forecast(self, system: System, day: date) -> DayForecast: ...
