from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.crypto import encrypt
from app.database import get_db, get_session_factory
from app.importers.base import INTERVAL
from app.importers.csv_import import CsvImportError, parse_csv
from app.importers.fox_ess import FoxEssAuthError, FoxEssClient, FoxEssError
from app.importers.store import (
    delete_imported_data,
    run_fox_backfill,
    save_intervals,
    sync_weather,
)
from app.models import (
    DataSourceKind,
    FoxEssConnection,
    ImportJob,
    JobStatus,
    System,
    UsageRecord,
    UsageSource,
    User,
    WeatherRecord,
)
from app.schemas import (
    Coverage,
    CsvImportOut,
    FoxEssConnectIn,
    FoxEssOut,
    ImportJobOut,
    ImportStatus,
    MatchedInterval,
    SyncRequest,
    WeatherOut,
)
from app.security import current_user
from app.weather.open_meteo import OpenMeteoClient, WeatherError

router = APIRouter(prefix="/api/import", tags=["import"])

# Fox allows 1440 calls per inverter per day and a backfill makes one per day of history.
MAX_SYNC_DAYS = 366
FOX_EARLIEST = date(2020, 1, 1)
MAX_CSV_BYTES = 10 * 1024 * 1024
MAX_INTERVAL_DAYS = 31


def get_fox_client_factory() -> Callable[[str], FoxEssClient]:
    return FoxEssClient


def get_weather_client() -> OpenMeteoClient:
    return OpenMeteoClient()


def _system(user: User) -> System:
    if user.system is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Set up your system first")
    return user.system


def _active_job(db: Session, system: System) -> ImportJob | None:
    return db.scalar(
        select(ImportJob).where(
            ImportJob.system_id == system.id,
            ImportJob.status.in_([JobStatus.queued, JobStatus.running]),
        )
    )


def _coverage(
    db: Session, model: type[UsageRecord] | type[WeatherRecord], system: System
) -> Coverage:
    count, first, last = db.execute(
        select(func.count(model.id), func.min(model.start), func.max(model.start)).where(
            model.system_id == system.id
        )
    ).one()
    as_utc = lambda m: m.replace(tzinfo=timezone.utc) if m else None  # noqa: E731
    return Coverage(count=count, first=as_utc(first), last=as_utc(last))


@router.get("/status", response_model=ImportStatus)
def import_status(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> ImportStatus:
    system = _system(user)
    weather_hours = set(
        db.scalars(select(WeatherRecord.start).where(WeatherRecord.system_id == system.id))
    )
    matched = sum(
        1
        for start in db.scalars(select(UsageRecord.start).where(UsageRecord.system_id == system.id))
        if start.replace(minute=0) in weather_hours
    )
    latest = db.scalar(
        select(ImportJob).where(ImportJob.system_id == system.id).order_by(ImportJob.id.desc())
    )
    return ImportStatus(
        fox_ess=FoxEssOut.model_validate(system.fox_ess) if system.fox_ess else None,
        usage=_coverage(db, UsageRecord, system),
        weather=_coverage(db, WeatherRecord, system),
        matched_intervals=matched,
        latest_job=ImportJobOut.model_validate(latest) if latest else None,
    )


@router.put("/fox-ess", response_model=FoxEssOut)
def connect_fox_ess(
    body: FoxEssConnectIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    fox_client: Callable[[str], FoxEssClient] = Depends(get_fox_client_factory),
) -> FoxEssOut:
    """Check the key against Fox ESS, then store it encrypted. The battery's minimum charge is
    read from the inverter too, when Fox reports it, and copied into the battery settings."""
    system = _system(user)
    client = fox_client(body.api_key)
    try:
        serials = client.device_serials()
    except FoxEssAuthError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Fox ESS did not accept that API key")
    except FoxEssError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    if body.device_sn.upper() not in {s.upper() for s in serials}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That inverter serial number is not on this Fox ESS account",
        )
    connection = system.fox_ess or FoxEssConnection(system=system)
    connection.api_key_encrypted = encrypt(body.api_key)
    connection.api_key_hint = body.api_key[-4:]
    connection.device_sn = body.device_sn
    system.data_source = DataSourceKind.fox_ess
    try:
        min_soc = client.battery_min_soc(body.device_sn)
    except FoxEssError:
        min_soc = None  # Not every inverter or account exposes it; the user can set it.
    if min_soc is not None and system.battery is not None:
        system.battery.min_soc_percent = min_soc
    db.add(connection)
    db.commit()
    out = FoxEssOut.model_validate(connection)
    out.battery_min_soc_percent = min_soc
    return out


@router.delete("/fox-ess", status_code=status.HTTP_204_NO_CONTENT)
def disconnect_fox_ess(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    """Forget the API key. Imported data is kept."""
    system = _system(user)
    if _active_job(db, system):
        raise HTTPException(status.HTTP_409_CONFLICT, "Wait for the running import to finish")
    system.fox_ess = None
    if system.data_source == DataSourceKind.fox_ess:
        system.data_source = DataSourceKind.none
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/data", status_code=status.HTTP_204_NO_CONTENT)
def delete_data(user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    """Delete all imported usage and weather, import history and the Fox ESS key.

    The system description (battery, panels, tariff) is kept.
    """
    system = _system(user)
    if _active_job(db, system):
        raise HTTPException(status.HTTP_409_CONFLICT, "Wait for the running import to finish")
    delete_imported_data(db, system)
    system.data_source = DataSourceKind.none
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/fox-ess/sync", response_model=ImportJobOut, status_code=status.HTTP_202_ACCEPTED)
def sync_fox_ess(
    body: SyncRequest,
    background: BackgroundTasks,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    session_factory: sessionmaker[Session] = Depends(get_session_factory),
    fox_client: Callable[[str], FoxEssClient] = Depends(get_fox_client_factory),
    weather_client: OpenMeteoClient = Depends(get_weather_client),
) -> ImportJob:
    """Start a background import of past days. Days already imported are skipped."""
    system = _system(user)
    if system.fox_ess is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Connect Fox ESS first")
    today = datetime.now(ZoneInfo(system.timezone)).date()
    if body.end_date > today:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "End date is in the future")
    if body.start_date < FOX_EARLIEST:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Fox ESS history starts in 2020")
    days = (body.end_date - body.start_date).days + 1
    if days > MAX_SYNC_DAYS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Import at most {MAX_SYNC_DAYS} days at a time",
        )
    if _active_job(db, system):
        raise HTTPException(status.HTTP_409_CONFLICT, "An import is already running")

    job = ImportJob(
        system=system, start_date=body.start_date, end_date=body.end_date, days_total=days
    )
    db.add(job)
    db.commit()
    background.add_task(run_fox_backfill, job.id, session_factory, fox_client, weather_client)
    return job


@router.post("/csv", response_model=CsvImportOut)
async def upload_csv(
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    weather_client: OpenMeteoClient = Depends(get_weather_client),
) -> CsvImportOut:
    system = _system(user)
    raw = await file.read(MAX_CSV_BYTES + 1)
    if len(raw) > MAX_CSV_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "The file is larger than 10 MB")
    try:
        intervals = parse_csv(raw.decode("utf-8-sig"), system.timezone)
    except UnicodeDecodeError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The file is not UTF-8 text")
    except CsvImportError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))

    count = save_intervals(db, system, intervals, UsageSource.csv)
    if system.data_source == DataSourceKind.none:
        system.data_source = DataSourceKind.csv
    db.commit()

    message, weather_hours = None, 0
    try:
        weather_hours = sync_weather(db, system, weather_client)
        db.commit()
    except WeatherError as exc:
        db.rollback()
        message = f"Usage imported, but weather could not be fetched: {exc}"
    return CsvImportOut(
        intervals_imported=count, weather_hours_added=weather_hours, message=message
    )


@router.post("/weather", response_model=CsvImportOut)
def fetch_missing_weather(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    weather_client: OpenMeteoClient = Depends(get_weather_client),
) -> CsvImportOut:
    """Retry the weather lookup for imported days that have none, e.g. after an outage."""
    system = _system(user)
    try:
        hours = sync_weather(db, system, weather_client)
    except WeatherError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    db.commit()
    return CsvImportOut(intervals_imported=0, weather_hours_added=hours)


@router.get("/intervals", response_model=list[MatchedInterval])
def matched_intervals(
    start: date = Query(description="First local day"),
    end: date = Query(description="Last local day, inclusive"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[MatchedInterval]:
    """Imported half hours with the weather for each, ready for the prediction model."""
    system = _system(user)
    if end < start or (end - start).days >= MAX_INTERVAL_DAYS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Ask for 1 to {MAX_INTERVAL_DAYS} days at a time",
        )
    zone = ZoneInfo(system.timezone)
    since = datetime.combine(start, time(), zone).astimezone(timezone.utc).replace(tzinfo=None)
    until = (
        datetime.combine(end + timedelta(days=1), time(), zone)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )
    usage = db.scalars(
        select(UsageRecord)
        .where(
            UsageRecord.system_id == system.id,
            UsageRecord.start >= since,
            UsageRecord.start < until,
        )
        .order_by(UsageRecord.start)
    ).all()
    weather = {
        w.start: w
        for w in db.scalars(
            select(WeatherRecord).where(
                WeatherRecord.system_id == system.id,
                WeatherRecord.start >= since - timedelta(hours=1),
                WeatherRecord.start < until,
            )
        )
    }
    out = []
    for u in usage:
        w = weather.get(u.start.replace(minute=0))
        out.append(
            MatchedInterval(
                start=u.start.replace(tzinfo=timezone.utc),
                end=u.start.replace(tzinfo=timezone.utc) + INTERVAL,
                load_kwh=u.load_kwh,
                solar_kwh=u.solar_kwh,
                grid_import_kwh=u.grid_import_kwh,
                grid_export_kwh=u.grid_export_kwh,
                battery_soc_percent=u.battery_soc_percent,
                source=u.source,
                weather=WeatherOut.model_validate(w) if w else None,
            )
        )
    return out
