from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from app.api.imports import get_weather_client
from app.database import get_db, get_session_factory
from app.importers.store import refetch_weather
from app.models import (
    Battery,
    PastTariff,
    RateDays,
    RateDirection,
    SolarArray,
    System,
    Tariff,
    TariffBand,
    User,
    WeatherRecord,
)
from app.schemas import SystemIn, SystemOut, TariffIn, TariffPeriodOut, TariffPresetOut
from app.tariffs import PRESETS, Preset
from app.security import current_user
from app.weather.open_meteo import OpenMeteoClient

router = APIRouter(prefix="/api/system", tags=["system"])


def _band_key(band) -> tuple:
    return (
        RateDirection(band.direction).value,
        RateDays(band.days).value,
        band.start,
        band.end,
        band.rate,
    )


def _rates_changed(current: Tariff, new: TariffIn) -> bool:
    return current.standing_charge_pence_per_day != new.standing_charge_pence_per_day or sorted(
        map(_band_key, current.bands)
    ) != sorted(map(_band_key, new.bands))


def _record_tariff_change(system: System, effective: date) -> None:
    """Keep what the current tariff charged before `effective`. A date before the current tariff
    began replaces whatever applied from then on, which is also how a mistake gets corrected."""
    zone = ZoneInfo(system.timezone)
    at = datetime.combine(effective, time(), zone).astimezone(timezone.utc).replace(tzinfo=None)
    rows = list(system.tariff_history)
    kept = [r for r in rows if r.valid_until <= at]
    later = rows[len(kept) :]
    if not later:
        if not kept or kept[-1].valid_until < at:
            tariff = system.tariff
            system.tariff_history.append(
                PastTariff(
                    valid_until=at,
                    name=tariff.name,
                    standing_charge_pence_per_day=tariff.standing_charge_pence_per_day,
                    bands=[
                        {
                            "direction": RateDirection(b.direction).value,
                            "days": RateDays(b.days).value,
                            "start": b.start.isoformat(),
                            "end": b.end.isoformat(),
                            "rate": b.rate,
                        }
                        for b in tariff.bands
                    ],
                )
            )
        return
    covering, *replaced = later
    if kept and kept[-1].valid_until == at:
        replaced.insert(0, covering)
    else:
        covering.valid_until = at
    for row in replaced:
        system.tariff_history.remove(row)


@router.get("", response_model=SystemOut)
def get_system(user: User = Depends(current_user)) -> System:
    if user.system is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No system set up yet")
    return user.system


@router.put("", response_model=SystemOut)
def save_system(
    body: SystemIn,
    background: BackgroundTasks,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    session_factory: sessionmaker[Session] = Depends(get_session_factory),
    weather_client: OpenMeteoClient = Depends(get_weather_client),
) -> System:
    """Create or replace the signed-in user's system."""
    system = user.system or System(user=user)
    today = datetime.now(ZoneInfo(body.timezone)).date()
    if body.tariff_effective_from is not None and body.tariff_effective_from > today:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "A tariff can't start in the future. Change it on the day it starts.",
        )
    moved = system.id is not None and (system.latitude, system.longitude) != (
        body.latitude,
        body.longitude,
    )
    if moved:
        # The stored weather is for the old location. Usage is what the meter measured, so it
        # stays, and the forecast can't learn from it until the new weather arrives.
        db.execute(delete(WeatherRecord).where(WeatherRecord.system_id == system.id))
    for field in ("name", "latitude", "longitude", "timezone", "planning_mode", "data_source"):
        setattr(system, field, getattr(body, field))

    if system.battery is None:
        system.battery = Battery()
    for field, value in body.battery.model_dump().items():
        setattr(system.battery, field, value)

    tariff = body.tariff
    if system.tariff is None:
        system.tariff = Tariff()
    elif _rates_changed(system.tariff, tariff):
        _record_tariff_change(system, body.tariff_effective_from or today)
    for field in ("name", "preset", "standing_charge_pence_per_day"):
        setattr(system.tariff, field, getattr(tariff, field))
    system.tariff.bands = [TariffBand(**band.model_dump()) for band in tariff.bands]

    system.arrays = [SolarArray(**array.model_dump()) for array in body.arrays]

    db.add(system)
    db.commit()
    db.refresh(system)
    if moved:
        background.add_task(refetch_weather, system.id, session_factory, weather_client)
    return system


@router.get("/tariff-history", response_model=list[TariffPeriodOut])
def tariff_history(user: User = Depends(current_user)) -> list[TariffPeriodOut]:
    """Every tariff the system has been on, newest first."""
    system = user.system
    if system is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No system set up yet")
    zone = ZoneInfo(system.timezone)

    def local(moment: datetime) -> date:
        return moment.replace(tzinfo=timezone.utc).astimezone(zone).date()

    rows = list(system.tariff_history)
    current = TariffPeriodOut(
        name=system.tariff.name,
        standing_charge_pence_per_day=system.tariff.standing_charge_pence_per_day,
        valid_from=local(rows[-1].valid_until) if rows else None,
        valid_until=None,
        bands=system.tariff.bands,
    )
    past = [
        TariffPeriodOut(
            name=row.name,
            standing_charge_pence_per_day=row.standing_charge_pence_per_day,
            valid_from=local(rows[i - 1].valid_until) if i else None,
            valid_until=local(row.valid_until),
            bands=row.bands,
        )
        for i, row in enumerate(rows)
    ]
    return [current, *reversed(past)]


@router.get("/tariff-presets", response_model=list[TariffPresetOut])
def tariff_presets() -> list[Preset]:
    """Common UK tariffs to start from. Rates are examples; users edit them to match their bill."""
    return PRESETS
