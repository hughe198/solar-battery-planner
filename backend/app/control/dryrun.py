"""Dry run: what tonight's apply would send to the inverter, without sending it.

    python -m app.control.dryrun --email you@example.com [--out review.md]

It reads the inverter's schedule once (a read, like the preview), builds tonight's plan and
proposal, and prints a review pack in Markdown: the plan, the schedule before and after, the
automated checks, the exact requests, and what a person and an AI reviewer should check. The
Fox client it uses is read-only, and the database session is rolled back, so nothing is written
anywhere. See docs/inverter-trust-ladder.md, stage 2.
"""

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.control.invariants import Check, check
from app.control.schedule import Period, Schedule
from app.control.service import Proposal, apply_requests, propose
from app.importers.fox_ess import SCHEDULER_PATH, FoxEssClient
from app.models import InverterControl, System

MARK = {"pass": "PASS", "fail": "FAIL", "warn": "NOTE"}


@dataclass
class DryRun:
    at: datetime
    raw: dict[str, Any]
    schedule: Schedule
    plan: Any
    proposal: Proposal
    requests: list[tuple[str, dict]]
    checks: list[Check]

    @property
    def failed(self) -> bool:
        return any(c.status == "fail" for c in self.checks)


def build(
    system: System,
    control: InverterControl | None,
    client: FoxEssClient,
    plan: Any,
    now: datetime | None = None,
) -> DryRun:
    """Read the schedule, and work out what applying `plan` would send. Sends no write."""
    sn = system.fox_ess.device_sn
    raw = client.scheduler(sn)
    schedule = Schedule.from_fox(raw)
    proposal = propose(system, control or InverterControl(), plan, schedule)
    return DryRun(
        at=now or datetime.now(timezone.utc),
        raw=raw,
        schedule=schedule,
        plan=plan,
        proposal=proposal,
        requests=apply_requests(sn, proposal),
        checks=check(proposal, system.timezone, raw),
    )


def _local(moment: datetime | None, tz: str) -> str:
    return moment.astimezone(ZoneInfo(tz)).strftime("%a %d %b %H:%M") if moment else "–"


def _table(periods: list[Period], new: list[Period] | None = None) -> list[str]:
    if not periods:
        return ["_No periods._"]
    rows = [
        "| Time | Mode | Stop at | Power | Minimum | |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for p in periods:
        mark = "new" if new and any(p is n for n in new) else ""

        def show(value: float | None, unit: str) -> str:
            return f"{value:g}{unit}" if value is not None else "–"

        rows.append(
            f"| {p.label()} | {p.work_mode} | {show(p.fd_soc, '%')} | {show(p.fd_pwr, ' W')} "
            f"| {show(p.min_soc_on_grid, '%')} | {mark} |"
        )
    return rows


def _mask(sn: str) -> str:
    return f"…{sn[-4:]}" if len(sn) > 4 else "…"


def render(dry: DryRun, system: System, show_serial: bool = False) -> str:
    tz = system.timezone
    p = dry.proposal
    sn = system.fox_ess.device_sn
    lines = [
        f"# Inverter dry run for {p.day:%A %d %B %Y}",
        "",
        f"Made {_local(dry.at, tz)}. **Nothing was sent to the inverter**: the schedule was read "
        "once, and the requests below are what Apply would send.",
        "",
        "## Automated checks",
        "",
        "| Result | Check | Detail |",
        "| --- | --- | --- |",
        *(f"| {MARK[c.status]} | {c.name} | {c.detail} |" for c in dry.checks),
        "",
        "## The plan",
        "",
        f"- Mode: {p.mode.value.replace('_', ' ')}",
        f"- Target: {p.planned_soc_percent:g}%, charging {p.charge_kwh:g} kWh",
        f"- Discharge: {_local(p.discharge_start, tz)} to {_local(p.discharge_stop, tz)}"
        + (f", down to {p.discharge_soc_percent:g}%" if p.discharge_soc_percent else ""),
        f"- Hold: {_local(p.hold_start, tz)} to {_local(p.hold_stop, tz)}",
        f"- Charge: {_local(p.start, tz)} to {_local(p.stop, tz)}"
        + (f", to {p.target_soc_percent:g}%" if p.target_soc_percent is not None else ""),
    ]
    reasons = getattr(dry.plan.recommendations[0], "reasons", None) or []
    lines += [f"- Why: {r}" for r in reasons[:3]]
    lines += [
        "",
        f"## Schedule now ({'on' if dry.schedule.enabled else 'off'})",
        "",
        *_table(dry.schedule.periods),
        "",
        f"## Schedule after ({'on' if p.enable_after else 'off'})",
        "",
        *_table(p.after, p.added),
        "",
    ]
    if p.notes:
        lines += ["## Notes shown in the app", "", *(f"- {n}" for n in p.notes), ""]
    lines += ["## Exact requests Apply would send", ""]
    if not dry.requests:
        lines += ["None: there's nothing to change.", ""]
    for n, (path, body) in enumerate(dry.requests, 1):
        shown = body if show_serial else {**body, "deviceSN": _mask(sn)}
        lines += [
            f"{n}. `POST {path}`",
            "",
            "```json",
            json.dumps(shown, indent=2, ensure_ascii=False),
            "```",
            "",
        ]
    lines += [
        f"Each write is followed by `POST {SCHEDULER_PATH}` to read it back.",
        "",
        "## For you to check",
        "",
        '- [ ] Open the Fox app\'s schedule. Does "Schedule now" match it, period for period?',
        '- [ ] Is the target, and each time in "The plan", what you\'d set by hand tonight?',
        '- [ ] Would you be happy if "Schedule after" ran every night until changed?',
        "- [ ] Is every NOTE above acceptable, and is there no FAIL?",
        "",
        "## For an AI reviewer",
        "",
        "Paste this whole file into Claude with the prompt below.",
        "",
        "> You're reviewing a request that will change a home battery inverter's schedule through",
        "> the Fox ESS Open API (Scheduler V3). Fox replaces the whole list of periods with the",
        "> `groups` sent; each period's settings are in `extraParam`, and a setting left out",
        "> keeps its old value. An all-day 00:00–23:59 period is Fox's default and must not be",
        "> sent. Ends are inclusive to the minute. Check, and answer each with yes or no",
        '> and a reason: (1) Does each request body match "Schedule after" exactly, field for',
        '> field? (2) Are any of the user\'s periods in "Schedule now" changed or missing in the',
        "> request, other than ones listed as replaced? (3) Is every ForceCharge or",
        '> ForceDischarge inside the times in "The plan"? (4) Is every fdSoc at or above its',
        "> minSocOnGrid, and every value inside a sensible range? (5) Could anything here",
        "> charge at a dear rate, empty the battery before an expensive period, or repeat",
        "> harmfully on later nights? (6) Do the automated checks agree with what you found?",
        "> Finish with SAFE or NOT SAFE.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from sqlalchemy import select

    from app.api.plan import make_plan, next_day
    from app.crypto import decrypt
    from app.database import SessionLocal
    from app.models import User
    from app.weather.open_meteo import OpenMeteoClient

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--email", required=True, help="the account whose inverter to read")
    parser.add_argument(
        "--start-soc", type=float, help="plan from this level, not the latest reading"
    )
    parser.add_argument("--out", help="write the review pack here as well as printing it")
    parser.add_argument("--show-serial", action="store_true", help="don't mask the serial number")
    args = parser.parse_args(argv)

    with SessionLocal() as db:
        try:
            user = db.scalar(select(User).where(User.email == args.email))
            system = user.system if user else None
            if system is None or system.fox_ess is None or system.battery is None:
                print("No system with a battery and Fox ESS connection for that email.")
                return 2
            # Read-only: this client refuses writes whatever the server setting says.
            client = FoxEssClient(decrypt(system.fox_ess.api_key_encrypted), writes_allowed=False)
            plan = make_plan(db, system, OpenMeteoClient(), next_day(system), args.start_soc)
            dry = build(system, system.inverter_control, client, plan)
            text = render(dry, system, args.show_serial)
        finally:
            db.rollback()
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    return 1 if dry.failed else 0


if __name__ == "__main__":
    sys.exit(main())
