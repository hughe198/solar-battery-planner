"""Hourly historical and forecast weather from Open-Meteo (https://open-meteo.com).

The reanalysis archive lags real time by several days, so the last week comes from the
historical forecast API instead. Forecasts for the coming days come from the forecast API.
No API key is needed; the free tier is for non-commercial use.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import settings

HOURLY_VARIABLES = (
    "temperature_2m",
    "cloud_cover",
    "shortwave_radiation",
    "direct_radiation",
    "diffuse_radiation",
)
RADIATION_VARIABLES = ("shortwave_radiation", "direct_radiation", "diffuse_radiation")
RECENT_DAYS = 7
# Open-Meteo's name for "as forecast a day earlier": each variable with this on the end.
DAY_AHEAD_SUFFIX = "_previous_day1"
# Open-Meteo has short outages (502/503/504) and rate limits (429). Retry those a couple of
# times before giving up; other errors (bad request, bad JSON) won't fix themselves.
RETRY_DELAYS_SECONDS = (1.0, 3.0)
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# When the forecast API is down, plan on the last forecast fetched for the same place if it
# is no older than this. Forecasts a few hours old are nearly as good as fresh ones.
STALE_FORECAST_MAX_AGE = timedelta(hours=12)

log = logging.getLogger(__name__)


class WeatherError(Exception):
    pass


class ForecastCache:
    """The last forecast hours fetched for each place, kept in memory, so a short Open-Meteo
    outage falls back to a forecast a few hours old instead of failing the plan."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._places: dict[
            tuple[float, float], dict[datetime, tuple[datetime, "HourlyWeather"]]
        ] = {}

    @staticmethod
    def _key(lat: float, lon: float) -> tuple[float, float]:
        return (round(lat, 4), round(lon, 4))

    def store(self, lat: float, lon: float, rows: "list[HourlyWeather]", fetched: datetime) -> None:
        with self._lock:
            place = self._places.setdefault(self._key(lat, lon), {})
            for row in rows:
                place[row.start] = (fetched, row)
            # Drop hours well in the past so the cache doesn't grow forever.
            cutoff = fetched - timedelta(days=3)
            for start in [s for s in place if s < cutoff]:
                del place[start]

    def recall(
        self, lat: float, lon: float, start: date, end: date, now: datetime
    ) -> "list[HourlyWeather]":
        with self._lock:
            place = self._places.get(self._key(lat, lon), {})
            return [
                row
                for when, row in sorted(place.values(), key=lambda v: v[1].start)
                if start <= row.start.date() <= end and now - when <= STALE_FORECAST_MAX_AGE
            ]

    def clear(self) -> None:
        with self._lock:
            self._places.clear()


forecast_cache = ForecastCache()
# Day-ahead forecasts for past days never change, and a plan is made many times a day: keep
# the last few answers (by place and dates) rather than ask again each time.
day_ahead_cache: dict[tuple[float, float, date, date], "list[HourlyWeather]"] = {}
DAY_AHEAD_CACHE_SIZE = 8


def _describe(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in RETRY_STATUSES:
            return (
                f"The weather service (Open-Meteo) is busy or down right now ({code} "
                f"{exc.response.reason_phrase}). This is usually brief, so try again in a few "
                "minutes"
            )
        return f"The weather service (Open-Meteo) refused the request ({code} {exc.response.reason_phrase})"
    if isinstance(exc, httpx.TimeoutException):
        return "The weather service (Open-Meteo) didn't answer in time. Try again in a few minutes"
    if isinstance(exc, httpx.TransportError):
        return (
            "Couldn't reach the weather service (Open-Meteo). Check the server's internet "
            "connection, or try again in a few minutes"
        )
    return f"The weather service (Open-Meteo) sent something unexpected: {exc}"


@dataclass(frozen=True)
class HourlyWeather:
    """Weather for the hour starting at `start` (aware UTC)."""

    start: datetime
    temperature_c: float | None
    cloud_cover_percent: float | None
    shortwave_radiation_wm2: float | None
    direct_radiation_wm2: float | None
    diffuse_radiation_wm2: float | None


def parse_hourly(payload: dict[str, Any]) -> list[HourlyWeather]:
    """Open-Meteo labels radiation with the end of the hour it averages over, and temperature
    and cloud cover with the instant they apply to. Re-key everything by hour start."""
    hourly = payload.get("hourly") or {}
    times = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t in hourly.get("time", [])]
    by_start: dict[datetime, dict[str, float | None]] = {}
    for variable in HOURLY_VARIABLES:
        shift = timedelta(hours=1) if variable in RADIATION_VARIABLES else timedelta()
        for moment, value in zip(times, hourly.get(variable, [])):
            by_start.setdefault(moment - shift, {})[variable] = value

    return [
        HourlyWeather(
            start=start,
            temperature_c=v.get("temperature_2m"),
            cloud_cover_percent=v.get("cloud_cover"),
            shortwave_radiation_wm2=v.get("shortwave_radiation"),
            direct_radiation_wm2=v.get("direct_radiation"),
            diffuse_radiation_wm2=v.get("diffuse_radiation"),
        )
        for start, v in sorted(by_start.items())
        # Drop hours that only got the shifted radiation half (the edges of the range).
        if "temperature_2m" in v and "shortwave_radiation" in v
    ]


class OpenMeteoClient:
    def __init__(
        self,
        http: httpx.Client | None = None,
        today: Callable[[], date] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        cache: ForecastCache | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.http = http or httpx.Client(timeout=60)
        self.today = today or (lambda: datetime.now(timezone.utc).date())
        self.sleep = sleep
        self.cache = forecast_cache if cache is None else cache
        self.now = now or (lambda: datetime.now(timezone.utc))

    def _get(
        self, url: str, lat: float, lon: float, start: date, end: date, suffix: str = ""
    ) -> list[HourlyWeather]:
        params = {
            "latitude": lat,
            "longitude": lon,
            # One extra day each side so every hour in range has both halves after re-keying.
            "start_date": (start - timedelta(days=1)).isoformat(),
            "end_date": (end + timedelta(days=1)).isoformat(),
            "hourly": ",".join(v + suffix for v in HOURLY_VARIABLES),
            "timezone": "GMT",
        }
        for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
            try:
                resp = self.http.get(url, params=params)
                resp.raise_for_status()
                payload = resp.json()
                if suffix:
                    hourly = payload.get("hourly") or {}
                    payload = {"hourly": {k.removesuffix(suffix): v for k, v in hourly.items()}}
                return parse_hourly(payload)
            except (httpx.HTTPError, ValueError) as exc:
                # Not timeouts: each already waited a full minute.
                transient = (
                    isinstance(exc, httpx.TransportError)
                    and not isinstance(exc, httpx.TimeoutException)
                ) or (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code in RETRY_STATUSES
                )
                if not transient or attempt == len(RETRY_DELAYS_SECONDS):
                    raise WeatherError(_describe(exc)) from exc
                log.warning("Open-Meteo request failed, retrying: %s", exc)
                self.sleep(RETRY_DELAYS_SECONDS[attempt])
        raise AssertionError("unreachable")

    def hourly(self, lat: float, lon: float, start: date, end: date) -> list[HourlyWeather]:
        """Hours whose UTC date is within [start, end] inclusive."""
        recent_from = self.today() - timedelta(days=RECENT_DAYS)
        rows: list[HourlyWeather] = []
        if start < recent_from:
            archive_end = min(end, recent_from - timedelta(days=1))
            rows += self._in_range(
                self._get(settings.open_meteo_archive_url, lat, lon, start, archive_end),
                start,
                archive_end,
            )
        if end >= recent_from:
            recent_start = max(start, recent_from)
            rows += self._in_range(
                self._get(settings.open_meteo_recent_url, lat, lon, recent_start, end),
                recent_start,
                end,
            )
        return rows

    def forecast(self, lat: float, lon: float, start: date, end: date) -> list[HourlyWeather]:
        """Forecast hours whose UTC date is within [start, end] inclusive (up to ~2 weeks ahead).

        If Open-Meteo is down, falls back to the last forecast fetched for this place when it
        is recent enough; callers check that every hour they need is there."""
        try:
            rows = self._in_range(
                self._get(settings.open_meteo_forecast_url, lat, lon, start, end),
                start,
                end,
            )
        except WeatherError:
            cached = self.cache.recall(lat, lon, start, end, self.now())
            hours_wanted = ((end - start).days + 1) * 24
            if len(cached) < hours_wanted or any(r.shortwave_radiation_wm2 is None for r in cached):
                raise
            log.warning("Open-Meteo forecast unavailable; using the last forecast fetched")
            return cached
        self.cache.store(lat, lon, rows, self.now())
        return rows

    def day_ahead(self, lat: float, lon: float, start: date, end: date) -> list[HourlyWeather]:
        """What was forecast a day ahead for the past hours whose UTC date is within
        [start, end] inclusive: the weather the planner would have been told, to compare with
        what the panels then made."""
        key = (round(lat, 4), round(lon, 4), start, end)
        if key not in day_ahead_cache:
            rows = self._in_range(
                self._get(
                    settings.open_meteo_previous_runs_url, lat, lon, start, end, DAY_AHEAD_SUFFIX
                ),
                start,
                end,
            )
            while len(day_ahead_cache) >= DAY_AHEAD_CACHE_SIZE:
                del day_ahead_cache[next(iter(day_ahead_cache))]
            day_ahead_cache[key] = rows
        return day_ahead_cache[key]

    @staticmethod
    def _in_range(rows: list[HourlyWeather], start: date, end: date) -> list[HourlyWeather]:
        return [r for r in rows if start <= r.start.date() <= end]
