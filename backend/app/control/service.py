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
    FORCE_DISCHARGE,
    Period,
    Schedule,
    charge_periods,
    fingerprint,
    matches,
    merge,
    without,
)
from app.importers.fox_ess import (
    SCHEDULER_FLAG_PATH,
    SCHEDULER_PATH,
    SCHEDULER_SET_PATH,
    FoxEssClient,
    FoxEssError,
    FoxEssWritesBlocked,
    scheduler_set_body,
)
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


# The lowest minimum charge the planner writes, whatever the inverter accepts. The owner chose
# to keep it at 10% for safety (1 November 2026), as the true lower limit is uncertain.
MIN_SOC_FLOOR = 10.0


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
    # The battery is held (the house on the grid) for this span before the charge.
    hold_start: datetime | None
    hold_stop: datetime | None
    # Before a free window the battery is exported down to this level.
    discharge_start: datetime | None
    discharge_stop: datetime | None
    discharge_soc_percent: float | None
    discharge_power_w: float | None
    target_soc_percent: float | None
    power_w: float | None
    min_soc_on_grid: float | None
    schedule: Schedule
    added: list[Period]
    removed: list[Period]
    after: list[Period]
    notes: list[str] = field(default_factory=list)
    # What's left of the user's periods that the new ones partly overlap.
    trimmed: list[Period] = field(default_factory=list)
    max_soc: float | None = None
    # What's written, in words, e.g. "hold 00:30–03:40; force charge to 80% 03:40–05:30".
    summary: str = ""

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.after)

    @property
    def enable_after(self) -> bool:
        """Whether the schedule is switched on after applying: on for a new charge, otherwise
        as it was."""
        return bool(self.after) and (self.schedule.enabled or bool(self.added))


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
    hold_start, hold_stop = getattr(rec, "hold_start", None), getattr(rec, "hold_stop", None)
    target = None
    added: list[Period] = []
    power = schedule.clamp("fdpwr", round(battery.max_charge_kw * 1000))
    wanted_min = max(round(battery.min_soc_percent), MIN_SOC_FLOOR)
    min_soc = schedule.clamp("minsocongrid", wanted_min)
    # Fox: fdSoc "must be equal or greater than minSocOnGrid" (D6). No forced period stops below
    # the minimum written with it, nor below what the inverter accepts.
    floor = max(schedule.ranges["fdsoc"][0], min_soc)
    # A setting left out of a V3 period keeps an old value, so every setting a stage depends on
    # is sent, maxSoc included when the inverter has one, at its top so it never stops a
    # charge (D7).
    max_soc = schedule.ranges["maxsoc"][1] if "maxsoc" in schedule.reported else None

    def stop_level(level: float) -> float:
        return min(max(level, floor), schedule.ranges["fdsoc"][1])

    hold: list[Period] = []
    if hold_start is not None and hold_stop is not None and hold_stop > hold_start:
        # A forced charge to the lowest level allowed charges nothing once the battery is above
        # it, but keeps the house on the grid: the battery is held.
        hold = charge_periods(
            hold_start, hold_stop, system.timezone, floor, power, min_soc, max_soc=max_soc
        )
        notes.append(
            "Until the charge, the battery is held: a forced charge to "
            f"{floor:g}% keeps the house on the cheap grid without charging, so stored energy "
            "isn't used overnight only to be bought back."
        )
    if rec.charge_kwh > 0 and start is not None and stop is not None and stop > start:
        target = stop_level(ceil(rec.target_soc_percent))
        added = charge_periods(
            start, stop, system.timezone, target, power, min_soc, max_soc=max_soc
        )
        if target != ceil(rec.target_soc_percent):
            notes.append(
                f"The charge level is written as {target:g}%, as it must be at least the "
                f"{floor:g}% minimum and no more than {schedule.ranges['fdsoc'][1]:g}%."
            )
        if not rec.force_charge_reaches_target:
            notes.append("The charge may not reach the target before the cheap rate ends.")
    else:
        notes.append("No charge is needed, so no force charge period is written.")
    drain: list[Period] = []
    drain_start = getattr(rec, "discharge_start", None)
    drain_stop = getattr(rec, "discharge_stop", None)
    drain_soc = drain_power = None
    if drain_start is not None and drain_stop is not None and drain_stop > drain_start:
        wanted = ceil(getattr(rec, "discharge_to_percent", None) or battery.min_soc_percent)
        drain_soc = stop_level(wanted)
        drain_power = schedule.clamp("fdpwr", round(battery.max_discharge_kw * 1000))
        drain = charge_periods(
            drain_start,
            drain_stop,
            system.timezone,
            drain_soc,
            drain_power,
            min_soc,
            work_mode=FORCE_DISCHARGE,
            max_soc=max_soc,
        )
        notes.append(
            f"Before the free window the battery is force discharged to {drain_soc:g}% at "
            f"{drain_power:g} W, exporting what's stored; the free window refills it."
        )
        if drain_soc != wanted:
            notes.append(
                f"The discharge stops at {drain_soc:g}% rather than {wanted:g}%, as it can't go "
                "below the minimum charge."
            )
    added = drain + hold + added
    for i, a in enumerate(added):
        for b in added[i + 1 :]:
            if a.overlaps(b):
                # D8: stages should meet, never overlap. Refuse rather than guess.
                raise ControlError(
                    f"The {a.work_mode} {a.label()} and {b.work_mode} {b.label()} stages overlap, "
                    "so nothing can be written. This is a bug in the planner."
                )
    if added and min_soc != round(battery.min_soc_percent):
        notes.append(
            f"Your minimum charge is {battery.min_soc_percent:g}%, but the planner's periods "
            f"use {min_soc:g}%, the lowest it writes."
        )
    if added and max_soc is not None:
        notes.append(f"Maximum charge is written as {max_soc:g}% so it doesn't stop the charge.")
    if not added:
        power = min_soc = None

    after, overlapping, trimmed = merge(current, added)
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
    for p in overlapping:
        rest = [t.label() for t in trimmed if t.raw is p.raw]
        notes.append(
            f"Your {p.work_mode} period {p.label()} overlaps the planner's, so "
            + (
                f"only the overlapping part is replaced; it's kept for {', '.join(rest)}."
                if rest
                else "it's replaced."
            )
        )
    if schedule.default is not None and (added or removed):
        notes.append(
            f"Your all-day {schedule.default.work_mode} period is Fox's default for times no "
            "other period covers. It isn't sent, as the API can't set it, and it's checked "
            "for after the write."
        )
    if schedule.switched_off and (added or removed):
        notes.append(
            "These switched-off periods will be lost, as the write replaces the whole list: "
            + ", ".join(f"{p.work_mode} {p.label()}" for p in schedule.switched_off)
            + "."
        )
    elif added or removed:
        notes.append(
            "Fox's current schedule API doesn't show switched-off periods. If you have any in "
            "the Fox app, this write may remove them."
        )
    stages = []
    if drain:
        stages.append(f"discharge to {drain_soc:g}% {_labels(drain)}")
    if hold:
        stages.append(f"hold {_labels(hold)}")
    if target is not None:
        stages.append(f"force charge to {target:g}% {_labels(added[len(drain) + len(hold):])}")
    return Proposal(
        day=plan.day,
        mode=rec.mode,
        planned_soc_percent=rec.target_soc_percent,
        charge_kwh=rec.charge_kwh,
        start=start if target is not None else None,
        stop=stop if target is not None else None,
        hold_start=hold_start if hold else None,
        hold_stop=hold_stop if hold else None,
        discharge_start=drain_start if drain else None,
        discharge_stop=drain_stop if drain else None,
        discharge_soc_percent=drain_soc if drain else None,
        discharge_power_w=drain_power if drain else None,
        target_soc_percent=target,
        power_w=power,
        min_soc_on_grid=min_soc,
        schedule=schedule,
        added=added,
        removed=removed,
        after=after,
        notes=notes,
        trimmed=trimmed,
        max_soc=max_soc if added else None,
        summary="; ".join(stages),
    )


def _labels(periods: list[Period]) -> str:
    return ", ".join(p.label() for p in periods)


def read_schedule(client: FoxEssClient, sn: str) -> Schedule:
    return Schedule.from_fox(client.scheduler(sn))


def write_requests(
    sn: str, periods: list[Period], enable: bool, was_enabled: bool
) -> list[tuple[str, dict]]:
    """The exact write requests, as (path, JSON body), in the order they're sent. The dry run
    prints these, and the request tests check the real client sends exactly these.

    The periods always go in one V3 write, and an empty list removes them all (D3). V3 has no
    master switch, so the V1 flag call follows only when the schedule must be switched on from
    off, or is to be off: switching off is always confirmed, as it's the safe state."""
    groups = [p.to_fox() for p in periods]
    on = bool(enable and groups)
    requests: list[tuple[str, dict]] = [(SCHEDULER_SET_PATH, scheduler_set_body(sn, groups))]
    if not (on and was_enabled):
        requests.append((SCHEDULER_FLAG_PATH, {"deviceSN": sn, "enable": int(on)}))
    return requests


def apply_requests(sn: str, proposal: Proposal) -> list[tuple[str, dict]]:
    """What `apply` would send for this proposal: nothing when there's nothing to change."""
    if not proposal.added and not proposal.removed:
        return []
    return write_requests(sn, proposal.after, proposal.enable_after, proposal.schedule.enabled)


class PartialWrite(FoxEssError):
    """A write failed after the periods reached, or may have reached, the inverter."""

    def __init__(self, message: str, periods_sent: bool) -> None:
        super().__init__(message)
        self.periods_sent = periods_sent


@dataclass(frozen=True)
class ApiCall:
    """One call Apply makes to Fox ESS, in order, for a person to read before authorising."""

    method: str
    path: str
    body: dict
    purpose: str
    writes: bool


def apply_calls(sn: str, proposal: Proposal) -> list[ApiCall]:
    """Every call Apply would make for this proposal, reads included, in order."""
    read = {"deviceSN": sn}
    calls = [
        ApiCall(
            "POST",
            SCHEDULER_PATH,
            read,
            "Read your schedule again, and stop if it has changed since this preview.",
            False,
        )
    ]
    writes = apply_requests(sn, proposal)
    for path, body in writes:
        if path == SCHEDULER_SET_PATH:
            n = len(body["groups"])
            purpose = (
                f"Replace your inverter's schedule with the {n} period{'s' if n != 1 else ''} "
                "shown above. Fox replaces the whole list with what's sent."
                if n
                else "Remove every period from your inverter's schedule."
            )
        else:
            purpose = (
                "Switch your inverter's schedule on, so the periods run."
                if body["enable"]
                else "Switch your inverter's schedule off."
            )
        calls.append(ApiCall("POST", path, body, purpose, True))
    if writes:
        calls.append(
            ApiCall(
                "POST",
                SCHEDULER_PATH,
                read,
                "Read the schedule back and check it matches what was sent.",
                False,
            )
        )
    return calls


def _write(
    client: FoxEssClient, sn: str, periods: list[Period], enable: bool, was_enabled: bool
) -> None:
    """Send the write requests in order. A failure partway raises `PartialWrite` when the
    periods did or may have reached the inverter."""
    reached = False
    for path, body in write_requests(sn, periods, enable, was_enabled):
        try:
            if path == SCHEDULER_SET_PATH:
                client.set_scheduler_groups(sn, body["groups"])
                reached = True
            else:
                client.set_scheduler_enabled(sn, bool(body["enable"]))
        except FoxEssWritesBlocked:
            raise
        except FoxEssError as exc:
            # No errno means the reply was lost on the way, so Fox may have acted on it.
            if reached or exc.errno is None:
                raise PartialWrite(str(exc), periods_sent=reached) from exc
            raise


def _verify(client: FoxEssClient, sn: str, before: Schedule, periods: list[Period], enable: bool):
    back = read_schedule(client, sn)
    problems = matches(periods, back.periods)
    expected_on = bool(enable and periods)
    if back.enabled != expected_on:
        problems.append(
            f"The inverter reports its schedule is {'on' if back.enabled else 'off'}, "
            f"not {'on' if expected_on else 'off'}."
        )
    if before.default is not None and back.default is None:
        problems.append(
            f"Your all-day {before.default.work_mode} period, Fox's default, is no longer "
            "reported. Check it in the Fox app."
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
    before: Schedule,
    periods: list[Period],
    enable: bool,
    done: str,
    **values: Any,
) -> tuple[ControlAction, bool]:
    """Write, read back and log. Returns the log entry, and whether the periods did or may
    have reached the inverter, so the caller can keep track of them even after a failure (D4)."""
    sn = system.fox_ess.device_sn
    sent = {"groups": [p.to_fox() for p in periods], "enable": int(bool(enable and periods))}
    try:
        _write(client, sn, periods, enable, before.enabled)
        back, problems = _verify(client, sn, before, periods, enable)
    except PartialWrite as exc:
        where = (
            "The periods were written, but the next step failed"
            if exc.periods_sent
            else "The reply was lost, so the periods may have been written"
        )
        action = _log(
            db,
            system,
            kind,
            trigger,
            ControlStatus.failed,
            f"{where}: {exc}. Check the schedule in the Fox app; Remove and Restore still "
            "know about these periods.",
            sent=sent,
            **values,
        )
        return action, True
    except FoxEssError as exc:
        action = _log(
            db,
            system,
            kind,
            trigger,
            ControlStatus.failed,
            f"Fox ESS refused or failed the change: {exc}. Check the schedule in the Fox app.",
            sent=sent,
            **values,
        )
        return action, False
    status = ControlStatus.mismatch if problems else ControlStatus.ok
    message = " ".join(problems) if problems else done
    action = _log(
        db,
        system,
        kind,
        trigger,
        status,
        message,
        sent=sent,
        read_back=back.to_snapshot(),
        **values,
    )
    return action, True




def apply(
    db: Session,
    system: System,
    control: InverterControl,
    client: FoxEssClient,
    proposal: Proposal,
    trigger: ControlTrigger,
) -> ControlAction:
    """Write a proposal. Saves the original schedule first if this is the first write."""
    if not proposal.added and not proposal.removed:
        return _log(
            db,
            system,
            ControlActionKind.apply,
            trigger,
            ControlStatus.skipped,
            "No charge needed and no planner periods to remove, so nothing was written.",
            day=proposal.day,
        )
    if control.original_schedule is None:
        control.original_schedule = proposal.schedule.to_snapshot()
        control.original_saved_at = datetime.now(timezone.utc)
    if proposal.added:
        what = proposal.summary
        done = f"{what[0].upper()}{what[1:]} written and read back as written."
    else:
        done = "No charge needed; the planner's force charge periods were removed."
    action, reached = _run_write(
        db,
        system,
        client,
        ControlActionKind.apply,
        trigger,
        proposal.schedule,
        proposal.after,
        proposal.enable_after,
        done,
        day=proposal.day,
        target_soc_percent=proposal.target_soc_percent,
    )
    added = [p.to_fox() for p in proposal.added]
    if action.status != ControlStatus.failed:
        control.written_groups = added
    elif reached:
        # The new periods may be there, and last night's may not have gone: remember both, so
        # Remove takes out whichever the inverter has (D4).
        earlier = [g for g in control.written_groups or [] if g not in added]
        control.written_groups = earlier + added
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
    action, _ = _run_write(
        db,
        system,
        client,
        ControlActionKind.clear,
        ControlTrigger.manual,
        schedule,
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
    try:
        # Read first, as before every write, to know whether the schedule must be switched.
        schedule = read_schedule(client, system.fox_ess.device_sn)
    except FoxEssError as exc:
        return _log(
            db,
            system,
            ControlActionKind.restore,
            ControlTrigger.manual,
            ControlStatus.failed,
            f"Couldn't read the inverter's schedule: {exc}",
        )
    original = Schedule.from_fox(control.original_schedule)
    action, _ = _run_write(
        db,
        system,
        client,
        ControlActionKind.restore,
        ControlTrigger.manual,
        schedule,
        original.periods,
        original.enabled,
        "Your original schedule was put back and read back as saved.",
    )
    if action.status != ControlStatus.failed:
        control.written_groups = []
        control.test_groups = []
    return action
