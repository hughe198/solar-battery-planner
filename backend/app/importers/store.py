"""Saves imported intervals and the weather that matches them."""

import logging
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.crypto import decrypt
from app.importers.base import UsageInterval
from app.importers.fox_ess import FoxEssClient, FoxEssError, FoxEssImporter, local_day_bounds
from app.models import (
    ControlAction,
    FoxEssConnection,
    ImportJob,
    InverterControl,
    JobStatus,
    System,
    UsageRecord,
    UsageSource,
    WeatherRecord,
)
from app.weather.open_meteo import OpenMeteoClient, WeatherError

log = logging.getLogger(__name__)

# A local day with at least this many half hours stored is not fetched again.
COMPLETE_DAY_INTERVALS = 46


def _naive_utc(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def delete_imported_data(db: Session, system: System) -> None:
    """Remove everything imported for the system, including the stored Fox ESS key, and the
    inverter control opt-in and its log (they need the key)."""
    for model in (
        UsageRecord,
        WeatherRecord,
        ImportJob,
        FoxEssConnection,
        InverterControl,
        ControlAction,
    ):
        db.execute(delete(model).where(model.system_id == system.id))
    db.expire(system)


def save_intervals(
    db: Session, system: System, intervals: Iterable[UsageInterval], source: UsageSource
) -> int:
    """Insert or replace half-hour records. Returns how many were written."""
    rows = {_naive_utc(i.start): i for i in intervals}
    if not rows:
        return 0
    existing = {
        r.start: r
        for r in db.scalars(
            select(UsageRecord).where(
                UsageRecord.system_id == system.id,
                UsageRecord.start >= min(rows),
                UsageRecord.start <= max(rows),
            )
        )
    }
    for start, interval in rows.items():
        record = existing.get(start) or UsageRecord(system_id=system.id, start=start)
        record.load_kwh = interval.load_kwh
        record.solar_kwh = interval.solar_kwh
        record.grid_import_kwh = interval.grid_import_kwh
        record.grid_export_kwh = interval.grid_export_kwh
        record.battery_soc_percent = interval.battery_soc_percent
        record.source = source
        db.add(record)
    db.flush()
    return len(rows)


def intervals_on_local_day(db: Session, system: System, day: date) -> int:
    start, end = local_day_bounds(day, system.timezone)
    return db.scalar(
        select(func.count(UsageRecord.id)).where(
            UsageRecord.system_id == system.id,
            UsageRecord.start >= _naive_utc(start),
            UsageRecord.start < _naive_utc(end),
        )
    )


def sync_weather(db: Session, system: System, client: OpenMeteoClient) -> int:
    """Fetch weather for every UTC day that has usage but not a full day of weather."""
    usage_days = {
        d if isinstance(d, date) else date.fromisoformat(str(d))
        for d in db.scalars(
            select(func.date(UsageRecord.start))
            .where(UsageRecord.system_id == system.id)
            .distinct()
        )
    }
    weather_days = {
        d if isinstance(d, date) else date.fromisoformat(str(d))
        for d, hours in db.execute(
            select(func.date(WeatherRecord.start), func.count(WeatherRecord.id))
            .where(WeatherRecord.system_id == system.id)
            .group_by(func.date(WeatherRecord.start))
        )
        if hours >= 24
    }
    # Future hours have no observations yet; don't keep asking for today.
    today = datetime.now(timezone.utc).date()
    missing = sorted(d for d in usage_days - weather_days if d < today)
    if not missing:
        return 0

    hours = client.hourly(system.latitude, system.longitude, missing[0], missing[-1])
    wanted = set(missing)
    hours = [h for h in hours if h.start.date() in wanted]
    if not hours:
        return 0
    existing = {
        r.start: r
        for r in db.scalars(
            select(WeatherRecord).where(
                WeatherRecord.system_id == system.id,
                WeatherRecord.start >= _naive_utc(hours[0].start),
                WeatherRecord.start <= _naive_utc(hours[-1].start),
            )
        )
    }
    for h in hours:
        start = _naive_utc(h.start)
        record = existing.get(start) or WeatherRecord(system_id=system.id, start=start)
        record.temperature_c = h.temperature_c
        record.cloud_cover_percent = h.cloud_cover_percent
        record.shortwave_radiation_wm2 = h.shortwave_radiation_wm2
        record.direct_radiation_wm2 = h.direct_radiation_wm2
        record.diffuse_radiation_wm2 = h.diffuse_radiation_wm2
        db.add(record)
    db.flush()
    return len(hours)


def run_fox_backfill(
    job_id: int,
    session_factory: sessionmaker[Session],
    fox_client: Callable[[str], FoxEssClient],
    weather_client: OpenMeteoClient,
) -> None:
    """Import a range of local days from Fox ESS, newest first, then match weather.

    Days already stored are skipped to save the user's daily API quota. Progress is committed
    after every day so a failure keeps what was fetched.
    """
    with session_factory() as db:
        job = db.get(ImportJob, job_id)
        system = job.system
        connection = system.fox_ess
        job.status = JobStatus.running
        db.commit()
        try:
            if connection is None:
                raise FoxEssError("Fox ESS is not connected")
            importer = FoxEssImporter(
                fox_client(decrypt(connection.api_key_encrypted)), connection.device_sn
            )
            day = job.end_date
            while day >= job.start_date:
                if intervals_on_local_day(db, system, day) >= COMPLETE_DAY_INTERVALS:
                    job.days_skipped += 1
                else:
                    start, end = local_day_bounds(day, system.timezone)
                    job.intervals_imported += save_intervals(
                        db, system, importer.fetch(start, end), UsageSource.fox_ess
                    )
                job.days_done += 1
                connection.last_synced_at = datetime.now(timezone.utc)
                db.commit()
                day -= timedelta(days=1)
        except FoxEssError as exc:
            db.rollback()
            job.status = JobStatus.failed
            job.message = str(exc)[:500]
        except Exception:
            log.exception("Fox ESS import job %s failed", job_id)
            db.rollback()
            job.status = JobStatus.failed
            job.message = "The import stopped because of an unexpected error."
        else:
            job.status = JobStatus.done
            try:
                sync_weather(db, system, weather_client)
            except WeatherError as exc:
                job.message = f"Usage imported, but weather could not be fetched: {exc}"[:500]
        job.finished_at = datetime.now(timezone.utc)
        db.commit()
