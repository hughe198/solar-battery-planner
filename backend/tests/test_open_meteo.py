from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from app.weather.open_meteo import (
    ForecastCache,
    OpenMeteoClient,
    WeatherError,
    parse_hourly,
)


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
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        today=lambda: date(2024, 6, 30),
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


def day_payload(day: str):
    """Two days of hours either side of `day` so every hour of `day` has both halves."""
    d = date.fromisoformat(day)
    times = [
        f"{(d + timedelta(days=offset)).isoformat()}T{h:02d}:00"
        for offset in (-1, 0, 1)
        for h in range(24)
    ]
    n = len(times)
    return {
        "hourly": {
            "time": times,
            "temperature_2m": [8.0] * n,
            "cloud_cover": [50] * n,
            "shortwave_radiation": [100.0] * n,
            "direct_radiation": [60.0] * n,
            "diffuse_radiation": [40.0] * n,
        }
    }


def scripted(responses, calls):
    def handler(request):
        calls.append(request.url.host)
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    return httpx.Client(transport=httpx.MockTransport(handler))


def make_client(responses, calls, sleeps, now=None, cache=None):
    return OpenMeteoClient(
        http=scripted(responses, calls),
        sleep=sleeps.append,
        cache=cache if cache is not None else ForecastCache(),
        now=now or (lambda: datetime(2026, 11, 8, 12, tzinfo=timezone.utc)),
    )


def test_retries_a_503_then_succeeds():
    calls, sleeps = [], []
    client = make_client(
        [httpx.Response(503), httpx.Response(200, json=day_payload("2026-11-09"))],
        calls,
        sleeps,
    )
    rows = client.forecast(51.85, -2.22, date(2026, 11, 9), date(2026, 11, 9))
    assert len(rows) == 24
    assert len(calls) == 2
    assert sleeps == [1.0]


def test_does_not_retry_a_bad_request():
    calls, sleeps = [], []
    client = make_client([httpx.Response(400)], calls, sleeps)
    with pytest.raises(WeatherError, match="refused"):
        client.forecast(51.85, -2.22, date(2026, 11, 9), date(2026, 11, 9))
    assert len(calls) == 1
    assert sleeps == []


def test_gives_a_friendly_message_when_still_down():
    calls, sleeps = [], []
    client = make_client([httpx.Response(503)] * 3, calls, sleeps)
    with pytest.raises(WeatherError) as err:
        client.forecast(51.85, -2.22, date(2026, 11, 9), date(2026, 11, 9))
    assert len(calls) == 3
    assert "busy or down right now (503 Service Unavailable)" in str(err.value)
    assert "https://" not in str(err.value)


def test_retries_a_dropped_connection():
    calls, sleeps = [], []
    client = make_client(
        [
            httpx.ConnectError("reset"),
            httpx.Response(200, json=day_payload("2026-11-09")),
        ],
        calls,
        sleeps,
    )
    assert len(client.forecast(51.85, -2.22, date(2026, 11, 9), date(2026, 11, 9))) == 24


def test_falls_back_to_the_last_forecast_during_an_outage():
    cache = ForecastCache()
    clock = [datetime(2026, 11, 8, 12, tzinfo=timezone.utc)]
    calls, sleeps = [], []
    client = make_client(
        [httpx.Response(200, json=day_payload("2026-11-09"))] + [httpx.Response(503)] * 6,
        calls,
        sleeps,
        now=lambda: clock[0],
        cache=cache,
    )
    fresh = client.forecast(51.85, -2.22, date(2026, 11, 9), date(2026, 11, 9))

    clock[0] += timedelta(hours=6)
    assert client.forecast(51.85, -2.22, date(2026, 11, 9), date(2026, 11, 9)) == fresh

    # Too old to trust: the outage shows.
    clock[0] += timedelta(hours=7)
    with pytest.raises(WeatherError):
        client.forecast(51.85, -2.22, date(2026, 11, 9), date(2026, 11, 9))


def test_no_fallback_when_the_cache_misses_part_of_the_range():
    cache = ForecastCache()
    calls, sleeps = [], []
    client = make_client(
        [httpx.Response(200, json=day_payload("2026-11-09"))] + [httpx.Response(503)] * 3,
        calls,
        sleeps,
        cache=cache,
    )
    client.forecast(51.85, -2.22, date(2026, 11, 9), date(2026, 11, 9))
    with pytest.raises(WeatherError):
        client.forecast(51.85, -2.22, date(2026, 11, 9), date(2026, 11, 10))
