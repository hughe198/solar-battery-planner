"""Expected usage (Away, Low, Normal, High), electric car charging and heat pump settings."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import create_engine, inspect, text

from app.forecasting.model import Calendar, LoadModel, daily_heating_degrees, typical_profile
from app.forecasting.service import History, backtest, fit
from app.forecasting.usage import (
    FIXED_HIGH_FACTOR,
    FIXED_LOW_FACTOR,
    Marks,
    UsageProfile,
    adjust,
    car_load,
    detect_car_charging,
)
from app.migrations import add_big_load_settings
from app.models import UsageLevel
from tests.conftest import register
from tests.test_forecast_api import tomorrow, upload_days, weather  # noqa: F401 (fixture)
from tests.test_forecast_model import SYSTEM, history, synthetic_weather
from tests.test_system import SYSTEM as SYSTEM_IN

START = datetime(2024, 3, 1, tzinfo=timezone.utc)


def starts(n, first=START):
    return [first + timedelta(minutes=30 * i) for i in range(n)]


def test_finds_car_sessions_but_not_a_single_spike():
    n = 48 * 10
    times = starts(n)
    slot = np.array([(t.hour * 2 + t.minute // 30) for t in times])
    load = np.full(n, 0.2)
    # A 7 kW car for two and a half hours on day 3, half an hour partial at each end.
    load[48 * 3 + 2 : 48 * 3 + 7] += 3.5
    load[48 * 3 + 1] += 1.5
    load[48 * 3 + 7] += 1.0
    # An oven and shower for a single half hour on day 5.
    load[48 * 5 + 36] += 3.0
    car = detect_car_charging(times, slot, load, 7.0)
    assert car.sum() == pytest.approx(5 * 3.5 + 1.5 + 1.0)
    assert car[48 * 5 + 36] == 0
    # A granny charger isn't mistaken for a 7 kW one, and vice versa.
    assert detect_car_charging(times, slot, load, 22.0).sum() == 0


def test_car_charging_is_kept_out_of_the_usage_model():
    clean = history(40)
    car = clean.load_kwh.copy()
    for d in range(0, 40, 3):  # every third night, 01:00 to 04:00 at 7 kW
        car[48 * d + 2 : 48 * d + 8] += 3.5
    with_car = History(clean.weather, clean.calendar, car, clean.solar_kwh)
    times = clean.weather.starts
    found = detect_car_charging(times, clean.calendar.slot, car, 7.0)
    cleaned = History(clean.weather, clean.calendar, car, clean.solar_kwh, car_kwh=found)
    truth = fit(SYSTEM, clean).load.coef[:48]
    assert fit(SYSTEM, cleaned).load.coef[:48] == pytest.approx(truth, abs=0.05)
    # Without the setting the night slots are dragged up by the car.
    assert fit(SYSTEM, with_car).load.coef[2:8].mean() > truth[2:8].mean() + 0.5


def test_marked_days_are_left_out_of_usage_but_not_solar():
    past = history(50)
    days = sorted(set(past.calendar.local_day))
    marks = Marks(levels={d: UsageLevel.away for d in days[30:37]})
    mask = marks.marked(past.calendar.local_day)
    away = past.load_kwh.copy()
    away[mask] = 0.05
    marked = History(past.weather, past.calendar, away, past.solar_kwh, marked=mask)
    unmarked = History(past.weather, past.calendar, away, past.solar_kwh)
    keep = np.flatnonzero(~mask)
    expected = fit(SYSTEM, past.take(keep)).load.coef
    assert fit(SYSTEM, marked).load.coef == pytest.approx(expected, abs=1e-9)
    # Left in, a week away drags normal forecasts down.
    assert fit(SYSTEM, unmarked).load.coef[:96].sum() < expected[:96].sum() - 0.3
    # Solar still learns from those days.
    assert fit(SYSTEM, marked).solar.trained_days == len(days)


def test_backtest_leaves_marked_days_out_of_load_accuracy():
    past = history(60)
    load = past.load_kwh.copy()
    last = sorted(set(past.calendar.local_day))[-4:-1]  # the last local day is partial
    marks = Marks(levels={d: UsageLevel.away for d in last})
    mask = marks.marked(past.calendar.local_day)
    load[mask] = 0.05
    result, _ = backtest(
        SYSTEM, History(past.weather, past.calendar, load, past.solar_kwh, marked=mask), marks
    )
    assert [d.usage for d in result.days[-3:]] == ["away"] * 3
    assert all(d.usage == "normal" for d in result.days[:-3])
    assert result.load_error_percent < 10


def test_heat_pump_learns_the_cold_weather_response():
    weather = synthetic_weather(60)
    cal = Calendar.of(weather.starts, "Europe/London")
    heating = daily_heating_degrees(cal, weather.temperature_c)
    load = typical_profile(8)[cal.slot] + 0.06 * heating
    with_hp = LoadModel.fit(cal, heating, load, heat_pump=True)
    without = LoadModel.fit(cal, heating, load)
    assert with_hp.coef[96:] == pytest.approx(np.full(4, 0.06), abs=0.01)
    assert np.all(without.coef[96:] < with_hp.coef[96:])
    assert with_hp.heating_part(cal, heating) == pytest.approx(0.06 * heating, rel=0.15)


def test_profile_uses_fixed_values_on_short_history():
    past = history(10)
    profile = UsageProfile.of(past.calendar, past.load_kwh, np.zeros(len(past)))
    assert not profile.learned
    assert profile.base_kwh_per_half_hour is None
    assert (profile.low_factor, profile.high_factor) == (FIXED_LOW_FACTOR, FIXED_HIGH_FACTOR)


def test_profile_learns_base_load_and_factors():
    past = history(40)
    profile = UsageProfile.of(past.calendar, past.load_kwh, np.zeros(len(past)))
    assert profile.learned
    night = typical_profile(10)[1:10].mean()
    assert profile.base_kwh_per_half_hour == pytest.approx(night, rel=0.1)
    assert profile.low_factor < 1 < profile.high_factor


def test_away_crosses_midnight_by_local_date():
    # 02:00 Saturday to 02:00 Monday local (UTC in summer is an hour behind).
    first = datetime(2024, 6, 1, 1, tzinfo=timezone.utc)
    times = starts(96, first)
    cal = Calendar.of(times, "Europe/London")
    sunday = date(2024, 6, 2)
    load = np.full(96, 0.4)
    profile = UsageProfile(0.1, 0.7, 1.35, True)
    marks = Marks(levels={sunday: UsageLevel.away})
    out = adjust(load, load * 0.8, load * 1.2, np.zeros(96), cal, marks, profile, np.zeros(96))
    for i, d in enumerate(cal.local_day):
        if d == sunday:
            assert out.load[i] == pytest.approx(0.1)
            assert out.low[i] < out.load[i] < out.high[i]
        else:
            assert out.load[i] == pytest.approx(0.4)


def test_away_keeps_the_heat_pump_share_and_low_high_scale():
    times = starts(48)
    cal = Calendar.of(times, "UTC")
    day = cal.local_day[0]
    load = np.full(48, 0.5)
    heating = np.full(48, 0.2)
    profile = UsageProfile(0.1, 0.7, 1.35, True)
    away = adjust(load, load, load, heating, cal, Marks({day: UsageLevel.away}), profile, np.zeros(48))
    assert away.load == pytest.approx(np.full(48, 0.3))
    low = adjust(load, load, load, heating, cal, Marks({day: UsageLevel.low}), profile, np.zeros(48))
    assert low.load == pytest.approx(np.full(48, 0.35))
    fixed = UsageProfile.fixed()
    away = adjust(load, load, load, heating, cal, Marks({day: UsageLevel.away}), fixed, np.zeros(48))
    assert away.load == pytest.approx(np.full(48, 0.25))


def test_car_load_draws_at_charger_power_from_its_start():
    times = starts(10)
    out = car_load(times, [(times[2], 8.0)], 7.0)
    assert list(out) == pytest.approx([0, 0, 3.5, 3.5, 1.0, 0, 0, 0, 0, 0])


def test_migration_adds_big_load_columns_once():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE systems (id INTEGER PRIMARY KEY)"))
        conn.execute(text("INSERT INTO systems (id) VALUES (1)"))
    add_big_load_settings(engine)
    add_big_load_settings(engine)
    names = {c["name"] for c in inspect(engine).get_columns("systems")}
    assert {"has_heat_pump", "has_electric_car", "car_charger_kw"} <= names
    with engine.connect() as conn:
        row = conn.execute(text("SELECT has_heat_pump, car_charger_kw FROM systems")).one()
    assert (bool(row[0]), row[1]) == (False, 7)


# API ----------------------------------------------------------------------------------------


@pytest.fixture
def headers(client, weather):  # noqa: F811
    h = register(client)
    assert client.put("/api/system", json=SYSTEM_IN, headers=h).status_code == 200
    return h


def set_settings(client, headers, **changes):
    resp = client.put("/api/system", json={**SYSTEM_IN, **changes}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_settings_default_off_and_save(client, headers):
    body = client.get("/api/system", headers=headers).json()
    assert (body["has_heat_pump"], body["has_electric_car"], body["car_charger_kw"]) == (
        False,
        False,
        7.0,
    )
    body = set_settings(client, headers, has_electric_car=True, car_charger_kw=3.6)
    assert body["has_electric_car"] and body["car_charger_kw"] == 3.6


def test_mark_days_and_ranges(client, headers):
    day = tomorrow()
    resp = client.put(f"/api/usage-expectations/{day}", json={"level": "high"}, headers=headers)
    assert resp.status_code == 200, resp.text
    resp = client.put(
        "/api/usage-expectations",
        json={"start": str(day + timedelta(days=3)), "end": str(day + timedelta(days=12)), "level": "away"},
        headers=headers,
    )
    assert resp.status_code == 200 and len(resp.json()) == 10
    # A past holiday can be marked too.
    past = day - timedelta(days=40)
    assert (
        client.put(f"/api/usage-expectations/{past}", json={"level": "away"}, headers=headers)
        .status_code
        == 200
    )
    body = client.get("/api/usage-expectations", headers=headers).json()
    assert [d["level"] for d in body["days"]] == ["away"] + ["high"] + ["away"] * 10
    assert body["profile"]["learned"] is False
    # Back to normal clears it.
    client.put(f"/api/usage-expectations/{day}", json={"level": "normal"}, headers=headers)
    body = client.get("/api/usage-expectations", headers=headers).json()
    assert len(body["days"]) == 11
    far = day + timedelta(days=400)
    assert (
        client.put(f"/api/usage-expectations/{far}", json={"level": "away"}, headers=headers)
        .status_code
        == 422
    )


def test_car_charge_needs_the_setting(client, headers):
    day = tomorrow()
    url = f"/api/usage-expectations/{day}"
    assert client.put(url, json={"car_kwh": 20}, headers=headers).status_code == 422
    set_settings(client, headers, has_electric_car=True)
    resp = client.put(url, json={"car_kwh": 20}, headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"day": str(day), "level": "normal", "car_kwh": 20}


def plan(client, headers):
    resp = client.get(f"/api/plan?day={tomorrow()}&start_soc_percent=20", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_away_lowers_the_plan_and_explains_it(client, headers):
    upload_days(client, headers, date(2024, 5, 1), 30)
    normal = plan(client, headers)
    client.put(f"/api/usage-expectations/{tomorrow()}", json={"level": "away"}, headers=headers)
    away = plan(client, headers)
    assert away["load"]["expected"] < normal["load"]["expected"] - 1
    assert away["recommendations"][0]["target_soc_percent"] <= normal["recommendations"][0][
        "target_soc_percent"
    ]
    assert any("as away" in n and "base load" in n for n in away["usage_notes"])
    # An empty house has a narrow likely range.
    assert away["load"]["high"] <= away["load"]["expected"] * 1.15 + 0.01


def test_car_night_adds_load_in_the_cheap_window(client, headers):
    upload_days(client, headers, date(2024, 5, 1), 30)
    set_settings(client, headers, has_electric_car=True)
    normal = plan(client, headers)
    client.put(f"/api/usage-expectations/{tomorrow()}", json={"car_kwh": 14}, headers=headers)
    car = plan(client, headers)
    assert car["load"]["expected"] == pytest.approx(normal["load"]["expected"] + 14, abs=0.01)
    window = [p for p in car["timeline"] if p["cheap"]]
    before = [p for p in normal["timeline"] if p["cheap"]]
    added = sum(p["load_kwh"] for p in window) - sum(p["load_kwh"] for p in before)
    assert added == pytest.approx(14, abs=0.01)
    # The day after the window is untouched, so the charge target doesn't change.
    assert car["recommendations"][0]["target_soc_percent"] == normal["recommendations"][0][
        "target_soc_percent"
    ]
    assert any("car takes 14 kWh" in n for n in car["usage_notes"])


def test_nothing_marked_and_settings_off_leaves_the_plan_unchanged(client, headers):
    upload_days(client, headers, date(2024, 5, 1), 30)
    before = plan(client, headers)
    day = tomorrow()
    client.put(f"/api/usage-expectations/{day}", json={"level": "away"}, headers=headers)
    client.put(f"/api/usage-expectations/{day}", json={"level": "normal"}, headers=headers)
    # Marks on other days don't touch this one.
    client.put(
        f"/api/usage-expectations/{day + timedelta(days=5)}", json={"level": "high"}, headers=headers
    )
    after = plan(client, headers)
    assert after == before
    assert after["usage_notes"] == []
