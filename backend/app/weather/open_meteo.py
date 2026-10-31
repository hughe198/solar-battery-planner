"""Hourly historical weather from Open-Meteo (https://open-meteo.com).

The reanalysis archive lags real time by several days, so the last week comes from the
historical forecast API instead. No API key is needed; the free tier is for non-commercial use.
"""

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


class WeatherError(Exception):
    pass


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
        self, http: httpx.Client | None = None, today: Callable[[], date] | None = None
    ) -> None:
        self.http = http or httpx.Client(timeout=60)
        self.today = today or (lambda: datetime.now(timezone.utc).date())

    def _get(self, url: str, lat: float, lon: float, start: date, end: date) -> list[HourlyWeather]:
        params = {
            "latitude": lat,
            "longitude": lon,
            # One extra day each side so every hour in range has both halves after re-keying.
            "start_date": (start - timedelta(days=1)).isoformat(),
            "end_date": (end + timedelta(days=1)).isoformat(),
            "hourly": ",".join(HOURLY_VARIABLES),
            "timezone": "GMT",
        }
        try:
            resp = self.http.get(url, params=params)
            resp.raise_for_status()
            return parse_hourly(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise WeatherError(f"Open-Meteo request failed: {exc}") from exc

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

    @staticmethod
    def _in_range(rows: list[HourlyWeather], start: date, end: date) -> list[HourlyWeather]:
        return [r for r in rows if start <= r.start.date() <= end]
