from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.forecast import MAX_DAYS_AHEAD, _system
from app.api.imports import get_weather_client
from app.database import get_db
from app.forecasting.service import HistoryForecaster
from app.forecasting.snapshots import remember_forecast
from app.models import LockedPlan, PlanningMode, System, User
from app.planner import optimiser
from app.planner.base import ChargePlan, Replay, ReplayDetail
from app.planner.charging import learn_drain_from_history, learn_rate_from_history
from app.planner.lead_in import simulate_lead_in, usable_reading
from app.planner.locking import dump_plan, load_plan, lock_at
from app.planner.replay import DEFAULT_DAYS, MAX_DAYS, Replayer
from app.planner.simulator import cycle_for
from app.schemas import ChargePlanOut, PlanLockIn, PlanLockOut, ReplayDetailOut, ReplayOut
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
    """Recommended overnight charge for each planning mode, with the reasoning behind it.

    From the night's lock time on, the plan kept then is returned as it was (`locked_at`);
    before it, the plan follows the latest forecast. A level typed in is always a fresh what-if
    and is never kept."""
    system = _system(user)
    today = datetime.now(ZoneInfo(system.timezone)).date()
    day = day or next_day(system)
    if not today <= day <= today + timedelta(days=MAX_DAYS_AHEAD):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Plans are available from today to {MAX_DAYS_AHEAD} days ahead",
        )
    try:
        if start_soc_percent is None:
            plan, lock = current_plan(db, system, weather_client, day)
        else:
            plan = make_plan(db, system, weather_client, day, start_soc_percent)
            lock = PlanLock(_lock_at(system, day), None)
        db.commit()
        return ChargePlanOut.model_validate(plan).model_copy(
            update={"locks_at": lock.locks_at, "locked_at": lock.locked_at}
        )
    except WeatherError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


@dataclass(frozen=True)
class PlanLock:
    # When the plan locks (or locked), and when it was actually kept: None while it still
    # follows the forecast. Kept late if nothing asked for the plan at the lock time.
    locks_at: datetime
    locked_at: datetime | None


def _lock_at(system: System, day: date) -> datetime:
    return lock_at(system, cycle_for(day, TariffSchedule.from_system(system), system.timezone))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def locked_plan(db: Session, system: System, day: date) -> LockedPlan | None:
    return db.scalar(
        select(LockedPlan).where(LockedPlan.system_id == system.id, LockedPlan.day == day)
    )


def current_plan(
    db: Session,
    system: System,
    weather_client: OpenMeteoClient,
    day: date,
    now: datetime | None = None,
) -> tuple[ChargePlan, PlanLock]:
    """The plan for `day` as it stands: the kept copy once it has locked, otherwise a fresh
    plan, which is kept if the lock time has come and the window hasn't closed. Adds to the
    session; the caller commits. Raises as `make_plan` does."""
    now = now or datetime.now(timezone.utc)
    cycle = cycle_for(day, TariffSchedule.from_system(system), system.timezone)
    at = lock_at(system, cycle)
    kept = locked_plan(db, system, day)
    if kept is not None:
        return load_plan(kept.plan), PlanLock(at, _aware(kept.locked_at))
    plan = make_plan(db, system, weather_client, day)
    if not at <= now < cycle.night_end:
        return plan, PlanLock(at, None)
    db.flush()  # anything else pending, outside the savepoint below
    try:
        with db.begin_nested():
            db.add(LockedPlan(system_id=system.id, day=day, locked_at=now, plan=dump_plan(plan)))
    except IntegrityError:  # another request kept it first; use theirs, so all agree
        kept = locked_plan(db, system, day)
        return load_plan(kept.plan), PlanLock(at, _aware(kept.locked_at))
    return plan, PlanLock(at, now)


def fix_sent_plan(
    db: Session, system: System, day: date, plan: ChargePlan, now: datetime | None = None
) -> None:
    """Keep the plan just sent to the inverter by hand, so what's shown from now on is what the
    inverter has. One already kept, because its lock time came or it was sent before, is
    replaced. Adds to the session; the caller commits."""
    kept = locked_plan(db, system, day)
    now = now or datetime.now(timezone.utc)
    if kept is None:
        db.add(
            LockedPlan(
                system_id=system.id, day=day, locked_at=now, plan=dump_plan(plan), fixed_by="sent"
            )
        )
    else:
        if kept.plan != dump_plan(plan):
            kept.plan, kept.locked_at = dump_plan(plan), now
        kept.fixed_by = "sent"


def release_sent_plan(db: Session, system: System, day: date) -> bool:
    """Let a plan kept because it was sent by hand follow the forecast again, if its lock time
    is still to come. The inverter keeps what was sent until something else is sent. Returns
    whether there was one to let go."""
    kept = locked_plan(db, system, day)
    if kept is None or kept.fixed_by != "sent" or _lock_at(system, day) <= datetime.now(
        timezone.utc
    ):
        return False
    db.delete(kept)
    return True


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
    drain = learn_drain_from_history(db, system)
    if start_soc_percent is not None:
        plan = replace(
            optimiser.plan(system, forecast, start_soc_percent, rate, drain),
            start_soc_source="given",
        )
    elif reading is None:
        plan = optimiser.plan(system, forecast, system.battery.min_soc_percent, rate, drain)
    else:
        wanted = set(lead_starts)
        lead_in = simulate_lead_in(
            system, reading, [i for i in forecast.intervals if i.start in wanted], drain
        )
        opening = lead_in[-1].soc_percent if lead_in else reading.soc_percent
        plan = replace(
            optimiser.plan(system, forecast, opening, rate, drain),
            start_soc_source="measured",
            reading_at=reading.at,
            reading_soc_percent=reading.soc_percent,
            lead_in=lead_in,
        )
    # Kept for looking back on the day; a level the user typed in is only a what-if.
    remember_forecast(
        db,
        system,
        day,
        forecast,
        None if start_soc_percent is not None else plan.recommendations[0].target_soc_percent,
        cycle.night_end,
    )
    return plan


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


@router.get("/replay/{day}", response_model=ReplayDetailOut)
def replay_day(
    day: date,
    mode: PlanningMode | None = Query(default=None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> ReplayDetail:
    """One past day in full: its metered half hours, the battery level the best charge would have
    given, and a review. 404 if the day can't be replayed, for example with missing data."""
    system = _system(user)
    detail = Replayer(db, system).detail(day, mode or system.planning_mode)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That day can't be replayed")
    return detail


def _lock_out(system: System, db: Session) -> PlanLockOut:
    day = next_day(system)
    kept = locked_plan(db, system, day)
    return PlanLockOut(
        mode="set_time" if system.plan_lock_time else "before_window",
        time=system.plan_lock_time.strftime("%H:%M") if system.plan_lock_time else None,
        day=day,
        locks_at=_lock_at(system, day),
        locked_at=_aware(kept.locked_at) if kept else None,
        fixed_by=kept.fixed_by if kept else None,
    )


@router.get("/lock", response_model=PlanLockOut)
def plan_lock(user: User = Depends(current_user), db: Session = Depends(get_db)) -> PlanLockOut:
    """When the next charge's plan locks, and whether it has."""
    return _lock_out(_system(user), db)


@router.put("/lock", response_model=PlanLockOut)
def set_plan_lock(
    body: PlanLockIn, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> PlanLockOut:
    """Choose when plans lock. If the next charge's plan is already kept but the new lock time
    is still to come, it is let go so it follows the forecast again until then. A plan kept
    because it was sent to the inverter stays, as that's what the inverter has."""
    system = _system(user)
    system.plan_lock_time = (
        time.fromisoformat(body.time) if body.mode == "set_time" and body.time else None
    )
    db.flush()
    day = next_day(system)
    if _lock_at(system, day) > datetime.now(timezone.utc):
        db.execute(
            delete(LockedPlan).where(
                LockedPlan.system_id == system.id,
                LockedPlan.day == day,
                LockedPlan.fixed_by != "sent",
            )
        )
    db.commit()
    return _lock_out(system, db)
