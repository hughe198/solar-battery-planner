"""Checks on a proposal before anything is written, in plain words.

These are the invariants from docs/inverter-api-test-plan.md that can be judged from one
proposal: the dry run prints them for a person and an AI reviewer to read, and the tests assert
them across many generated plans and schedules. Nothing here talks to an inverter.

Applying writes the whole day (see app.control.schedule): the planner's forced stages, and
self-use at every other minute. So beside the ranges and Fox's rules, the checks are that the
day is covered exactly once, that nothing forced runs outside the plan, and that everything
else is self-use at the planner's levels.
"""

from dataclasses import dataclass
from typing import Any, Literal

from app.control.schedule import (
    FORCE_CHARGE,
    FORCE_DISCHARGE,
    SELF_USE,
    Period,
    charge_periods,
)

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
    after: list[Period] = proposal.after
    forced = [p for p in after if p.forced]
    rest = [p for p in after if not p.forced]

    # 5. Every value sent is within the range the inverter reports.
    out_of_range = []
    for p in after:
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
            "; ".join(out_of_range) or f"{len(after)} period(s) checked.",
        )
    )

    # 6. Periods are well formed, don't overlap, and fit.
    shape = [
        f"{p.label()} runs {p.start}–{p.end} minutes"
        for p in after
        if not 0 <= p.start <= p.end < DAY_MINUTES
    ]
    for i, a in enumerate(after):
        for b in after[i + 1 :]:
            if a.overlaps(b):
                shape.append(f"{a.work_mode} {a.label()} overlaps {b.work_mode} {b.label()}")
    if len(after) > schedule.max_groups:
        shape.append(f"{len(after)} periods, but the inverter holds {schedule.max_groups}")
    checks.append(
        Check(
            "Periods well formed, not overlapping, and fit",
            "fail" if shape else "pass",
            "; ".join(shape)
            or f"{len(after)} of at most {schedule.max_groups} periods, none clash.",
        )
    )

    # D13 and whole-day control: every minute has a period, so nothing is left to whatever
    # the inverter does with no schedule.
    gaps = sorted(set(range(DAY_MINUTES)) - _minutes(after))
    checks.append(
        Check(
            "The whole day covered",
            "fail" if gaps else "pass",
            f"{len(gaps)} minute(s) with no period, from minute {gaps[0]}"
            if gaps
            else "Every minute from 00:00 to 23:59 has a period.",
        )
    )

    # Fox's all-day default period is never sent.
    default_sent = [p.label() for p in after if p.all_day]
    checks.append(
        Check(
            "All-day default period not sent",
            "fail" if default_sent else "pass",
            ("Sent: " + ", ".join(default_sent)) if default_sent else "None sent.",
        )
    )

    # 9. Forced periods only in the spans the plan gave: none of the user's own survive.
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
    for p in forced:
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

    # Everything else is self-use at the planner's levels: none of the user's own settings,
    # such as an older 5% minimum, carries on.
    minimum = proposal.self_use_min_soc
    odd = [
        f"{p.work_mode} {p.label()}"
        + (f" minimum {p.min_soc_on_grid:g}%" if p.min_soc_on_grid != minimum else "")
        for p in rest
        if p.work_mode != SELF_USE or p.min_soc_on_grid != minimum
    ]
    checks.append(
        Check(
            "Self-use at your minimum everywhere else",
            "fail" if odd else "pass",
            ("Not so: " + ", ".join(odd))
            if odd
            else f"{len(rest)} self-use period(s), down to {minimum:g}%.",
        )
    )

    # The grid charge stays at or under the owner's maximum, leaving room for solar.
    cap = proposal.grid_charge_max
    over = [
        f"{p.label()} charges to {p.fd_soc:g}%"
        for p in forced
        if p.work_mode == FORCE_CHARGE and cap is not None and (p.fd_soc or 0) > max(cap, minimum)
    ]
    checks.append(
        Check(
            "Grid charge at or under your maximum",
            "fail" if over else "pass",
            ("Over: " + ", ".join(over))
            if over
            else (f"No higher than {cap:g}%." if cap is not None else "No maximum set."),
        )
    )

    # D6. Fox says fdSoc must be at least minSocOnGrid.
    below = [
        f"{p.work_mode} {p.label()} stops at {p.fd_soc:g}% under the {p.min_soc_on_grid:g}% minimum"
        for p in after
        if p.fd_soc is not None and p.min_soc_on_grid is not None and p.fd_soc < p.min_soc_on_grid
    ]
    checks.append(
        Check(
            "Stop level at or above the minimum (Fox's rule, defect D6)",
            "fail" if below else "pass",
            "; ".join(below) or "Every period stops at or above its minimum.",
        )
    )

    # Things a person should know, though nothing is wrong.
    if proposal.removed:
        checks.append(
            Check(
                "Your current periods replaced",
                "warn",
                f"{len(proposal.removed)} period(s) replaced. "
                + (
                    "Your schedule as it is now is saved first, for Restore."
                    if proposal.saves_manual
                    else "Your latest manual schedule is already saved, for Restore."
                ),
            )
        )
    switched_off = schedule.switched_off
    if switched_off and proposal.changes:
        checks.append(
            Check(
                "Switched-off periods dropped (D2)",
                "warn",
                f"{len(switched_off)} switched-off period(s) won't be written back: "
                + ", ".join(f"{p.work_mode} {p.label()}" for p in switched_off),
            )
        )
    if not schedule.enabled and proposal.changes:
        checks.append(
            Check(
                "Your schedule switches on",
                "warn",
                "The schedule is off now; writing the whole day switches it on.",
            )
        )
    return checks
