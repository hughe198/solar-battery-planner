from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.api.imports import get_weather_client
from app.main import app
from tests.conftest import register
from tests.test_imports_api import CSV_HEADER, FakeWeather
from tests.test_system import SYSTEM


class FakeForecastWeather(FakeWeather):
    def forecast(self, lat, lon, start, end):
        return self.hourly(lat, lon, start, end)


@pytest.fixture
def weather():
    fake = FakeForecastWeather()
    app.dependency_overrides[get_weather_client] = lambda: fake
    yield fake


@pytest.fixture
def headers(client, weather):
    h = register(client)
    assert client.put("/api/system", json=SYSTEM, headers=h).status_code == 200
    return h


def tomorrow():
    return datetime.now(ZoneInfo("Europe/London")).date() + timedelta(days=1)


def upload_days(client, headers, first: date, days: int):
    rows = []
    for d in range(days):
        day = first + timedelta(days=d)
        for slot in range(48):
            load = 0.5 if 34 <= slot < 42 else 0.15
            solar = 0.6 if 18 <= slot < 34 else 0.0
            rows.append(f"{day}T{slot // 2:02d}:{slot % 2 * 30:02d},{load},{solar},0,0\n")
    resp = client.post(
        "/api/import/csv", files={"file": ("u.csv", CSV_HEADER + "".join(rows))}, headers=headers
    )
    assert resp.status_code == 200, resp.text


def test_forecast_without_history_uses_priors(client, headers):
    resp = client.get("/api/forecast", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["day"] == tomorrow().isoformat()
    assert body["load_method"] == "typical"
    assert body["solar_method"] == "physics"
    assert body["history_days"] == 0
    assert body["backtest"] is None
    assert len(body["intervals"]) in (46, 48, 50)
    first = body["intervals"][0]
    assert first["solar_kwh"] == 0  # local midnight
    assert first["load_low_kwh"] <= first["load_kwh"] <= first["load_high_kwh"]
    assert body["load"]["expected"] == pytest.approx(2700 / 365, rel=0.01)
    assert body["load"]["low"] < body["load"]["expected"] < body["load"]["high"]
    assert body["solar"]["expected"] > 0


def test_forecast_learns_from_history_and_reports_accuracy(client, headers):
    upload_days(client, headers, date(2024, 5, 1), 30)
    accuracy = client.get("/api/forecast/accuracy", headers=headers).json()
    assert accuracy["history_days"] == 30
    assert accuracy["backtest"]["test_days"] == 7
    assert accuracy["backtest"]["load_error_percent"] < 5

    body = client.get("/api/forecast", headers=headers).json()
    assert body["load_method"] == "learned"
    assert body["solar_method"] == "learned"
    assert body["backtest"]["train_days"] == 23
    # Every day used 10 kWh with an evening peak; the forecast should say the same.
    assert body["load"]["expected"] == pytest.approx(10, rel=0.05)
    evening = [i for i in body["intervals"] if i["load_kwh"] > 0.4]
    assert 6 <= len(evening) <= 10
    assert len(body["array_performance"]) == 2


def test_forecast_day_must_be_soon(client, headers):
    too_far = tomorrow() + timedelta(days=8)
    assert client.get(f"/api/forecast?day={too_far}", headers=headers).status_code == 422
    past = tomorrow() - timedelta(days=2)
    assert client.get(f"/api/forecast?day={past}", headers=headers).status_code == 422


def test_forecast_weather_outage(client, headers):
    app.dependency_overrides[get_weather_client] = lambda: FakeForecastWeather(fail=True)
    resp = client.get("/api/forecast", headers=headers)
    assert resp.status_code == 502


def test_needs_a_system(client, weather):
    h = register(client)
    assert client.get("/api/forecast", headers=h).status_code == 404
