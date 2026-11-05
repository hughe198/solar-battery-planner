"""Expected usage: the user marks days as Away, Low, Normal or High, and nights the electric car
will charge. The forecast, plan and automation all read these (see app.forecasting.usage)."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.forecast import _system
from app.database import get_db
from app.forecasting.service import fit, load_history, load_marks, usage_profile
from app.models import System, UsageExpectation, UsageLevel, User
from app.schemas import (
    UsageExpectationIn,
    UsageExpectationOut,
    UsageExpectationsOut,
    UsageProfileOut,
    UsageRangeIn,
)
from app.security import current_user

router = APIRouter(prefix="/api/usage-expectations", tags=["usage"])

# Marks can be made up to a year ahead; past days can be marked too, such as a holiday already
# in the history, so the model stops learning from it.
MAX_DAYS_AHEAD = 366


def _today(system: System) -> date:
    return datetime.now(ZoneInfo(system.timezone)).date()


def _check_day(system: System, day: date) -> None:
    if day > _today(system) + timedelta(days=MAX_DAYS_AHEAD):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Days can be marked up to a year ahead"
        )


def _row(db: Session, system: System, day: date) -> UsageExpectation | None:
    return db.scalar(
        select(UsageExpectation).where(
            UsageExpectation.system_id == system.id, UsageExpectation.day == day
        )
    )


def _set(
    db: Session, system: System, day: date, level: UsageLevel, car_kwh: float | None
) -> None:
    row = _row(db, system, day)
    car_kwh = car_kwh or None
    if level is UsageLevel.normal and car_kwh is None:
        if row is not None:
            db.delete(row)
        return
    if row is None:
        row = UsageExpectation(system_id=system.id, day=day)
        db.add(row)
    row.level = level
    row.car_kwh = car_kwh


@router.get("", response_model=UsageExpectationsOut)
def list_expectations(
    start: date | None = Query(default=None, description="First local day; default 60 days ago"),
    end: date | None = Query(default=None, description="Last local day; default a year ahead"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> UsageExpectationsOut:
    """Marked days in a range, and the home's figures behind each option."""
    system = _system(user)
    today = _today(system)
    start = start or today - timedelta(days=60)
    end = end or today + timedelta(days=MAX_DAYS_AHEAD)
    rows = db.scalars(
        select(UsageExpectation)
        .where(
            UsageExpectation.system_id == system.id,
            UsageExpectation.day >= start,
            UsageExpectation.day <= end,
        )
        .order_by(UsageExpectation.day)
    ).all()
    marks = load_marks(db, system)
    history = load_history(db, system, marks)
    profile = usage_profile(system, fit(system, history), history)
    return UsageExpectationsOut(
        days=[UsageExpectationOut.model_validate(r) for r in rows],
        profile=UsageProfileOut(
            base_daily_kwh=(
                round(profile.base_daily_kwh, 2) if profile.base_daily_kwh is not None else None
            ),
            low_factor=round(profile.low_factor, 3),
            high_factor=round(profile.high_factor, 3),
            learned=profile.learned,
        ),
        has_heat_pump=system.has_heat_pump,
        has_electric_car=system.has_electric_car,
        car_charger_kw=system.car_charger_kw,
    )


@router.put("/{day}", response_model=UsageExpectationOut)
def set_day(
    day: date,
    body: UsageExpectationIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> UsageExpectationOut:
    """Set one day's level and car charge. Normal with no car charge clears it."""
    system = _system(user)
    _check_day(system, day)
    if body.car_kwh and not system.has_electric_car:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Turn on the electric car setting on the System page first",
        )
    _set(db, system, day, body.level, body.car_kwh)
    db.commit()
    return UsageExpectationOut(day=day, level=body.level, car_kwh=body.car_kwh or None)


@router.put("", response_model=list[UsageExpectationOut])
def set_range(
    body: UsageRangeIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[UsageExpectationOut]:
    """Set the same level on every day from start to end, such as a holiday. Car charges
    already planned on those days are kept."""
    system = _system(user)
    _check_day(system, body.end)
    out = []
    day = body.start
    while day <= body.end:
        row = _row(db, system, day)
        car = row.car_kwh if row is not None else None
        _set(db, system, day, body.level, car)
        out.append(UsageExpectationOut(day=day, level=body.level, car_kwh=car))
        day += timedelta(days=1)
    db.commit()
    return out
