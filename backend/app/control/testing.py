"""A step-by-step write test, so a user can build trust in the planner's inverter writes
before letting it change anything that matters.

Each step does a little more than the one before, reads the result back and is logged:

1. Read: read the schedule. Nothing is written. It is saved as the baseline for the test.
2. Write back unchanged: write the schedule exactly as read. Proves the write call works
   with nothing meant to change.
3. Add a test period: add a 10-minute, low-power force charge hours away, so it won't run
   before step 4 takes it out.
4. Remove it: take the test period out and check the schedule matches the baseline exactly.
   Passing this unlocks applying the plan.
5. Watch a live charge (optional): a 10-minute low-power charge starting in a few minutes, to
   watch in the Fox app.
6. Remove the live test period and check the schedule matches the baseline again.

Test periods are recorded, so "Remove test periods" and "Restore" always take them out. If one
is left behind it is at most a 10-minute charge at the test power each day.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.control.schedule import FORCE_CHARGE, Period, Schedule, without
from app.control.service import ControlError, _log, _run_write, read_schedule
from app.importers.fox_ess import FoxEssClient, FoxEssError
from app.models import (
    ControlAction,
    ControlActionKind,
    ControlStatus,
    ControlTrigger,
    InverterControl,
    System,
)

TEST_MINUTES = 10
TEST_POWER_W = 500
FUTURE_FROM = timedelta(hours=3)
LIVE_FROM = timedelta(minutes=5)
UNLOCKS_APPLY = 4

STEPS = {
    1: "Read your schedule (nothing is written)",
    2: "Write your schedule back unchanged",
    3: "Add a test period hours from now",
    4: "Remove it and check the schedule is as it was",
    5: "Optional: watch a 10-minute low-power charge",
    6: "Remove the live test period",
}


def _test_period(schedule: Schedule, tz: str, earliest: datetime, search: timedelta) -> Period:
    """A 10-minute force charge at the test power, in the first free gap from `earliest` that
    doesn't cross midnight."""
    zone = ZoneInfo(tz)
    power = schedule.clamp("fdpwr", TEST_POWER_W)
    soc = schedule.clamp("fdsoc", 100)
    min_soc = schedule.clamp("minsocongrid", schedule.ranges["minsocongrid"][0])
    moment = earliest.astimezone(zone)
    moment = moment.replace(second=0, microsecond=0) + timedelta(minutes=5 - moment.minute % 5)
    end = earliest + search
    while moment < end:
        start = moment.hour * 60 + moment.minute
        if start + TEST_MINUTES <= 24 * 60:
            candidate = Period(
                start,
                start + TEST_MINUTES - 1,
                FORCE_CHARGE,
                fd_soc=soc,
                fd_pwr=power,
                min_soc_on_grid=min_soc,
            )
            if not any(candidate.overlaps(p) for p in schedule.periods):
                return candidate
        moment += timedelta(minutes=5)
    raise ControlError("There's no free 10-minute gap in your schedule for a test period.")


def run_step(
    db: Session,
    system: System,
    control: InverterControl,
    client: FoxEssClient,
    step: int,
    now: datetime,
) -> ControlAction:
    if step not in STEPS:
        raise ControlError("Unknown test step")
    if step > control.test_level + 1 and not (step == 6 and control.test_level >= 4):
        raise ControlError(f"Pass step {control.test_level + 1} first.")
    sn = system.fox_ess.device_sn

    def log(status: ControlStatus, message: str, **values) -> ControlAction:
        return _log(
            db,
            system,
            ControlActionKind.test,
            ControlTrigger.manual,
            status,
            f"Step {step}: {message}",
            **values,
        )

    try:
        schedule = read_schedule(client, sn)
    except FoxEssError as exc:
        return log(ControlStatus.failed, f"Couldn't read the inverter's schedule: {exc}")

    action: ControlAction
    if step == 1:
        control.test_baseline = schedule.to_snapshot()
        if control.original_schedule is None:
            control.original_schedule = schedule.to_snapshot()
            control.original_saved_at = now
        periods = ", ".join(f"{p.work_mode} {p.label()}" for p in schedule.periods) or "none"
        action = log(
            ControlStatus.ok,
            f"Read your schedule: it is {'on' if schedule.enabled else 'off'}, with periods: "
            f"{periods}. Nothing was written.",
            read_back=schedule.to_snapshot(),
        )
    elif step == 2:
        action = _run_write(
            db,
            system,
            client,
            ControlActionKind.test,
            ControlTrigger.manual,
            schedule.periods,
            schedule.enabled,
            "Step 2: your schedule was written back unchanged and read back the same.",
        )
    elif step in (3, 5):
        if not schedule.enabled and schedule.periods:
            return log(
                ControlStatus.failed,
                "Your schedule is switched off but has periods in it. Adding a test period "
                "would switch them on too, so nothing was written.",
            )
        try:
            if step == 3:
                test = _test_period(
                    schedule, system.timezone, now + FUTURE_FROM, timedelta(hours=18)
                )
            else:
                test = _test_period(
                    schedule, system.timezone, now + LIVE_FROM, timedelta(minutes=30)
                )
        except ControlError as exc:
            return log(ControlStatus.failed, str(exc))
        after = sorted([*schedule.periods, test], key=lambda p: p.start)
        when = (
            "It won't run before step 4 removes it."
            if step == 3
            else (
                "Watch the battery in the Fox app: it should charge from the grid at about "
                f"{test.fd_pwr:g} W, then stop. Run step 6 afterwards."
            )
        )
        action = _run_write(
            db,
            system,
            client,
            ControlActionKind.test,
            ControlTrigger.manual,
            after,
            True,
            f"Step {step}: test charge period {test.label()} at {test.fd_pwr:g} W added and "
            f"read back as written. {when}",
        )
        if action.status != ControlStatus.failed:
            control.test_groups = [*(control.test_groups or []), test.to_fox()]
    else:
        ours = [Period.from_fox(g) for g in control.test_groups or []]
        after, removed = without(schedule.periods, ours)
        if not removed:
            control.test_groups = []
            action = log(ControlStatus.failed, "No test period was found on the inverter.")
        else:
            baseline = Schedule.from_fox(control.test_baseline or schedule.to_snapshot())
            action = _run_write(
                db,
                system,
                client,
                ControlActionKind.test,
                ControlTrigger.manual,
                after,
                baseline.enabled,
                f"Step {step}: test period removed; your schedule reads back exactly as it was.",
            )
            if action.status != ControlStatus.failed:
                control.test_groups = []
            if action.status == ControlStatus.ok:
                back = Schedule.from_fox(action.read_back or {})
                if back.periods != baseline.periods or back.enabled != baseline.enabled:
                    action.status = ControlStatus.mismatch
                    action.message = (
                        f"Step {step}: the test period was removed, but the schedule doesn't "
                        "match the one read in step 1. Check it in the Fox app, or use Restore."
                    )

    if action.status == ControlStatus.ok:
        control.test_level = max(control.test_level, step)
    return action
