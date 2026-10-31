from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from app.forecasting.model import Calendar, LoadModel, SolarModel, typical_profile
from app.forecasting.service import History, backtest, day_half_hours, fit
from app.forecasting.solar import (
    DEFAULT_PERFORMANCE_RATIO,
    HalfHourWeather,
    Orientation,
    array_features,
    split_hours,
    sun_position,
)

LAT, LON = 51.5, -0.12
ARRAYS = [Orientation(4.0, 35, 180), Orientation(2.0, 30, 90)]
SYSTEM = SimpleNamespace(
    latitude=LAT,
    longitude=LON,
    timezone="Europe/London",
    arrays=[
        SimpleNamespace(kwp=a.kwp, tilt_degrees=a.tilt_degrees, azimuth_degrees=a.azimuth_degrees)
        for a in ARRAYS
    ],
)


def elevation_and_azimuth(moment):
    cos_z, az = sun_position(np.array([moment.timestamp()]), LAT, LON)
    return 90 - np.degrees(np.arccos(cos_z[0])), az[0]


def test_sun_position_london_midsummer():
    # Solar noon in London is about 12:00 UTC; the sun is ~62° up and due south.
    elevation, azimuth = elevation_and_azimuth(datetime(2024, 6, 21, 12, 0, tzinfo=timezone.utc))
    assert elevation == pytest.approx(62, abs=1)
    assert azimuth == pytest.approx(180, abs=3)
    # Early evening sun is in the west.
    elevation, azimuth = elevation_and_azimuth(datetime(2024, 6, 21, 17, 0, tzinfo=timezone.utc))
    assert 25 < elevation < 35
    assert 265 < azimuth < 285
    elevation, _ = elevation_and_azimuth(datetime(2024, 12, 21, 12, 0, tzinfo=timezone.utc))
    assert elevation == pytest.approx(15, abs=1)


def synthetic_weather(days, start=datetime(2024, 3, 1, tzinfo=timezone.utc), seed=0):
    rng = np.random.default_rng(seed)
    hours = [start + timedelta(hours=i) for i in range(24 * days)]
    cos_z, _ = sun_position(np.array([h.timestamp() + 1800 for h in hours]), LAT, LON)
    cloud = np.repeat(rng.uniform(0, 100, days), 24)
    ghi = np.clip(cos_z, 0, None) * 900 * (1 - 0.7 * cloud / 100)
    dhi = ghi * (0.2 + 0.6 * cloud / 100)
    temp = np.repeat(rng.uniform(0, 18, days), 24)
    return split_hours(hours, list(ghi), list(dhi), list(temp), list(cloud), LAT, LON)


def test_split_hours_keeps_hourly_energy_and_darkness():
    weather = synthetic_weather(2)
    hourly = weather.ghi.reshape(-1, 2).mean(axis=1)
    assert np.all(weather.ghi >= 0)
    # Halves average back to the hour, and midnight is dark.
    assert hourly.sum() > 0
    assert weather.ghi[0] == 0


def test_physics_estimate_is_plausible():
    day = synthetic_weather(1, start=datetime(2024, 6, 21, tzinfo=timezone.utc))
    weather = HalfHourWeather(
        day.starts,
        ghi=day.ghi / day.ghi.max() * 850,
        dhi=day.ghi / day.ghi.max() * 100,
        temperature_c=np.full(48, 15.0),
        cloud_cover_percent=np.zeros(48),
    )
    ideal = array_features(weather, ARRAYS, LAT, LON)
    # Clear midsummer day: a south 4 kWp array peaks near its rating and is dark at night.
    assert 1.4 < ideal[:, 0].max() < 2.2  # kWh per half hour
    assert ideal[0, 0] == 0
    # The east array peaks in the morning.
    assert np.argmax(ideal[:, 1]) < np.argmax(ideal[:, 0])


def history(days, multipliers=(0.7, 0.9), seed=0):
    rng = np.random.default_rng(seed)
    weather = synthetic_weather(days, seed=seed)
    cal = Calendar.of(weather.starts, "Europe/London")
    solar = array_features(weather, ARRAYS, LAT, LON) @ np.array(multipliers)
    solar *= rng.lognormal(0, 0.1, len(solar))
    load = typical_profile(10)[cal.slot] * (1 + 0.3 * cal.weekend)
    load += rng.normal(0, 0.03, len(load))
    return History(weather, cal, np.clip(load, 0.01, None), solar)


def test_learns_array_performance_and_load_shape():
    past = history(60)
    models = fit(SYSTEM, past)
    # East and south arrays overlap a lot, so the split is approximate but the total is not.
    assert models.solar.multipliers == pytest.approx([0.7, 0.9], abs=0.1)
    ideal = array_features(past.weather, ARRAYS, LAT, LON)
    assert models.solar.predict(ideal).sum() == pytest.approx(past.solar_kwh.sum(), rel=0.03)
    weekday = models.load.coef[:48]
    assert weekday == pytest.approx(typical_profile(10), abs=0.03)
    assert models.load.coef[48:96].sum() == pytest.approx(13, rel=0.05)


def test_thin_history_stays_near_the_prior():
    models = fit(SYSTEM, history(60).take(np.arange(48)))
    assert models.solar.trained_days == 1
    assert np.all(np.abs(models.solar.multipliers - DEFAULT_PERFORMANCE_RATIO) < 0.15)


def test_no_history_uses_typical_profile_and_physics():
    empty = history(1).take(np.array([], dtype=int))
    models = fit(SYSTEM, empty)
    assert models.load.trained_days == 0
    assert models.load.coef[:48].sum() == pytest.approx(2700 / 365)
    assert list(models.solar.multipliers) == [DEFAULT_PERFORMANCE_RATIO] * 2


def test_backtest_needs_three_weeks():
    assert backtest(SYSTEM, history(20)) is None


def test_backtest_reports_accuracy_and_beats_physics_alone():
    result, spread = backtest(SYSTEM, history(60))
    assert result.test_days == 15
    assert result.train_days == 45
    assert len(result.days) == 15
    assert result.load_error_percent < 10
    assert result.solar_error_percent < result.solar_physics_error_percent
    low, high = spread.solar_interval
    assert low < 1 < high
    assert spread.load_interval.shape == (6, 2)


def test_day_half_hours_handles_clock_changes():
    assert len(day_half_hours(date(2024, 3, 31), "Europe/London")) == 46
    assert len(day_half_hours(date(2024, 10, 27), "Europe/London")) == 50
    starts = day_half_hours(date(2024, 6, 1), "Europe/London")
    assert starts[0] == datetime(2024, 5, 31, 23, tzinfo=timezone.utc)


def test_load_heating_term_raises_cold_day_usage():
    cal = Calendar.of(synthetic_weather(1).starts, "Europe/London")
    model = LoadModel(np.concatenate([np.full(96, 0.2), [0.0, 0.05, 0.05, 0.05]]), 30)
    warm = model.predict(cal, np.zeros(48)).sum()
    cold = model.predict(cal, np.full(48, 10.0)).sum()
    assert cold == pytest.approx(warm + 36 * 0.5)


def test_solar_model_never_predicts_negative():
    model = SolarModel(np.array([0.8, 0.0]), 10)
    assert np.all(model.predict(np.array([[0.0, 1.0], [1.0, 1.0]])) >= 0)
