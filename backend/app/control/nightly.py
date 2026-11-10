"""Automatic apply: each evening, write the next charge to every inverter whose owner turned
auto-apply on, or asked for tonight's plan to be sent at the last minute.

`run_once` is one pass. It runs every minute in the server when SBP_AUTO_APPLY is set, or from
cron with `python -m app.control.nightly`. A system is due from the moment its plan locks (see
app.planner.locking: just before the charge window opens unless the owner set a time) until
half an hour before the window closes, and the locked plan is what is sent. Each night is tried
once: if the plan or the write fails, the failure is logged and the inverter keeps the schedule
it had, which repeats nightly.

`lock_due` keeps the plan of every system whose lock time has come, so a plan locks on time
even when nobody looks at it; it never touches an inverter.
"""

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.plan import current_plan, locked_plan, next_day
from app.config import settings
from app.control.service import ControlError, apply, propose, read_schedule
from app.crypto import decrypt
from app.importers.fox_ess import WRITE_GAP_SECONDS, FoxEssClient, FoxEssError
from app.models import (
    ControlAction,
    ControlActionKind,
    ControlStatus,
    ControlTrigger,
    InverterControl,
    System,
)
from app.planner.locking import lock_at
from app.planner.simulator import cycle_for
from app.tariffs import TariffSchedule
from app.weather.open_meteo import OpenMeteoClient, WeatherError

log = logging.getLogger(__name__)

LATEST_BEFORE_END = timedelta(minutes=30)
# Often, so "at the last minute" is within a minute of the lock time. A pass that has nothing
# due only reads the database.
PASS_EVERY_SECONDS = 60


def control_client(api_key: str) -> FoxEssClient:
    """A Fox client paced for write endpoints (one call every 2 seconds)."""
    return FoxEssClient(api_key, min_gap=WRITE_GAP_SECONDS)


def run_once(
    db: Session,
    client_factory: Callable[[str], FoxEssClient],
    weather_client: OpenMeteoClient,
    now: datetime | None = None,
) -> list[ControlAction]:
    if not settings.inverter_writes:
        log.info("Writes to inverters are switched off (SBP_INVERTER_WRITES), so nothing to do")
        return []
    now = now or datetime.now(timezone.utc)
    actions = []
    controls = db.scalars(
        select(InverterControl).where(
            InverterControl.enabled.is_(True),
            or_(InverterControl.auto_apply.is_(True), InverterControl.last_minute_day.is_not(None)),
        )
    ).all()
    for control in controls:
        system = control.system
        if system.fox_ess is None or system.battery is None:
            continue
        day = next_day(system, now)
        if control.last_minute_day is not None and control.last_minute_day < day:
            control.last_minute_day = None  # asked for a night that has gone
            db.commit()
        last_minute = control.last_minute_day == day
        if not (control.auto_apply or last_minute):
            continue
        cycle = cycle_for(day, TariffSchedule.from_system(system), system.timezone)
        if cycle.night_start == cycle.night_end:
            continue  # no cheaper overnight rate, so never a charge to apply
        if not lock_at(system, cycle) <= now < cycle.night_end - LATEST_BEFORE_END:
            continue
        if control.last_auto_day == day:
            continue
        control.last_auto_day = day
        control.last_minute_day = None
        actions.append(_apply_for(db, control, client_factory, weather_client, day, now))
        db.commit()
    return actions


def lock_due(
    db: Session, weather_client: OpenMeteoClient, now: datetime | None = None
) -> list[System]:
    """Keep the next plan of every system whose lock time has come and whose window hasn't
    closed. Returns the systems locked in this pass. A plan that can't be made is tried again
    on the next pass."""
    now = now or datetime.now(timezone.utc)
    locked = []
    for system in db.scalars(select(System)).all():
        if system.battery is None or system.tariff is None:
            continue
        day = next_day(system, now)
        cycle = cycle_for(day, TariffSchedule.from_system(system), system.timezone)
        if not lock_at(system, cycle) <= now < cycle.night_end:
            continue
        if locked_plan(db, system, day) is not None:
            continue
        try:
            current_plan(db, system, weather_client, day, now)
            db.commit()
            locked.append(system)
        except (WeatherError, ValueError) as exc:
            db.rollback()
            log.warning("Couldn't lock the plan for system %s: %s", system.id, exc)
        except Exception:  # one system's fault mustn't stop the others locking
            db.rollback()
            log.exception("Couldn't lock the plan for system %s", system.id)
    return locked


def _apply_for(db, control, client_factory, weather_client, day, now) -> ControlAction:
    system = control.system

    def failed(message: str) -> ControlAction:
        action = ControlAction(
            system=system,
            kind=ControlActionKind.apply,
            trigger=ControlTrigger.auto,
            status=ControlStatus.failed,
            day=day,
            message=f"{message} The inverter keeps the schedule it had.",
        )
        db.add(action)
        return action

    try:
        plan, _ = current_plan(db, system, weather_client, day, now)
    except (WeatherError, ValueError) as exc:
        return failed(f"Couldn't plan the charge: {exc}.")
    client = client_factory(decrypt(system.fox_ess.api_key_encrypted))
    try:
        proposal = propose(system, control, plan, read_schedule(client, system.fox_ess.device_sn))
    except FoxEssError as exc:
        return failed(f"Couldn't read the inverter's schedule: {exc}.")
    except ControlError as exc:
        return failed(str(exc))
    return apply(db, system, control, client, proposal, ControlTrigger.auto)


def start_loop(session_factory: sessionmaker[Session]) -> threading.Event:
    """Run a pass every few minutes in a background thread until the event is set."""
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                with session_factory() as db:
                    weather = OpenMeteoClient()
                    lock_due(db, weather)
                    run_once(db, control_client, weather)
            except Exception:  # keep the loop alive; each failure is also in the audit log
                log.exception("Automatic apply pass failed")
            stop.wait(PASS_EVERY_SECONDS)

    threading.Thread(target=loop, name="auto-apply", daemon=True).start()
    return stop


if __name__ == "__main__":
    from app.database import SessionLocal

    logging.basicConfig(level=logging.INFO)
    with SessionLocal() as session:
        lock_due(session, OpenMeteoClient())
        for done in run_once(session, control_client, OpenMeteoClient()):
            log.info("System %s: %s %s", done.system_id, done.status.value, done.message)
