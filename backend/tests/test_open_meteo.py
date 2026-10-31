from datetime import date, datetime, timezone

import httpx

from app.weather.open_meteo import OpenMeteoClient, parse_hourly


def payload(start_hour, hours):
    times = [f"2024-06-01T{h:02d}:00" for h in range(start_hour, start_hour + hours)]
    return {
        "hourly": {
            "time": times,
            "temperature_2m": [10.0 + i for i in range(hours)],
            "cloud_cover": [50] * hours,
            "shortwave_radiation": [100.0 * i for i in range(hours)],
            "direct_radiation": [60.0 * i for i in range(hours)],
            "diffuse_radiation": [40.0 * i for i in range(hours)],
        }
    }


def test_radiation_is_rekeyed_to_hour_start():
    rows = parse_hourly(payload(9, 3))
    # 09:00 temperature with the 09:00-10:00 radiation (labelled 10:00 by Open-Meteo).
    assert [r.start.hour for r in rows] == [9, 10]
    assert rows[0].temperature_c == 10.0
    assert rows[0].shortwave_radiation_wm2 == 100.0
    assert rows[1].shortwave_radiation_wm2 == 200.0


def test_uses_archive_for_old_days_and_recent_api_for_last_week():
    urls = []

    def handler(request):
        urls.append((request.url.host, request.url.params["start_date"]))
        return httpx.Response(200, json={"hourly": {"time": []}})

    client = OpenMeteoClient(
        http=httpx.Client(transport=httpx.MockTransport(handler)), today=lambda: date(2024, 6, 30)
    )
    client.hourly(51.5, -0.1, date(2024, 6, 1), date(2024, 6, 28))
    assert urls == [
        ("archive-api.open-meteo.com", "2024-05-31"),
        ("historical-forecast-api.open-meteo.com", "2024-06-22"),
    ]
    urls.clear()
    client.hourly(51.5, -0.1, date(2024, 1, 1), date(2024, 1, 2))
    assert [u[0] for u in urls] == ["archive-api.open-meteo.com"]


def test_aware_utc_starts():
    (row,) = parse_hourly(payload(0, 2))
    assert row.start == datetime(2024, 6, 1, 0, tzinfo=timezone.utc)
