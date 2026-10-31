"""Train on a system's history, check accuracy on recent days, and forecast a day ahead.

Training is a few small least-squares solves, so it runs on every request rather than being
stored; a forecast always reflects the latest import and the current system description.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.forecasting.base import (
    Backtest,
    BacktestDay,
    DayForecast,
    IntervalForecast,
    Method,
    Range,
)
from app.forecasting.model import Calendar, LoadModel, SolarModel, daily_heating_degrees
from app.forecasting.solar import HalfHourWeather, Orientation, array_features, split_hours
from app.importers.base import INTERVAL
from app.models import System, UsageRecord, WeatherRecord
from app.weather.open_meteo import HourlyWeather, OpenMeteoClient, WeatherError

# Below this many days of history the generic prior still carries real weight.
LEARNED_DAYS = 14
# A local day needs this many half hours (with weather) to count towards daily accuracy.
COMPLETE_DAY_INTERVALS = 46
MIN_BACKTEST_DAYS = 21
MAX_TEST_DAYS = 28
MIN_TEST_DAYS = 7
# The backtest uses observed weather; tomorrow's is a forecast, so solar ranges are widened,
# and never narrower than the floors below (a rough allowance for day-ahead cloud forecast
# error until forecasts are logged and compared with what happened).
WEATHER_FORECAST_WIDENING = 1.5
SOLAR_INTERVAL_FLOOR = (0.6, 1.3)
SOLAR_DAY_FLOOR = (0.8, 1.15)
# Half hours predicted below this share of the panels' rated output (dawn, dusk, heavy cloud)
# have wild actual/predicted ratios, so they are left out when sizing solar ranges.
MIN_SOLAR_SHARE_FOR_RATIO = 0.1
# Ratios are capped so one odd interval can't blow the range up.
MAX_RATIO = 3.0
LOAD_BLOCK_SLOTS = 8  # residual quantiles per four-hour block of the day

# Ranges (actual / predicted, 10th and 90th percentile) used before there is a backtest.
DEFAULT_LOAD_INTERVAL = (0.6, 1.6)
DEFAULT_LOAD_DAY = (0.8, 1.25)
DEFAULT_SOLAR_INTERVAL = (0.4, 1.4)
DEFAULT_SOLAR_DAY = (0.6, 1.3)


@dataclass(frozen=True)
class History:
    """Half hours that have both usage and weather, oldest first."""

    weather: HalfHourWeather
    calendar: Calendar
    load_kwh: np.ndarray
    solar_kwh: np.ndarray

    def __len__(self) -> int:
        return len(self.load_kwh)

    def take(self, index: np.ndarray) -> "History":
        return History(
            weather=self.weather.take(index),
            calendar=Calendar(
                slot=self.calendar.slot[index],
                weekend=self.calendar.weekend[index],
                local_day=[self.calendar.local_day[i] for i in index],
            ),
            load_kwh=self.load_kwh[index],
            solar_kwh=self.solar_kwh[index],
        )

    def day_ordinals(self) -> np.ndarray:
        return np.array([d.toordinal() for d in self.calendar.local_day], dtype=int)


@dataclass(frozen=True)
class Models:
    load: LoadModel
    solar: SolarModel


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc)


def _orientations(system: System) -> list[Orientation]:
    return [Orientation(a.kwp, a.tilt_degrees, a.azimuth_degrees) for a in system.arrays]


def _half_hour_weather(system: System, hours: list[HourlyWeather]) -> HalfHourWeather:
    return split_hours(
        [h.start for h in hours],
        [h.shortwave_radiation_wm2 for h in hours],
        [h.diffuse_radiation_wm2 for h in hours],
        [h.temperature_c for h in hours],
        [h.cloud_cover_percent for h in hours],
        system.latitude,
        system.longitude,
    )


def load_history(db: Session, system: System) -> History:
    usage = db.scalars(
        select(UsageRecord).where(UsageRecord.system_id == system.id).order_by(UsageRecord.start)
    ).all()
    weather = {
        _utc(w.start): w
        for w in db.scalars(select(WeatherRecord).where(WeatherRecord.system_id == system.id))
        if w.shortwave_radiation_wm2 is not None
    }
    by_start = {_utc(u.start): u for u in usage if u.load_kwh >= 0 and u.solar_kwh >= 0}
    hours = sorted({s.replace(minute=0) for s in by_start} & weather.keys())
    hourly = [
        HourlyWeather(
            start=h,
            temperature_c=weather[h].temperature_c,
            cloud_cover_percent=weather[h].cloud_cover_percent,
            shortwave_radiation_wm2=weather[h].shortwave_radiation_wm2,
            direct_radiation_wm2=weather[h].direct_radiation_wm2,
            diffuse_radiation_wm2=weather[h].diffuse_radiation_wm2,
        )
        for h in hours
    ]
    half_hours = _half_hour_weather(system, hourly)
    index = np.array([i for i, s in enumerate(half_hours.starts) if s in by_start], dtype=int)
    half_hours = half_hours.take(index)
    rows = [by_start[s] for s in half_hours.starts]
    return History(
        weather=half_hours,
        calendar=Calendar.of(half_hours.starts, system.timezone),
        load_kwh=np.array([r.load_kwh for r in rows], dtype=float),
        solar_kwh=np.array([r.solar_kwh for r in rows], dtype=float),
    )


def fit(system: System, history: History) -> Models:
    days = len(set(history.calendar.local_day))
    heating = daily_heating_degrees(history.calendar, history.weather.temperature_c)
    load = LoadModel.fit(history.calendar, heating, history.load_kwh)
    if not system.arrays:
        return Models(load, SolarModel(np.zeros(0), 0))
    features = array_features(
        history.weather, _orientations(system), system.latitude, system.longitude
    )
    return Models(load, SolarModel.fit(features, history.solar_kwh, days))


def predict(
    system: System, models: Models, weather: HalfHourWeather, calendar: Calendar
) -> tuple[np.ndarray, np.ndarray]:
    heating = daily_heating_degrees(calendar, weather.temperature_c)
    load = models.load.predict(calendar, heating)
    if not system.arrays:
        return load, np.zeros(len(load))
    features = array_features(weather, _orientations(system), system.latitude, system.longitude)
    return load, models.solar.predict(features)


@dataclass(frozen=True)
class Spread:
    """Empirical 10th/90th percentile errors, from a backtest or the defaults."""

    # Additive load offsets (kWh) per four-hour block, or None to use DEFAULT_LOAD_INTERVAL.
    load_interval: np.ndarray | None
    solar_interval: tuple[float, float]
    load_day: tuple[float, float]
    solar_day: tuple[float, float]

    @classmethod
    def default(cls) -> "Spread":
        return cls(None, DEFAULT_SOLAR_INTERVAL, DEFAULT_LOAD_DAY, DEFAULT_SOLAR_DAY)


def _widen(ratios: tuple[float, float], floor: tuple[float, float]) -> tuple[float, float]:
    low, high = ratios
    return (
        max(0.0, min(floor[0], 1 - (1 - low) * WEATHER_FORECAST_WIDENING)),
        max(floor[1], 1 + (high - 1) * WEATHER_FORECAST_WIDENING),
    )


def _quantile_ratios(actual: np.ndarray, predicted: np.ndarray, default: tuple[float, float]):
    mask = predicted > 0
    if mask.sum() < 5:
        return default
    ratios = np.clip(actual[mask] / predicted[mask], 0.0, MAX_RATIO)
    low, high = np.percentile(ratios, [10, 90])
    return float(min(low, 1.0)), float(max(high, 1.0))


def backtest(system: System, history: History) -> tuple[Backtest, Spread] | None:
    """Train on everything before the last few complete days, then predict those days."""
    days = history.day_ordinals()
    unique, counts = np.unique(days, return_counts=True)
    complete = unique[counts >= COMPLETE_DAY_INTERVALS]
    if len(complete) < MIN_BACKTEST_DAYS:
        return None
    test_count = int(np.clip(len(complete) // 4, MIN_TEST_DAYS, MAX_TEST_DAYS))
    test_days = complete[-test_count:]
    train = history.take(np.flatnonzero(days < test_days[0]))
    test = history.take(np.flatnonzero(np.isin(days, test_days)))

    models = fit(system, train)
    load_pred, solar_pred = predict(system, models, test.weather, test.calendar)
    _, physics_pred = predict(
        system,
        Models(models.load, SolarModel.physics(len(system.arrays))),
        test.weather,
        test.calendar,
    )

    test_ordinals = test.day_ordinals()
    rows = []
    for d in test_days:
        m = test_ordinals == d
        rows.append(
            BacktestDay(
                day=date.fromordinal(int(d)),
                load_actual_kwh=float(test.load_kwh[m].sum()),
                load_predicted_kwh=float(load_pred[m].sum()),
                solar_actual_kwh=float(test.solar_kwh[m].sum()),
                solar_predicted_kwh=float(solar_pred[m].sum()),
                solar_physics_kwh=float(physics_pred[m].sum()),
            )
        )
    load_actual = np.array([r.load_actual_kwh for r in rows])
    load_daily = np.array([r.load_predicted_kwh for r in rows])
    solar_actual = np.array([r.solar_actual_kwh for r in rows])
    solar_daily = np.array([r.solar_predicted_kwh for r in rows])
    physics_daily = np.array([r.solar_physics_kwh for r in rows])

    def error_percent(predicted: np.ndarray, actual: np.ndarray) -> float | None:
        total = actual.sum()
        return float(np.abs(predicted - actual).sum() / total * 100) if total > 0 else None

    result = Backtest(
        train_days=len(set(train.calendar.local_day)),
        test_days=test_count,
        load_mae_kwh=float(np.abs(load_daily - load_actual).mean()),
        load_error_percent=error_percent(load_daily, load_actual) or 0.0,
        solar_mae_kwh=float(np.abs(solar_daily - solar_actual).mean()),
        solar_error_percent=error_percent(solar_daily, solar_actual),
        solar_physics_error_percent=error_percent(physics_daily, solar_actual),
        days=rows,
    )

    blocks = test.calendar.slot // LOAD_BLOCK_SLOTS
    residual = test.load_kwh - load_pred
    load_offsets = np.array(
        [np.percentile(residual[blocks == b], [10, 90]) for b in range(48 // LOAD_BLOCK_SLOTS)]
    )
    rated_half_hour_kwh = sum(a.kwp for a in system.arrays) * 0.5
    sunny = solar_pred >= MIN_SOLAR_SHARE_FOR_RATIO * rated_half_hour_kwh
    spread = Spread(
        load_interval=load_offsets,
        solar_interval=_quantile_ratios(
            test.solar_kwh[sunny], solar_pred[sunny], DEFAULT_SOLAR_INTERVAL
        ),
        load_day=_quantile_ratios(load_actual, load_daily, DEFAULT_LOAD_DAY),
        solar_day=_quantile_ratios(solar_actual, solar_daily, DEFAULT_SOLAR_DAY),
    )
    return result, spread


def day_half_hours(day: date, tz: str) -> list[datetime]:
    """UTC starts of each half hour of a local day (46, 48 or 50 around clock changes)."""
    zone = ZoneInfo(tz)
    start = datetime.combine(day, time(), zone).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time(), zone).astimezone(timezone.utc)
    return [start + INTERVAL * i for i in range(int((end - start) / INTERVAL))]


def forecast_weather(
    system: System, starts: list[datetime], client: OpenMeteoClient
) -> HalfHourWeather:
    hours = sorted({s.replace(minute=0) for s in starts})
    by_hour = {
        h.start: h
        for h in client.forecast(
            system.latitude, system.longitude, hours[0].date(), hours[-1].date()
        )
    }
    missing = [h for h in hours if h not in by_hour or by_hour[h].shortwave_radiation_wm2 is None]
    if missing:
        raise WeatherError(f"No weather forecast yet for {missing[0]:%Y-%m-%d %H:%M} UTC")
    half_hours = _half_hour_weather(system, [by_hour[h] for h in hours])
    wanted = set(starts)
    return half_hours.take(
        np.array([i for i, s in enumerate(half_hours.starts) if s in wanted], dtype=int)
    )


def _method(days: int, prior: Method) -> Method:
    if days == 0:
        return prior
    return "learned" if days >= LEARNED_DAYS else "blended"


class HistoryForecaster:
    def __init__(self, db: Session, weather_client: OpenMeteoClient) -> None:
        self.db = db
        self.weather_client = weather_client

    def forecast(self, system: System, day: date) -> DayForecast:
        history = load_history(self.db, system)
        history_days = len(set(history.calendar.local_day))
        models = fit(system, history)
        checked = backtest(system, history)
        spread = checked[1] if checked else Spread.default()

        starts = day_half_hours(day, system.timezone)
        weather = forecast_weather(system, starts, self.weather_client)
        calendar = Calendar.of(weather.starts, system.timezone)
        load, solar = predict(system, models, weather, calendar)

        if spread.load_interval is None:
            load_low = load * DEFAULT_LOAD_INTERVAL[0]
            load_high = load * DEFAULT_LOAD_INTERVAL[1]
        else:
            offsets = spread.load_interval[calendar.slot // LOAD_BLOCK_SLOTS]
            load_low = np.clip(load + offsets[:, 0], 0, None)
            load_high = load + offsets[:, 1]
        # The defaults already allow for weather forecast error.
        solar_ratio, solar_day = spread.solar_interval, spread.solar_day
        if checked:
            solar_ratio = _widen(solar_ratio, SOLAR_INTERVAL_FLOOR)
            solar_day = _widen(solar_day, SOLAR_DAY_FLOOR)

        intervals = [
            IntervalForecast(
                start=s,
                end=s + INTERVAL,
                load_kwh=round(float(load[i]), 4),
                solar_kwh=round(float(solar[i]), 4),
                load_low_kwh=round(float(load_low[i]), 4),
                load_high_kwh=round(float(load_high[i]), 4),
                solar_low_kwh=round(float(solar[i] * solar_ratio[0]), 4),
                solar_high_kwh=round(float(solar[i] * solar_ratio[1]), 4),
                temperature_c=float(weather.temperature_c[i]),
                cloud_cover_percent=float(weather.cloud_cover_percent[i]),
            )
            for i, s in enumerate(weather.starts)
        ]
        load_total, solar_total = float(load.sum()), float(solar.sum())
        return DayForecast(
            day=day,
            intervals=intervals,
            load=Range(
                load_total, load_total * spread.load_day[0], load_total * spread.load_day[1]
            ),
            solar=Range(solar_total, solar_total * solar_day[0], solar_total * solar_day[1]),
            load_method=_method(models.load.trained_days, "typical"),
            solar_method=_method(models.solar.trained_days, "physics"),
            history_days=history_days,
            array_performance=[round(float(m), 3) for m in models.solar.multipliers],
            backtest=checked[0] if checked else None,
        )
