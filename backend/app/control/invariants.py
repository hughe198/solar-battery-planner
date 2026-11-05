"""Checks on a proposal before anything is written, in plain words.

These are the invariants from docs/inverter-api-test-plan.md that can be judged from one
proposal: the dry run prints them for a person and an AI reviewer to read, and the tests assert
them across many generated plans and schedules. Nothing here talks to an inverter.
"""

from dataclasses import dataclass
from typing import Any, Literal

from app.control.schedule import FORCE_CHARGE, FORCE_DISCHARGE, Period, charge_periods, v3_group

Status = Literal["pass", "fail", "warn"]
DAY_MINUTES = 24 * 60


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str


def _minutes(periods: list[Period]) -> set[int]:
    return {m for p in periods for m in range(p.start, p.end + 1)}


def check(proposal: Any, tz: str, raw_schedule: dict[str, Any] | None = None) -> list[Check]:
    """Every check on a proposal from app.control.service.propose. `raw_schedule` is the
    schedule exactly as Fox returned it, for what the parsed one leaves out."""
    checks: list[Check] = []
    schedule = proposal.schedule
    added: list[Period] = proposal.added
    trimmed: list[Period] = getattr(proposal, "trimmed", [])
    kept = [p for p in proposal.after if not any(p is a for a in added)]
    untouched = [p for p in kept if not any(p is t for t in trimmed)]

    # 5. Every value sent is within the range the inverter reports.
    out_of_range = []
    for p in added:
        for key, value in (
            ("fdsoc", p.fd_soc),
            ("fdpwr", p.fd_pwr),
            ("minsocongrid", p.min_soc_on_grid),
            ("maxsoc", p.max_soc),
        ):
            low, high = schedule.ranges[key]
            if value is not None and not low <= value <= high:
                out_of_range.append(f"{p.label()} {key} {value:g} outside {low:g}–{high:g}")
    checks.append(
        Check(
            "Values within the inverter's ranges",
            "fail" if out_of_range else "pass",
            "; ".join(out_of_range) or f"{len(added)} new period(s) checked.",
        )
    )

    # 6. Periods are well formed, ours don't overlap anything, and they fit.
    shape = [
        f"{p.label()} runs {p.start}–{p.end} minutes"
        for p in proposal.after
        if not 0 <= p.start <= p.end < DAY_MINUTES
    ]
    for i, a in enumerate(added):
        for b in added[i + 1 :] + kept:
            if a.overlaps(b):
                shape.append(f"{a.work_mode} {a.label()} overlaps {b.work_mode} {b.label()}")
    if len(proposal.after) > schedule.max_groups:
        shape.append(f"{len(proposal.after)} periods, but the inverter holds {schedule.max_groups}")
    checks.append(
        Check(
            "Periods well formed, not overlapping, and fit",
            "fail" if shape else "pass",
            "; ".join(shape)
            or f"{len(proposal.after)} of at most {schedule.max_groups} periods, none clash.",
        )
    )

    # 7. The user's periods that aren't overlapped go back exactly as read, in V3's shape.
    # What's left of a partly overlapped one keeps every setting, with only its times changed.
    changed = [p.label() for p in untouched if p.raw and p.to_fox() != v3_group(p.raw)]
    for p in trimmed:
        times = ("startHour", "startMinute", "endHour", "endMinute")
        sent = {k: v for k, v in p.to_fox().items() if k not in times}
        read = {k: v for k, v in v3_group(p.raw).items() if k not in times}
        if sent != read:
            changed.append(p.label())
    checks.append(
        Check(
            "Your other periods sent back unchanged",
            "fail" if changed else "pass",
            ("Changed: " + ", ".join(changed)) if changed else f"{len(kept)} kept as read.",
        )
    )

    # Fox's all-day default period is never sent.
    default_sent = [p.label() for p in proposal.after if p.all_day]
    checks.append(
        Check(
            "All-day default period not sent",
            "fail" if default_sent else "pass",
            ("Sent: " + ", ".join(default_sent))
            if default_sent
            else (
                f"Your all-day {schedule.default.work_mode} period is left to Fox."
                if schedule.default is not None
                else "There isn't one."
            ),
        )
    )

    # 9. Forced periods only in the spans the plan gave.
    charge_spans = [
        (s, e)
        for s, e in (
            (proposal.hold_start, proposal.hold_stop),
            (proposal.start, proposal.stop),
        )
        if s is not None and e is not None
    ]
    allowed_charge = _minutes(
        [p for s, e in charge_spans for p in charge_periods(s, e, tz, 0, 0, 0)]
    )
    allowed_discharge = (
        _minutes(charge_periods(proposal.discharge_start, proposal.discharge_stop, tz, 0, 0, 0))
        if proposal.discharge_start is not None and proposal.discharge_stop is not None
        else set()
    )
    stray = []
    for p in added:
        allowed = {FORCE_CHARGE: allowed_charge, FORCE_DISCHARGE: allowed_discharge}.get(
            p.work_mode, set()
        )
        if not _minutes([p]) <= allowed:
            stray.append(f"{p.work_mode} {p.label()}")
    checks.append(
        Check(
            "Forced periods only where the plan put them",
            "fail" if stray else "pass",
            ("Outside the plan: " + ", ".join(stray)) if stray else "All inside the plan's times.",
        )
    )

    # D6. Fox says fdSoc must be at least minSocOnGrid.
    below = [
        f"{p.work_mode} {p.label()} stops at {p.fd_soc:g}% under the {p.min_soc_on_grid:g}% minimum"
        for p in added
        if p.fd_soc is not None and p.min_soc_on_grid is not None and p.fd_soc < p.min_soc_on_grid
    ]
    checks.append(
        Check(
            "Stop level at or above the minimum (Fox's rule, defect D6)",
            "fail" if below else "pass",
            "; ".join(below) or "Every new period stops at or above its minimum.",
        )
    )

    # Things a person should know, though nothing is wrong.
    mine_changed = [
        p for p in proposal.removed if p.work_mode not in (FORCE_CHARGE, FORCE_DISCHARGE)
    ]
    if mine_changed:
        checks.append(
            Check(
                "Your own periods overlapped (D5)",
                "warn",
                "Only the overlapping part is replaced: "
                + ", ".join(
                    f"{p.work_mode} {p.label()}, kept for "
                    + (", ".join(t.label() for t in trimmed if t.raw is p.raw) or "none of it")
                    for p in mine_changed
                ),
            )
        )
    switched_off = schedule.switched_off
    if switched_off and (proposal.added or proposal.removed):
        checks.append(
            Check(
                "Switched-off periods dropped (D2)",
                "warn",
                f"{len(switched_off)} switched-off period(s) won't be written back: "
                + ", ".join(f"{p.work_mode} {p.label()}" for p in switched_off),
            )
        )
    if proposal.added and not schedule.enabled and kept:
        checks.append(
            Check(
                "Your periods switch on too",
                "warn",
                "The schedule is off now, and switching it on runs your other periods as well.",
            )
        )
    return checks
