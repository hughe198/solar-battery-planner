from dataclasses import replace
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
from app.planner.lead_in import simulate_lead_in, usable_reading
from app.planner.replay import DEFAULT_DAYS, MAX_DAYS, Replayer
from app.planner.simulator import cycle_for
from app.schemas import ChargePlanOut, ReplayOut
from app.security import current_user
from app.tariffs import TariffSchedule
from app.weather.open_meteo import OpenMeteoClient, WeatherError

router = APIRouter(prefix="/api/plan", tags=["plan"])


def next_day(system: System, now: datetime | None = None) -> date:
    """The day the next charge is for: today until this morning's charge window ends."""
    now = (now or datetime.now(ZoneInfo(system.timezone))).astimezone(ZoneInfo(system.timezone))
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
    try:
        return make_plan(db, system, weather_client, day, start_soc_percent)
    except WeatherError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


def make_plan(
    db: Session,
    system: System,
    weather_client: OpenMeteoClient,
    day: date,
    start_soc_percent: float | None = None,
) -> ChargePlan:
    """The plan for `day`, starting from the given level, or else the level predicted from the
    latest imported reading, or else the battery's minimum charge.
    Raises WeatherError when the forecast can't be fetched and ValueError when it can't plan."""
    # The whole cycle, not the calendar day: a window can open before midnight, and the day runs
    # on until the next window opens, which can be after the following midnight.
    cycle = cycle_for(day, TariffSchedule.from_system(system), system.timezone)
    reading = None
    if start_soc_percent is None and system.battery is not None:
        reading = usable_reading(db, system, day, cycle)
    lead_starts = reading.half_hours_until(cycle.night_start) if reading else []
    starts = lead_starts + cycle.night_starts() + cycle.day_starts()
    forecast = HistoryForecaster(db, weather_client).forecast(system, day, starts)
    rate = learn_rate_from_history(db, system)
    if start_soc_percent is not None:
        plan = optimiser.plan(system, forecast, start_soc_percent, rate)
        return replace(plan, start_soc_source="given")
    if reading is None:
        return optimiser.plan(system, forecast, system.battery.min_soc_percent, rate)
    wanted = set(lead_starts)
    lead_in = simulate_lead_in(system, reading, [i for i in forecast.intervals if i.start in wanted])
    opening = lead_in[-1].soc_percent if lead_in else reading.soc_percent
    plan = optimiser.plan(system, forecast, opening, rate)
    return replace(
        plan,
        start_soc_source="measured",
        reading_at=reading.at,
        reading_soc_percent=reading.soc_percent,
        lead_in=lead_in,
    )


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
