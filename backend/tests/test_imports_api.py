from datetime import date, datetime, timedelta, timezone

import pytest

from app.api.imports import get_fox_client_factory, get_weather_client
from app.importers.fox_ess import FoxEssAuthError
from app.main import app
from app.weather.open_meteo import HourlyWeather, WeatherError
from tests.conftest import register
from tests.test_system import SYSTEM

UTC_SYSTEM = {**SYSTEM, "timezone": "UTC"}
CSV_HEADER = "start,load_kwh,solar_kwh,grid_import_kwh,grid_export_kwh\n"


class FakeFox:
    calls: list[tuple[datetime, datetime]] = []

    def __init__(self, api_key):
        self.raise_auth = api_key == "bad-key-123"

    def device_serials(self):
        if self.raise_auth:
            raise FoxEssAuthError("nope")
        return ["INV0001"]

    def battery_min_soc(self, sn):
        return 5.0

    def force_charge_power_kw(self, sn):
        return 3.6

    def history(self, sn, begin, end):
        FakeFox.calls.append((begin, end))
        points, moment = [], begin
        while moment < end:
            points.append({"time": moment.strftime("%Y-%m-%d %H:%M:%S GMT+0000"), "value": 1.2})
            moment += timedelta(minutes=5)
        return [
            {"variable": v, "data": points}
            for v in ("loadsPower", "pvPower", "gridConsumptionPower", "feedinPower", "SoC")
        ]


class FakeWeather:
    def __init__(self, fail=False):
        self.fail = fail
        self.requests = []

    def hourly(self, lat, lon, start, end):
        if self.fail:
            raise WeatherError("down")
        self.requests.append((start, end))
        moment = datetime.combine(start, datetime.min.time(), timezone.utc)
        rows = []
        while moment.date() <= end:
            rows.append(HourlyWeather(moment, 12.0, 80.0, 150.0, 90.0, 60.0))
            moment += timedelta(hours=1)
        return rows


@pytest.fixture
def weather():
    fake = FakeWeather()
    app.dependency_overrides[get_weather_client] = lambda: fake
    yield fake


@pytest.fixture
def headers(client, weather):
    FakeFox.calls = []
    app.dependency_overrides[get_fox_client_factory] = lambda: FakeFox
    h = register(client)
    assert client.put("/api/system", json=UTC_SYSTEM, headers=h).status_code == 200
    return h


def connect(client, headers, key="good-key-123", sn="inv0001"):
    return client.put(
        "/api/import/fox-ess", json={"api_key": key, "device_sn": sn}, headers=headers
    )


def test_needs_a_system(client, weather):
    h = register(client)
    assert client.get("/api/import/status", headers=h).status_code == 404


def test_connect_validates_key_and_serial(client, headers):
    assert connect(client, headers, key="bad-key-123").status_code == 400
    assert connect(client, headers, sn="OTHER").status_code == 400
    resp = connect(client, headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["api_key_hint"] == "-123"
    assert "api_key" not in resp.json()
    assert resp.json()["battery_min_soc_percent"] == 5.0
    system = client.get("/api/system", headers=headers).json()
    assert system["data_source"] == "fox_ess"
    assert system["battery"]["min_soc_percent"] == 5.0  # read from the inverter
    assert system["battery"]["max_charge_kw"] == 3.6
    assert resp.json()["battery_max_charge_kw"] == 3.6
    status = client.get("/api/import/status", headers=headers).json()
    assert status["fox_ess"]["device_sn"] == "inv0001"


def test_key_is_stored_encrypted(client, headers):
    from app.database import get_session_factory
    from app.models import FoxEssConnection

    connect(client, headers)
    with app.dependency_overrides[get_session_factory]()() as db:
        stored = db.query(FoxEssConnection).one().api_key_encrypted
    assert "good-key-123" not in stored


def test_sync_imports_days_and_matches_weather(client, headers, weather):
    connect(client, headers)
    body = {"start_date": "2024-06-01", "end_date": "2024-06-02"}
    resp = client.post("/api/import/fox-ess/sync", json=body, headers=headers)
    assert resp.status_code == 202, resp.text

    status = client.get("/api/import/status", headers=headers).json()
    job = status["latest_job"]
    assert job["status"] == "done", job
    assert job["days_done"] == 2
    assert job["intervals_imported"] == 96
    assert status["usage"]["count"] == 96
    assert status["weather"]["count"] == 48
    assert status["matched_intervals"] == 96
    assert weather.requests == [(date(2024, 6, 1), date(2024, 6, 2))]
    # Newest day first, one call per day.
    assert [c[0].day for c in FakeFox.calls] == [2, 1]

    rows = client.get(
        "/api/import/intervals",
        params={"start": "2024-06-01", "end": "2024-06-01"},
        headers=headers,
    ).json()
    assert len(rows) == 48
    assert rows[0]["start"].startswith("2024-06-01T00:00:00")
    assert rows[0]["load_kwh"] == 0.6
    assert rows[0]["weather"]["shortwave_radiation_wm2"] == 150.0

    # A second sync skips complete days instead of spending API quota.
    FakeFox.calls = []
    client.post("/api/import/fox-ess/sync", json=body, headers=headers)
    job = client.get("/api/import/status", headers=headers).json()["latest_job"]
    assert job["days_skipped"] == 2
    assert FakeFox.calls == []


def test_sync_validation(client, headers):
    body = {"start_date": "2024-06-01", "end_date": "2024-06-02"}
    assert client.post("/api/import/fox-ess/sync", json=body, headers=headers).status_code == 400
    connect(client, headers)
    future = (date.today() + timedelta(days=2)).isoformat()
    for bad in (
        {"start_date": "2024-06-02", "end_date": "2024-06-01"},
        {"start_date": "2019-12-31", "end_date": "2020-01-01"},
        {"start_date": "2023-01-01", "end_date": "2024-06-01"},
        {"start_date": "2024-06-01", "end_date": future},
    ):
        assert client.post("/api/import/fox-ess/sync", json=bad, headers=headers).status_code == 422


def test_failed_fox_call_fails_the_job(client, headers):
    connect(client, headers)

    class Broken(FakeFox):
        def history(self, sn, begin, end):
            raise FoxEssAuthError("Fox ESS rejected the API key")

    app.dependency_overrides[get_fox_client_factory] = lambda: Broken
    body = {"start_date": "2024-06-01", "end_date": "2024-06-01"}
    client.post("/api/import/fox-ess/sync", json=body, headers=headers)
    job = client.get("/api/import/status", headers=headers).json()["latest_job"]
    assert job["status"] == "failed"
    assert "rejected" in job["message"]


def test_disconnect_keeps_data(client, headers):
    connect(client, headers)
    client.post(
        "/api/import/fox-ess/sync",
        json={"start_date": "2024-06-01", "end_date": "2024-06-01"},
        headers=headers,
    )
    assert client.delete("/api/import/fox-ess", headers=headers).status_code == 204
    status = client.get("/api/import/status", headers=headers).json()
    assert status["fox_ess"] is None
    assert status["usage"]["count"] == 48


def test_csv_upload(client, headers, weather):
    rows = "".join(
        f"2024-06-01T{h:02d}:{m:02d},0.25,0.1,0.15,0\n" for h in range(2) for m in (0, 30)
    )
    resp = client.post(
        "/api/import/csv",
        files={"file": ("usage.csv", CSV_HEADER + rows, "text/csv")},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"intervals_imported": 4, "weather_hours_added": 24, "message": None}
    assert client.get("/api/system", headers=headers).json()["data_source"] == "csv"
    assert client.get("/api/import/status", headers=headers).json()["matched_intervals"] == 4

    # Re-uploading replaces rather than duplicates, and weather is not fetched again.
    client.post(
        "/api/import/csv", files={"file": ("usage.csv", CSV_HEADER + rows)}, headers=headers
    )
    assert client.get("/api/import/status", headers=headers).json()["usage"]["count"] == 4
    assert len(weather.requests) == 1


def test_csv_upload_errors(client, headers):
    resp = client.post(
        "/api/import/csv", files={"file": ("x.csv", "start,load_kwh\n")}, headers=headers
    )
    assert resp.status_code == 422
    assert "missing column" in resp.json()["detail"]


def test_csv_upload_survives_weather_outage(client, headers):
    app.dependency_overrides[get_weather_client] = lambda: FakeWeather(fail=True)
    resp = client.post(
        "/api/import/csv",
        files={"file": ("u.csv", CSV_HEADER + "2024-06-01T00:00,1,0,1,0\n")},
        headers=headers,
    )
    assert resp.status_code == 200
    assert "weather could not be fetched" in resp.json()["message"]
    assert client.get("/api/import/status", headers=headers).json()["usage"]["count"] == 1
    assert client.post("/api/import/weather", headers=headers).status_code == 502

    app.dependency_overrides[get_weather_client] = lambda: FakeWeather()
    resp = client.post("/api/import/weather", headers=headers)
    assert resp.json()["weather_hours_added"] == 24
    assert client.get("/api/import/status", headers=headers).json()["matched_intervals"] == 1


def test_moving_the_system_clears_weather(client, headers):
    client.post(
        "/api/import/csv",
        files={"file": ("u.csv", CSV_HEADER + "2024-06-01T00:00,1,0,1,0\n")},
        headers=headers,
    )
    client.put("/api/system", json={**UTC_SYSTEM, "name": "Renamed"}, headers=headers)
    assert client.get("/api/import/status", headers=headers).json()["weather"]["count"] == 24
    client.put("/api/system", json={**UTC_SYSTEM, "latitude": 52.0}, headers=headers)
    assert client.get("/api/import/status", headers=headers).json()["weather"]["count"] == 0


def test_data_is_per_user(client, headers):
    client.post(
        "/api/import/csv",
        files={"file": ("u.csv", CSV_HEADER + "2024-06-01T00:00,1,0,1,0\n")},
        headers=headers,
    )
    other = register(client, email="other@example.com")
    client.put("/api/system", json=UTC_SYSTEM, headers=other)
    assert client.get("/api/import/status", headers=other).json()["usage"]["count"] == 0


def test_delete_imported_data(client, headers):
    connect(client, headers)
    client.post(
        "/api/import/fox-ess/sync",
        json={"start_date": "2024-06-01", "end_date": "2024-06-01"},
        headers=headers,
    )
    assert client.delete("/api/import/data", headers=headers).status_code == 204
    status = client.get("/api/import/status", headers=headers).json()
    assert status["fox_ess"] is None
    assert status["usage"]["count"] == 0
    assert status["weather"]["count"] == 0
    assert status["latest_job"] is None
    system = client.get("/api/system", headers=headers).json()
    assert system["data_source"] == "none"
    assert system["battery"]["capacity_kwh"] == SYSTEM["battery"]["capacity_kwh"]


def test_delete_account(client, headers):
    from app.database import get_session_factory
    from app.models import FoxEssConnection, System, UsageRecord, User

    connect(client, headers)
    client.post(
        "/api/import/fox-ess/sync",
        json={"start_date": "2024-06-01", "end_date": "2024-06-01"},
        headers=headers,
    )
    assert client.delete("/api/auth/me", headers=headers).status_code == 204
    assert client.get("/api/auth/me", headers=headers).status_code == 401
    with app.dependency_overrides[get_session_factory]()() as db:
        for model in (User, System, FoxEssConnection, UsageRecord):
            assert db.query(model).count() == 0


def test_times_are_returned_as_utc(client, headers):
    connect(client, headers)
    client.post(
        "/api/import/fox-ess/sync",
        json={"start_date": "2024-06-01", "end_date": "2024-06-01"},
        headers=headers,
    )
    status = client.get("/api/import/status", headers=headers).json()
    assert status["fox_ess"]["last_synced_at"].endswith(("Z", "+00:00"))
    assert status["latest_job"]["finished_at"].endswith(("Z", "+00:00"))
