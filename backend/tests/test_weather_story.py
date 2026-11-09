from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from app.forecasting.weather_story import (
    DayWeather,
    recent_daily_solar,
    recent_mean_temperature,
    tell,
)

START = datetime(2026, 11, 10, tzinfo=timezone.utc)
STARTS = [START + timedelta(minutes=30 * i) for i in range(48)]
DAY = (np.arange(48) >= 16) & (np.arange(48) < 32)  # 08:00 to 16:00


def day(cloud, temp=10.0, solar_per_slot=0.3, cold=None, panels=True):
    cloud = np.full(48, float(cloud)) if np.isscalar(cloud) else np.asarray(cloud, dtype=float)
    solar = np.where(DAY, solar_per_slot, 0.0)
    return DayWeather(
        starts=STARTS,
        cloud_percent=cloud,
        temperature_c=np.full(48, float(temp)),
        sun_wm2=np.where(DAY, 300.0, 0.0),
        solar_kwh=solar if panels else np.zeros(48),
        load_kwh=np.full(48, 0.15),
        cold_kwh=np.zeros(48) if cold is None else np.asarray(cold, dtype=float),
        has_panels=panels,
        solar_low_kwh=float(solar.sum()) * 0.6,
        solar_high_kwh=float(solar.sum()) * 1.3,
    )


def test_cloudy_day_below_recent_average():
    story = tell(day(85, solar_per_slot=0.2625), "UTC", recent_solar=7.0, recent_temp=10.0)
    assert story.headline == "Cloudy, 10°C"
    assert story.summary == "Cloudy skies put solar about 40% below your recent average."
    assert story.points[0].startswith("Solar: cloudy skies (85% cloud in daylight), so 4.2 kWh")
    assert "14-day average of 7.0 kWh" in story.points[0]
    assert "makes little difference" in story.points[1]
    assert story.recent_solar_kwh == 7.0


def test_sunny_day_about_average():
    story = tell(day(10, solar_per_slot=0.45), "UTC", recent_solar=7.0, recent_temp=None)
    assert story.headline.startswith("Sunny")
    assert story.summary == "Sunny skies should give about your usual solar."
    assert story.detail == ""


def test_cold_day_names_the_extra_usage_and_when():
    cold = np.where(np.arange(48) >= 34, 0.07, 0.0)  # 17:00 onwards, about 0.98 kWh
    story = tell(day(85, temp=4, cold=cold), "UTC", recent_solar=4.8, recent_temp=8.0)
    assert story.summary.endswith(", and the cold adds about 1.0 kWh to your usage.")
    usage = story.points[1]
    assert usage.startswith("Usage: an average of 4°C (recent days averaged 8°C) adds about 1.0")
    assert "in the evening" in usage
    assert story.cold_kwh == 0.98


def test_bright_spell_is_named():
    cloud = np.full(48, 90.0)
    cloud[26:30] = 30.0  # 13:00 to 15:00
    story = tell(day(cloud), "Europe/London", recent_solar=None, recent_temp=None)
    assert story.detail == "brighter 13:00–15:00"
    assert story.points[0].endswith("A brighter spell from 13:00 to 15:00 helps.")


def test_changeable_cloud_is_called_uncertain():
    cloud = np.where(np.arange(48) % 4 < 2, 10.0, 95.0)
    story = tell(day(cloud), "UTC", recent_solar=None, recent_temp=None)
    assert any(p.startswith("The cloud is changeable") for p in story.points)


def test_without_recent_days_it_gives_the_amount():
    story = tell(day(50, solar_per_slot=0.3), "UTC", recent_solar=None, recent_temp=None)
    assert story.summary == "Partly cloudy skies should give about 4.8 kWh of solar."


def test_without_panels_it_talks_only_about_usage():
    story = tell(day(50, panels=False), "UTC", recent_solar=None, recent_temp=None)
    assert not any(p.startswith("Solar") for p in story.points)
    assert story.solar_kwh is None
    assert story.summary.endswith("the temperature makes little difference to your usage.")


def test_recent_averages_skip_partial_days_and_the_day_itself():
    days = [date(2026, 11, d) for d in (8, 9, 10) for _ in range(48)][:-10]
    solar = np.full(len(days), 0.1)
    # The 8th and 9th are whole (4.8 kWh each); the 10th is partial and is the day itself.
    assert recent_daily_solar(days, solar, date(2026, 11, 10)) == pytest.approx(4.8)
    assert recent_daily_solar(days, solar, date(2026, 12, 30)) is None
    temps = np.array([5.0] * 48 + [7.0] * 48 + [20.0] * 38)
    assert recent_mean_temperature(days, temps, date(2026, 11, 10)) == 6.0
