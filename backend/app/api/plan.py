from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.forecast import MAX_DAYS_AHEAD, _system
from app.api.imports import get_weather_client
from app.database import get_db
from app.forecasting.service import HistoryForecaster
from app.models import PlanningMode, System, User
from app.planner import optimiser
from app.planner.base import ChargePlan, Replay
from app.planner.charging import learn_rate_from_history
from app.planner.replay import DEFAULT_DAYS, MAX_DAYS, Replayer
from app.planner.simulator import cycle_for
from app.schemas import ChargePlanOut, ReplayOut
from app.security import current_user
from app.tariffs import TariffSchedule
from app.weather.open_meteo import OpenMeteoClient, WeatherError

router = APIRouter(prefix="/api/plan", tags=["plan"])


def next_day(system: System) -> date:
    """The day the next charge is for: today until this morning's charge window ends."""
    now = datetime.now(ZoneInfo(system.timezone))
    cycle = cycle_for(now.date(), TariffSchedule.from_system(system), system.timezone)
    return now.date() if now < cycle.night_end else now.date() + timedelta(days=1)


@router.get("", response_model=ChargePlanOut)
def charge_plan(
    day: date | None = Query(default=None, description="Local day the charge is for"),
    start_soc_percent: float | None = Query(
        default=None, ge=0, le=100, description="Battery level when the cheap window opens"
    ),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    weather_client: OpenMeteoClient = Depends(get_weather_client),
) -> ChargePlan:
    """Recommended overnight charge for each planning mode, with the reasoning behind it."""
    system = _system(user)
    today = datetime.now(ZoneInfo(system.timezone)).date()
    day = day or next_day(system)
    if not today <= day <= today + timedelta(days=MAX_DAYS_AHEAD):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Plans are available from today to {MAX_DAYS_AHEAD} days ahead",
        )
    if start_soc_percent is None:
        start_soc_percent = system.battery.min_soc_percent
    try:
        forecast = HistoryForecaster(db, weather_client).forecast(system, day)
    except WeatherError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    try:
        rate = learn_rate_from_history(db, system)
        return optimiser.plan(system, forecast, start_soc_percent, rate)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


@router.get("/replay", response_model=ReplayOut)
def replay(
    days: int = Query(default=DEFAULT_DAYS, ge=1, le=MAX_DAYS),
    mode: PlanningMode | None = Query(default=None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Replay:
    """Past days replayed from the charge actually made: simulator accuracy, and what the
    best charge with hindsight would have saved. Needs imported state of charge."""
    system = _system(user)
    return Replayer(db, system).replay(mode or system.planning_mode, days)
