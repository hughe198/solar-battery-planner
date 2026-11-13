"""Prediction model (step 3): forecast a day's load and solar generation per
interval from weather forecasts, trained on imported usage matched to
historical weather."""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Literal, Protocol

from app.models import System

if TYPE_CHECKING:
    from app.forecasting.weather_story import WeatherStory

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
    # The usage the user marked for the day (away, low, normal or high). Marked days are left
    # out of load accuracy.
    usage: str = "normal"


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


# Days looked back over for how far solar has fallen short of its day-ahead forecast.
SHORTFALL_LOOKBACK_DAYS = 45


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
    # Plain sentences on how the user's marks and big loads changed the forecast.
    usage_notes: list[str] = field(default_factory=list)
    # What the weather does to the day's solar and usage, in a sentence and a few points.
    weather: "WeatherStory | None" = None
    # The most the day's solar has fallen short of its day-ahead forecast recently (kWh), when
    # the dull day's solar allows for that; None when it comes from the darkest recent day.
    solar_shortfall_kwh: float | None = None


class Forecaster(Protocol):
    def forecast(
        self, system: System, day: date, starts: list[datetime] | None = None
    ) -> DayForecast: ...
