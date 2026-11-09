"""Keep what was forecast for each day before it began, for the look back at yesterday."""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.forecasting.base import DayForecast, Range
from app.models import ForecastSnapshot, System


def _scaled(day_total: float, cycle: Range) -> tuple[float, float]:
    """The day's likely range, in the same proportion as the forecast's range for its cycle."""
    if cycle.expected <= 0:
        return day_total, day_total
    return day_total * cycle.low / cycle.expected, day_total * cycle.high / cycle.expected


def remember_forecast(
    db: Session,
    system: System,
    day: date,
    forecast: DayForecast,
    target_soc_percent: float | None,
    window_ends: datetime,
    now: datetime | None = None,
) -> ForecastSnapshot | None:
    """Save or update the forecast for local `day`, until its cheap window ends (`window_ends`);
    after that the day has started for the battery and the saved forecast is left alone.
    Adds to the session; the caller commits. A None target keeps any target already saved."""
    now = now or datetime.now(timezone.utc)
    if now >= window_ends:
        return None
    zone = ZoneInfo(system.timezone)
    own = [i for i in forecast.intervals if i.start.astimezone(zone).date() == day]
    if not own:
        return None
    load = sum(i.load_kwh for i in own)
    solar = sum(i.solar_kwh for i in own)
    load_low, load_high = _scaled(load, forecast.load)
    solar_low, solar_high = _scaled(solar, forecast.solar)

    snap = db.scalar(
        select(ForecastSnapshot).where(
            ForecastSnapshot.system_id == system.id, ForecastSnapshot.day == day
        )
    )
    if snap is None:
        snap = ForecastSnapshot(system_id=system.id, day=day)
        db.add(snap)
    snap.made_at = now
    snap.load_kwh = round(load, 3)
    snap.load_low_kwh = round(load_low, 3)
    snap.load_high_kwh = round(load_high, 3)
    snap.solar_kwh = round(solar, 3)
    snap.solar_low_kwh = round(solar_low, 3)
    snap.solar_high_kwh = round(solar_high, 3)
    story = forecast.weather
    snap.cloud_percent = story.daylight_cloud_percent if story else None
    snap.temperature_c = story.mean_temperature_c if story else None
    snap.weather_headline = story.headline if story else None
    if target_soc_percent is not None:
        snap.target_soc_percent = target_soc_percent
    return snap
