"""Train on a system's history, check accuracy on recent days, and forecast a day ahead.

Training is a few small least-squares solves, so it runs on every request rather than being
stored; a forecast always reflects the latest import and the current system description.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import func, select
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
from app.forecasting.usage import (
    AWAY_DAY_RANGE,
    Adjusted,
    Marks,
    UsageProfile,
    adjust,
    car_load,
    detect_car_charging,
)
from app.forecasting.weather_story import (
    DayWeather,
    WeatherStory,
    recent_daily_solar,
    recent_mean_temperature,
    tell,
)
from app.importers.base import INTERVAL
from app.models import System, UsageExpectation, UsageLevel, UsageRecord, WeatherRecord
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
# The dull day's solar is never brighter than the darkest of this many recent days, which
# follow the season; the 10th percentile of a month's errors missed days like 5 November
# 2026 (0.52 kWh against a dull day of 0.8).
DARK_DAY_LOOKBACK_DAYS = 14
NOON_SLOT = 24
SUNSET_SOLAR_KWH = 0.01
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
    # Car charging found in the load (kWh), and half hours on days the user marked as other
    # than normal. Neither feeds the usage model; both still count for solar.
    car_kwh: np.ndarray | None = None
    marked: np.ndarray | None = None

    def __post_init__(self) -> None:
        if self.car_kwh is None:
            object.__setattr__(self, "car_kwh", np.zeros(len(self.load_kwh)))
        if self.marked is None:
            object.__setattr__(self, "marked", np.zeros(len(self.load_kwh), dtype=bool))

    def __len__(self) -> int:
        return len(self.load_kwh)

    @property
    def house_kwh(self) -> np.ndarray:
        """The load without car charging."""
        return self.load_kwh - self.car_kwh

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
            car_kwh=self.car_kwh[index],
            marked=self.marked[index],
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


def load_marks(db: Session, system: System) -> Marks:
    rows = db.scalars(
        select(UsageExpectation).where(UsageExpectation.system_id == system.id)
    ).all()
    return Marks(
        levels={r.day: r.level for r in rows if r.level is not UsageLevel.normal},
        car_kwh={r.day: r.car_kwh for r in rows if r.car_kwh},
    )


def load_history(db: Session, system: System, marks: Marks | None = None) -> History:
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
    calendar = Calendar.of(half_hours.starts, system.timezone)
    load = np.array([r.load_kwh for r in rows], dtype=float)
    marks = marks if marks is not None else load_marks(db, system)
    car = (
        detect_car_charging(half_hours.starts, calendar.slot, load, system.car_charger_kw)
        if system.has_electric_car
        else None
    )
    return History(
        weather=half_hours,
        calendar=calendar,
        load_kwh=load,
        solar_kwh=np.array([r.solar_kwh for r in rows], dtype=float),
        car_kwh=car,
        marked=marks.marked(calendar.local_day),
    )


def _heat_pump(system: System) -> bool:
    return bool(getattr(system, "has_heat_pump", False))


def usage_history(history: History) -> History:
    """The half hours the usage model learns from: days the user didn't mark."""
    return history.take(np.flatnonzero(~history.marked))


def fit(system: System, history: History) -> Models:
    days = len(set(history.calendar.local_day))
    usage = usage_history(history)
    heating = daily_heating_degrees(usage.calendar, usage.weather.temperature_c)
    load = LoadModel.fit(
        usage.calendar,
        heating,
        usage.house_kwh,
        newest_day_ordinal=int(history.day_ordinals().max()) if len(history) else None,
        heat_pump=_heat_pump(system),
    )
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


def darkest_recent_day(
    calendar: Calendar, solar_kwh: np.ndarray, days: int = DARK_DAY_LOOKBACK_DAYS
) -> float | None:
    """Solar on the darkest of the last `days` local days, so the dull day follows the season
    (late summer's sun can't flatter a November plan). A day counts once its readings run past
    noon and its last one has no solar, so a day imported while the sun is up isn't taken for
    a dark one."""
    if not len(solar_kwh):
        return None
    totals: dict[date, float] = {}
    last: dict[date, int] = {}
    for i, day in enumerate(calendar.local_day):
        totals[day] = totals.get(day, 0.0) + float(solar_kwh[i])
        last[day] = i
    latest = max(totals)
    done = [
        totals[d]
        for d in totals
        if (latest - d).days < days
        and calendar.slot[last[d]] >= NOON_SLOT
        and solar_kwh[last[d]] < SUNSET_SOLAR_KWH
    ]
    return min(done) if done else None


def _recent_solar(db: Session, system: System) -> tuple[Calendar, np.ndarray]:
    latest = db.scalar(
        select(func.max(UsageRecord.start)).where(UsageRecord.system_id == system.id)
    )
    if latest is None:
        return Calendar.of([], system.timezone), np.array([])
    records = db.scalars(
        select(UsageRecord)
        .where(
            UsageRecord.system_id == system.id,
            UsageRecord.start > latest - timedelta(days=DARK_DAY_LOOKBACK_DAYS + 1),
        )
        .order_by(UsageRecord.start)
    ).all()
    return (
        Calendar.of([_utc(r.start) for r in records], system.timezone),
        np.array([r.solar_kwh for r in records], dtype=float),
    )


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


def backtest(
    system: System, history: History, marks: Marks | None = None
) -> tuple[Backtest, Spread] | None:
    """Train on everything before the last few complete days, then predict those days.

    Days the user marked are still checked for solar, but left out of load accuracy, since the
    model alone isn't meant to predict them."""
    marks = marks or Marks()
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
    house = test.house_kwh
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
                load_actual_kwh=float(house[m].sum()),
                load_predicted_kwh=float(load_pred[m].sum()),
                solar_actual_kwh=float(test.solar_kwh[m].sum()),
                solar_predicted_kwh=float(solar_pred[m].sum()),
                solar_physics_kwh=float(physics_pred[m].sum()),
                usage=marks.level(date.fromordinal(int(d))).value,
            )
        )
    # Marked days aren't predicted by the model alone, so load accuracy leaves them out.
    normal = np.array(
        [not test.marked[test_ordinals == r.day.toordinal()].any() for r in rows], dtype=bool
    )
    if not normal.any():
        normal[:] = True
    load_actual = np.array([r.load_actual_kwh for r in rows])[normal]
    load_daily = np.array([r.load_predicted_kwh for r in rows])[normal]
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
    residual = house - load_pred
    keep = ~test.marked if (~test.marked).any() else np.ones(len(test), dtype=bool)
    load_offsets = np.array(
        [
            np.percentile(residual[keep & (blocks == b)], [10, 90])
            if (keep & (blocks == b)).any()
            else [0.0, 0.0]
            for b in range(48 // LOAD_BLOCK_SLOTS)
        ]
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

    def forecast(
        self, system: System, day: date, starts: list[datetime] | None = None
    ) -> DayForecast:
        """The forecast for local `day`, or for the half hours in `starts` if given (the planner
        needs a whole charge cycle, which crosses midnight). Totals cover the same half hours."""
        marks = load_marks(self.db, system)
        history = load_history(self.db, system, marks)
        history_days = len(set(history.calendar.local_day))
        models = fit(system, history)
        checked = backtest(system, history, marks)
        spread = checked[1] if checked else Spread.default()

        starts = starts or day_half_hours(day, system.timezone)
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
        expectation = _expected_usage(
            system, models, history, marks, weather, calendar, load, load_low, load_high
        )
        if expectation is not None:
            adjusted, notes = expectation
            load, load_low, load_high = adjusted.load, adjusted.low, adjusted.high
        else:
            adjusted, notes = None, []
        # The defaults already allow for weather forecast error.
        solar_ratio, solar_day = spread.solar_interval, spread.solar_day
        if checked:
            solar_ratio = _widen(solar_ratio, SOLAR_INTERVAL_FLOOR)
            solar_day = _widen(solar_day, SOLAR_DAY_FLOOR)
        # From the readings alone: the history above waits for weather, which can lag a day.
        darkest = darkest_recent_day(*_recent_solar(self.db, system))
        solar_expected = float(solar.sum())
        if darkest is not None and solar_expected > 0:
            solar_day = (min(solar_day[0], darkest / solar_expected), solar_day[1])

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
        if adjusted is None:
            load_range = Range(
                load_total, load_total * spread.load_day[0], load_total * spread.load_day[1]
            )
        else:
            # The car's charge is known; an empty house varies less than a lived-in one.
            away = np.array([lv is UsageLevel.away for lv in adjusted.levels], dtype=bool)
            house = load - adjusted.car_kwh
            car_total = float(adjusted.car_kwh.sum())
            lived, empty = float(house[~away].sum()), float(house[away].sum())
            load_range = Range(
                load_total,
                lived * spread.load_day[0] + empty * AWAY_DAY_RANGE[0] + car_total,
                lived * spread.load_day[1] + empty * AWAY_DAY_RANGE[1] + car_total,
            )
        story = self._weather_story(
            system, models, history, day, weather, calendar, load, solar, solar_ratio
        )
        return DayForecast(
            day=day,
            intervals=intervals,
            load=load_range,
            solar=Range(solar_total, solar_total * solar_day[0], solar_total * solar_day[1]),
            load_method=_method(models.load.trained_days, "typical"),
            solar_method=_method(models.solar.trained_days, "physics"),
            history_days=history_days,
            array_performance=[round(float(m), 3) for m in models.solar.multipliers],
            backtest=checked[0] if checked else None,
            usage_notes=notes,
            weather=story,
        )

    def _weather_story(
        self,
        system: System,
        models: Models,
        history: History,
        day: date,
        weather: HalfHourWeather,
        calendar: Calendar,
        load: np.ndarray,
        solar: np.ndarray,
        solar_ratio: tuple[float, float],
    ) -> WeatherStory | None:
        """What the weather does to `day` (just that local day, when the forecast covers a
        whole charge cycle), against the home's recent days."""
        index = np.array([i for i, d in enumerate(calendar.local_day) if d == day], dtype=int)
        if not len(index):
            return None
        heating = daily_heating_degrees(calendar, weather.temperature_c)
        cold = models.load.heating_part(calendar, heating)
        w = weather.take(index)
        recent_calendar, recent_solar = _recent_solar(self.db, system)
        return tell(
            DayWeather(
                starts=w.starts,
                cloud_percent=w.cloud_cover_percent,
                temperature_c=w.temperature_c,
                sun_wm2=w.ghi,
                solar_kwh=solar[index],
                load_kwh=load[index],
                cold_kwh=cold[index],
                has_panels=bool(system.arrays),
                solar_low_kwh=float(solar[index].sum() * solar_ratio[0]),
                solar_high_kwh=float(solar[index].sum() * solar_ratio[1]),
            ),
            system.timezone,
            recent_daily_solar(recent_calendar.local_day, recent_solar, day),
            recent_mean_temperature(
                history.calendar.local_day, history.weather.temperature_c, day
            ),
        )


def _clock(moment: datetime, tz: str) -> str:
    return moment.astimezone(ZoneInfo(tz)).strftime("%H:%M")


def planned_car_charges(
    system: System, marks: Marks, days: list[date]
) -> list[tuple[date, datetime, float]]:
    """(day, start, kWh) for each car charge in the cheap windows leading into `days` and the
    day after them: the charge starts as the window opens."""
    if not marks.car_kwh or system.tariff is None:
        return []
    from app.planner.simulator import cycle_for
    from app.tariffs import TariffSchedule

    book = TariffSchedule.from_system(system)
    wanted = sorted({d + timedelta(days=k) for d in days for k in (0, 1)})
    return [
        (d, cycle_for(d, book, system.timezone).night_start, marks.car_kwh[d])
        for d in wanted
        if d in marks.car_kwh
    ]


def usage_profile(system: System, models: Models, history: History) -> UsageProfile:
    """Base load and Low/High factors from the unmarked history, less any car charging and
    the heat pump's share."""
    usage = usage_history(history)
    if _heat_pump(system):
        past_heating = models.load.heating_part(
            usage.calendar, daily_heating_degrees(usage.calendar, usage.weather.temperature_c)
        )
    else:
        past_heating = np.zeros(len(usage))
    return UsageProfile.of(usage.calendar, usage.house_kwh, past_heating)


LEVEL_PHRASES = {
    UsageLevel.away: "away",
    UsageLevel.low: "a low-use day",
    UsageLevel.high: "a high-use day",
}


def _expected_usage(
    system: System,
    models: Models,
    history: History,
    marks: Marks,
    weather: HalfHourWeather,
    calendar: Calendar,
    load: np.ndarray,
    load_low: np.ndarray,
    load_high: np.ndarray,
) -> tuple[Adjusted, list[str]] | None:
    """The forecast load after the user's day marks and car charges, with notes explaining
    them; None when nothing applies, so the forecast is exactly the model's."""
    days = sorted(set(calendar.local_day))
    marked_days = [d for d in days if marks.level(d) is not UsageLevel.normal]
    charges = planned_car_charges(system, marks, days) if system.has_electric_car else []
    car = car_load(weather.starts, [(s, k) for _, s, k in charges], system.car_charger_kw)
    if not marked_days and not car.any():
        return None

    heating = daily_heating_degrees(calendar, weather.temperature_c)
    heat_pump = _heat_pump(system)
    heating_kwh = models.load.heating_part(calendar, heating) if heat_pump else np.zeros(len(load))
    profile = usage_profile(system, models, history)
    adjusted = adjust(load, load_low, load_high, heating_kwh, calendar, marks, profile, car)

    notes = []
    fixed = "" if profile.learned else " (a fixed allowance until there are 14 days of history)"
    for d in marked_days:
        level = marks.level(d)
        name = f"{d:%A} {d.day} {d:%B}"
        if level is UsageLevel.away:
            if profile.base_daily_kwh is None:
                basis = f"half the usual forecast{fixed}"
            else:
                basis = f"your {profile.base_daily_kwh:.1f} kWh a day base load"
                if heat_pump:
                    basis += ", plus the heat pump's share for the forecast temperatures"
            notes.append(f"You've marked {name} as away, so usage is based on {basis}.")
        else:
            factor = profile.factor(level)
            source = (
                "your quietest days against a typical one"
                if level is UsageLevel.low
                else "your busiest days against a typical one"
            )
            notes.append(
                f"You've marked {name} as {LEVEL_PHRASES[level]}, so usage is scaled by "
                f"{factor:.2f}" + (f" ({source})." if profile.learned else f"{fixed}.")
            )
    for _, begin, kwh in charges:
        if not car_load(weather.starts, [(begin, kwh)], system.car_charger_kw).any():
            continue
        hours = kwh / system.car_charger_kw if system.car_charger_kw > 0 else 0
        notes.append(
            f"The car takes {kwh:.0f} kWh from {_clock(begin, system.timezone)}, about "
            f"{hours:.1f} hours at {system.car_charger_kw:g} kW. The battery is held while the "
            f"cheap window is open, so the car charges from the grid, not the battery."
        )
    return adjusted, notes

