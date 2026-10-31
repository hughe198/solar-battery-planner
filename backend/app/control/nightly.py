"""Automatic apply: each evening, write the next charge to every inverter whose owner turned
auto-apply on.

`run_once` is one pass. It runs every few minutes in the server when SBP_AUTO_APPLY is set, or
from cron with `python -m app.control.nightly`. A system is due from two hours before its
charge window opens until half an hour before the window closes, and each night is tried once:
if the plan or the write fails, the failure is logged and the inverter keeps the schedule it
had, which repeats nightly.
"""

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.plan import make_plan, next_day
from app.control.service import ControlError, apply, propose, read_schedule
from app.crypto import decrypt
from app.importers.fox_ess import WRITE_GAP_SECONDS, FoxEssClient, FoxEssError
from app.models import (
    ControlAction,
    ControlActionKind,
    ControlStatus,
    ControlTrigger,
    InverterControl,
)
from app.planner.simulator import cycle_for
from app.tariffs import TariffSchedule
from app.weather.open_meteo import OpenMeteoClient, WeatherError

log = logging.getLogger(__name__)

LEAD = timedelta(hours=2)
LATEST_BEFORE_END = timedelta(minutes=30)
PASS_EVERY_SECONDS = 5 * 60


def control_client(api_key: str) -> FoxEssClient:
    """A Fox client paced for write endpoints (one call every 2 seconds)."""
    return FoxEssClient(api_key, min_gap=WRITE_GAP_SECONDS)


def run_once(
    db: Session,
    client_factory: Callable[[str], FoxEssClient],
    weather_client: OpenMeteoClient,
    now: datetime | None = None,
) -> list[ControlAction]:
    now = now or datetime.now(timezone.utc)
    actions = []
    controls = db.scalars(
        select(InverterControl).where(
            InverterControl.enabled.is_(True), InverterControl.auto_apply.is_(True)
        )
    ).all()
    for control in controls:
        system = control.system
        if system.fox_ess is None or system.battery is None:
            continue
        day = next_day(system, now)
        cycle = cycle_for(day, TariffSchedule.from_system(system), system.timezone)
        if cycle.night_start == cycle.night_end:
            continue  # no cheaper overnight rate, so never a charge to apply
        if not cycle.night_start - LEAD <= now < cycle.night_end - LATEST_BEFORE_END:
            continue
        if control.last_auto_day == day:
            continue
        control.last_auto_day = day
        actions.append(_apply_for(db, control, client_factory, weather_client, day))
        db.commit()
    return actions


def _apply_for(db, control, client_factory, weather_client, day) -> ControlAction:
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
        plan = make_plan(db, system, weather_client, day)
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
                    run_once(db, control_client, OpenMeteoClient())
            except Exception:  # keep the loop alive; each failure is also in the audit log
                log.exception("Automatic apply pass failed")
            stop.wait(PASS_EVERY_SECONDS)

    threading.Thread(target=loop, name="auto-apply", daemon=True).start()
    return stop


if __name__ == "__main__":
    from app.database import SessionLocal

    logging.basicConfig(level=logging.INFO)
    with SessionLocal() as session:
        for done in run_once(session, control_client, OpenMeteoClient()):
            log.info("System %s: %s %s", done.system_id, done.status.value, done.message)
