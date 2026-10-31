from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Battery, SolarArray, System, Tariff, User, WeatherRecord
from app.schemas import SystemIn, SystemOut
from app.security import current_user

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("", response_model=SystemOut)
def get_system(user: User = Depends(current_user)) -> System:
    if user.system is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No system set up yet")
    return user.system


@router.put("", response_model=SystemOut)
def save_system(
    body: SystemIn, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> System:
    """Create or replace the signed-in user's system."""
    system = user.system or System(user=user)
    if system.id is not None and (system.latitude, system.longitude) != (
        body.latitude,
        body.longitude,
    ):
        # Weather is fetched for the old location; drop it so the next import refetches.
        db.execute(delete(WeatherRecord).where(WeatherRecord.system_id == system.id))
    for field in ("name", "latitude", "longitude", "timezone", "planning_mode", "data_source"):
        setattr(system, field, getattr(body, field))

    if system.battery is None:
        system.battery = Battery()
    for field, value in body.battery.model_dump().items():
        setattr(system.battery, field, value)

    if system.tariff is None:
        system.tariff = Tariff()
    for field, value in body.tariff.model_dump().items():
        setattr(system.tariff, field, value)

    system.arrays = [SolarArray(**array.model_dump()) for array in body.arrays]

    db.add(system)
    db.commit()
    db.refresh(system)
    return system
