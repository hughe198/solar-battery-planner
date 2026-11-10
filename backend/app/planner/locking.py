"""When a charge night's plan stops changing.

Until its lock time the plan is remade from the latest forecast and battery reading each time
it is asked for, so it moves by a few percent through the day. At the lock time it is made
one last time and kept (`LockedPlan`); from then on that copy is what is shown and what is
sent to the inverter, whether by hand, at the last minute or by automatic apply.

The lock time is the user's choice: just before the cheap window opens (the default, so the
plan uses the freshest forecast and reading), or a set local time such as 21:00.
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter

from app.models import System
from app.planner.base import ChargePlan
from app.planner.simulator import Cycle, cycle_for
from app.tariffs import TariffSchedule

# "Just before the window": time enough for a pass of the automatic apply (every minute) and
# its few paced calls to Fox to finish before the window opens.
BEFORE_WINDOW = timedelta(minutes=10)
# A set time later than that is taken from the day before, but never more than this before
# the window; past it, the plan locks just before the window instead.
LONGEST_LEAD = timedelta(hours=18)

_plan = TypeAdapter(ChargePlan)


def lock_at(system: System, cycle: Cycle) -> datetime:
    """When the plan for `cycle` locks, in UTC."""
    latest = cycle.night_start - BEFORE_WINDOW
    if system.plan_lock_time is None:
        return latest
    return set_time_before(system.plan_lock_time, latest, system.timezone)


def set_time_before(at: time, latest: datetime, tz: str) -> datetime:
    """The last local `at` no later than `latest`, or `latest` if that is too long before."""
    zone = ZoneInfo(tz)
    local = latest.astimezone(zone)
    candidate = datetime.combine(local.date(), at, zone)
    if candidate > local:
        candidate = datetime.combine(local.date() - timedelta(days=1), at, zone)
    candidate = candidate.astimezone(timezone.utc)
    return latest if latest - candidate > LONGEST_LEAD else candidate


def lock_at_for_day(system: System, day: date) -> datetime:
    return lock_at(system, cycle_for(day, TariffSchedule.from_system(system), system.timezone))


def dump_plan(plan: ChargePlan) -> dict:
    return _plan.dump_python(plan, mode="json")


def load_plan(data: dict) -> ChargePlan:
    return _plan.validate_python(data)
