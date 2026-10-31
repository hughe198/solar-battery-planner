"""Write the planner's recommendation to a Fox ESS inverter, for users who have opted in.

Every write follows the same steps: read the inverter's schedule, work out the new one, write
it, read it back and compare, and log what was sent and what came back. Before the first write
the schedule is saved so it can be put back exactly.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from math import ceil
from typing import Any

from sqlalchemy.orm import Session

from app.control.schedule import (
    Period,
    Schedule,
    charge_periods,
    fingerprint,
    matches,
    merge,
    without,
)
from app.importers.fox_ess import FoxEssClient, FoxEssError
from app.models import (
    ControlAction,
    ControlActionKind,
    ControlStatus,
    ControlTrigger,
    InverterControl,
    PlanningMode,
    System,
)
from app.planner.base import ChargePlan


class ControlError(Exception):
    """The plan can't be written as asked; nothing was sent to the inverter."""


@dataclass
class Proposal:
    """Exactly what applying the plan would write, and why."""

    day: date
    mode: PlanningMode
    planned_soc_percent: float
    charge_kwh: float
    start: datetime | None
    stop: datetime | None
    target_soc_percent: float | None
    power_w: float | None
    min_soc_on_grid: float | None
    schedule: Schedule
    added: list[Period]
    removed: list[Period]
    after: list[Period]
    notes: list[str] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.after)


def propose(
    system: System, control: InverterControl, plan: ChargePlan, schedule: Schedule
) -> Proposal:
    """The schedule that puts the chosen mode's recommendation on the inverter."""
    rec = plan.recommendations[0]
    battery = system.battery
    notes: list[str] = []
    previous = [Period.from_fox(g) for g in control.written_groups or []]
    current, replaced = without(schedule.periods, previous)

    start, stop = rec.force_charge_start, rec.force_charge_stop
    target = power = min_soc = None
    added: list[Period] = []
    if rec.charge_kwh > 0 and start is not None and stop is not None and stop > start:
        target = schedule.clamp("fdsoc", ceil(rec.target_soc_percent))
        power = schedule.clamp("fdpwr", round(battery.max_charge_kw * 1000))
        min_soc = schedule.clamp("minsocongrid", round(battery.min_soc_percent))
        added = charge_periods(start, stop, system.timezone, target, power, min_soc)
        if "maxsoc" in schedule.reported:
            top = schedule.ranges["maxsoc"][1]
            added = [Period(**{**p.__dict__, "raw": {"maxSoc": int(top)}}) for p in added]
            notes.append(f"Maximum charge is written as {top:g}% so it doesn't stop the charge.")
        if target != ceil(rec.target_soc_percent):
            notes.append(
                f"The inverter accepts charge levels from "
                f"{schedule.ranges['fdsoc'][0]:g}%, so {target:g}% is written."
            )
        if min_soc != round(battery.min_soc_percent):
            low = schedule.ranges["minsocongrid"][0]
            notes.append(
                f"Your minimum charge is {battery.min_soc_percent:g}%, but the inverter's schedule "
                f"accepts {low:g}% or more, so the charge periods use {min_soc:g}%."
            )
        if not rec.force_charge_reaches_target:
            notes.append("The charge may not reach the target before the cheap rate ends.")
    else:
        notes.append("No charge is needed, so no force charge period is written.")

    after, overlapping = merge(current, added)
    removed = replaced + overlapping
    if len(after) > schedule.max_groups:
        raise ControlError(
            f"The inverter's schedule holds at most {schedule.max_groups} periods and this "
            f"would need {len(after)}. Remove a period in the Fox app and try again."
        )
    if added:
        notes.append(
            "Fox schedules repeat every day, so this charge also runs on later nights until "
            "it is changed or removed."
        )
    if added and not schedule.enabled and current:
        notes.append(
            "Your inverter's schedule is switched off. Switching it on for the charge also "
            "turns on your other periods in it: "
            + ", ".join(f"{p.work_mode} {p.label()}" for p in current)
            + "."
        )
    if removed and any(p.work_mode != "ForceCharge" for p in removed):
        notes.append("Some of your own schedule periods overlap the charge and are replaced.")
    return Proposal(
        day=plan.day,
        mode=rec.mode,
        planned_soc_percent=rec.target_soc_percent,
        charge_kwh=rec.charge_kwh,
        start=start if added else None,
        stop=stop if added else None,
        target_soc_percent=target,
        power_w=power,
        min_soc_on_grid=min_soc,
        schedule=schedule,
        added=added,
        removed=removed,
        after=after,
        notes=notes,
    )


def read_schedule(client: FoxEssClient, sn: str) -> Schedule:
    return Schedule.from_fox(client.scheduler(sn))


def _write(client: FoxEssClient, sn: str, periods: list[Period], enable: bool) -> None:
    """Write the periods, then switch the schedule on or off. With no periods the schedule is
    switched off, which returns the inverter to its own work mode."""
    groups = [p.to_fox() for p in periods]
    if groups:
        client.set_scheduler_groups(sn, groups)
    client.set_scheduler_enabled(sn, bool(enable and groups))


def _verify(client: FoxEssClient, sn: str, periods: list[Period], enable: bool):
    back = read_schedule(client, sn)
    problems = matches(periods, back.periods) if enable and periods else []
    expected_on = bool(enable and periods)
    if back.enabled != expected_on:
        problems.append(
            f"The inverter reports its schedule is {'on' if back.enabled else 'off'}, "
            f"not {'on' if expected_on else 'off'}."
        )
    return back, problems


def _log(
    db: Session,
    system: System,
    kind: ControlActionKind,
    trigger: ControlTrigger,
    status: ControlStatus,
    message: str,
    **values: Any,
) -> ControlAction:
    action = ControlAction(
        system=system, kind=kind, trigger=trigger, status=status, message=message[:1000], **values
    )
    db.add(action)
    return action


def _run_write(
    db: Session,
    system: System,
    client: FoxEssClient,
    kind: ControlActionKind,
    trigger: ControlTrigger,
    periods: list[Period],
    enable: bool,
    done: str,
    **values: Any,
) -> ControlAction:
    sn = system.fox_ess.device_sn
    sent = {"groups": [p.to_fox() for p in periods], "enable": int(bool(enable and periods))}
    try:
        _write(client, sn, periods, enable)
        back, problems = _verify(client, sn, periods, enable)
    except FoxEssError as exc:
        return _log(
            db,
            system,
            kind,
            trigger,
            ControlStatus.failed,
            f"Fox ESS refused or failed the change: {exc}. Check the schedule in the Fox app.",
            sent=sent,
            **values,
        )
    status = ControlStatus.mismatch if problems else ControlStatus.ok
    message = " ".join(problems) if problems else done
    return _log(
        db,
        system,
        kind,
        trigger,
        status,
        message,
        sent=sent,
        read_back=back.to_snapshot() | {"enable": int(back.enabled)},
        **values,
    )


def apply(
    db: Session,
    system: System,
    control: InverterControl,
    client: FoxEssClient,
    proposal: Proposal,
    trigger: ControlTrigger,
) -> ControlAction:
    """Write a proposal. Saves the original schedule first if this is the first write."""
    if control.original_schedule is None:
        control.original_schedule = proposal.schedule.to_snapshot()
        control.original_saved_at = datetime.now(timezone.utc)
    if proposal.added:
        done = (
            f"Force charge to {proposal.target_soc_percent:g}% written for "
            f"{', '.join(p.label() for p in proposal.added)} and read back as written."
        )
    else:
        done = "No charge needed; the planner's force charge periods were removed."
    enable = bool(proposal.after) and (proposal.schedule.enabled or bool(proposal.added))
    action = _run_write(
        db,
        system,
        client,
        ControlActionKind.apply,
        trigger,
        proposal.after,
        enable,
        done,
        day=proposal.day,
        target_soc_percent=proposal.target_soc_percent,
    )
    if action.status != ControlStatus.failed:
        control.written_groups = [p.to_fox() for p in proposal.added]
    return action


def clear(
    db: Session, system: System, control: InverterControl, client: FoxEssClient
) -> ControlAction:
    """Take the planner's force charge and test periods out of the schedule, keeping
    everything else."""
    ours = [
        Period.from_fox(g) for g in [*(control.written_groups or []), *(control.test_groups or [])]
    ]
    try:
        schedule = read_schedule(client, system.fox_ess.device_sn)
    except FoxEssError as exc:
        return _log(
            db,
            system,
            ControlActionKind.clear,
            ControlTrigger.manual,
            ControlStatus.failed,
            f"Couldn't read the inverter's schedule: {exc}",
        )
    after, removed = without(schedule.periods, ours)
    if not removed:
        control.written_groups = []
        control.test_groups = []
        return _log(
            db,
            system,
            ControlActionKind.clear,
            ControlTrigger.manual,
            ControlStatus.skipped,
            "The inverter has no force charge periods from the planner, so nothing was changed.",
        )
    action = _run_write(
        db,
        system,
        client,
        ControlActionKind.clear,
        ControlTrigger.manual,
        after,
        schedule.enabled,
        "The planner's force charge periods were removed.",
    )
    if action.status != ControlStatus.failed:
        control.written_groups = []
        control.test_groups = []
    return action


def restore(
    db: Session, system: System, control: InverterControl, client: FoxEssClient
) -> ControlAction:
    """Put back the schedule saved before the planner first wrote to the inverter."""
    if control.original_schedule is None:
        return _log(
            db,
            system,
            ControlActionKind.restore,
            ControlTrigger.manual,
            ControlStatus.skipped,
            "The planner hasn't changed the inverter, so there is nothing to restore.",
        )
    original = Schedule.from_fox(control.original_schedule)
    action = _run_write(
        db,
        system,
        client,
        ControlActionKind.restore,
        ControlTrigger.manual,
        original.periods,
        original.enabled,
        "Your original schedule was put back and read back as saved.",
    )
    if action.status != ControlStatus.failed:
        control.written_groups = []
        control.test_groups = []
    return action
