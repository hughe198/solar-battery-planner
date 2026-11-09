"""Layer 1 of docs/inverter-api-test-plan.md: the exact requests sent to Fox ESS.

Unlike test_control.py, which swaps the Fox client for a fake, these run the real FoxEssClient,
signing and all, against a fake Fox cloud behind httpx.MockTransport, and record every request.
Each scenario's requests are compared with a golden file in tests/golden/inverter/, so any
change to what reaches an inverter shows up as a diff for a person to approve.

To accept a deliberate change: UPDATE_GOLDEN=1 pytest tests/test_fox_requests.py, then review the
diff of tests/golden/inverter/ before committing it.

The defects D1 to D11 from the test plan each have a test below, named after the defect, that
failed before its fix.
"""

import copy
import hashlib
import json
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.config import settings
from app.control import dryrun, service
from app.control.invariants import check
from app.control.schedule import Period, Schedule, v3_group
from app.importers.fox_ess import (
    SCHEDULER_FLAG_PATH,
    SCHEDULER_PATH,
    SCHEDULER_SET_PATH,
    FoxEssClient,
    FoxEssError,
    FoxEssWritesBlocked,
)
from app.models import Battery, ControlStatus, ControlTrigger, InverterControl
from tests.test_control import PROPERTIES, group, plan_for, utc

GOLDEN = Path(__file__).parent / "golden" / "inverter"
SN = "TESTSN0001"
KEY = "test-key"


class FoxCloud:
    """Fox's cloud as far as Scheduler V3 goes: holds one inverter's schedule, checks each
    request is signed, and records every request in order.

    It's strict where Fox's documents are: a write is refused with 40257 (invalid parameter)
    if periods overlap, a time is out of range, or a forced period stops below its minimum.
    Switched-off periods (`enable: 0`) are hidden from reads, as V3 was seen to do, and lost on
    the next write. `default` is Fox's all-day default period, which reads include and any
    write drops, as the owner's inverter did on 5 November 2026 (D13)."""

    def __init__(
        self, groups=(), enable=1, properties=PROPERTIES, max_groups=96, default=None
    ):
        self.groups = copy.deepcopy(list(groups))
        self.enable = enable
        self.properties = properties
        self.max_groups = max_groups
        self.default = default
        self.requests: list[dict] = []
        # path -> errnos to return, one per call, before answering normally
        self.errors: dict[str, list[int]] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}")
        self.requests.append({"method": request.method, "path": path, "body": body})
        signed = rf"{path}\r\n{KEY}\r\n{request.headers['timestamp']}"
        if (
            request.headers.get("token") != KEY
            or request.headers.get("signature") != hashlib.md5(signed.encode()).hexdigest()
        ):
            return httpx.Response(200, json={"errno": 41808, "msg": "bad signature"})
        if self.errors.get(path):
            return httpx.Response(200, json={"errno": self.errors[path].pop(0), "msg": "injected"})
        if path == SCHEDULER_PATH:
            shown = [g for g in self.groups if g.get("enable", 1)]
            result = {
                "enable": self.enable,
                "maxGroupCount": self.max_groups,
                "groups": ([self.default] if self.default else []) + copy.deepcopy(shown),
                "properties": self.properties,
            }
            return httpx.Response(200, json={"errno": 0, "result": result})
        if path == SCHEDULER_SET_PATH:
            problem = self.invalid(body)
            if problem:
                return httpx.Response(200, json={"errno": 40257, "msg": problem})
            self.groups = copy.deepcopy(body["groups"])
            self.default = None
        elif path == SCHEDULER_FLAG_PATH:
            self.enable = body["enable"]
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"errno": 0, "result": None})

    def invalid(self, body: dict) -> str | None:
        if body.get("isDefault") is not False or "groups" not in body:
            return "isDefault and groups are required"
        groups = body["groups"]
        if len(groups) > self.max_groups:
            return "too many groups"
        periods = []
        for g in groups:
            if "enable" in g or any(k in g for k in ("fdSoc", "fdPwr", "minSocOnGrid")):
                return "V1 fields in a V3 group"
            if not (0 <= g["startHour"] < 24 and 0 <= g["endHour"] < 24):
                return "hour out of range"
            p = Period.from_fox(g)
            if p.start > p.end:
                return "starts after it ends"
            if p.all_day:
                return "all-day default sent"
            if p.fd_soc is not None and p.min_soc_on_grid is not None:
                if p.fd_soc < p.min_soc_on_grid:
                    return "fdSoc must be equal or greater than minSocOnGrid"
            periods.append(p)
        for i, a in enumerate(periods):
            if any(a.overlaps(b) for b in periods[i + 1 :]):
                return "periods overlap"
        return None

    def client(self, **kwargs) -> FoxEssClient:
        http = httpx.Client(transport=httpx.MockTransport(self.handle), base_url="https://fox.test")
        return FoxEssClient(KEY, http=http, min_gap=0, sleep=lambda s: None, **kwargs)

    def writes(self) -> list[dict]:
        return [r for r in self.requests if r["path"] != SCHEDULER_PATH]


@pytest.fixture
def writes_on(monkeypatch):
    monkeypatch.setattr(settings, "inverter_writes", True)


@pytest.fixture
def logged(monkeypatch):
    """Keeps audit log entries in a list, so the service runs without a database."""
    entries = []

    def log(db, system, kind, trigger, status, message, **values):
        entry = SimpleNamespace(kind=kind, status=status, message=message, **values)
        entries.append(entry)
        return entry

    monkeypatch.setattr(service, "_log", log)
    return entries


def system_in(tz="UTC", min_soc=10.0):
    battery = Battery(
        capacity_kwh=10, min_soc_percent=min_soc, max_charge_kw=3.6, max_discharge_kw=3.6
    )
    return SimpleNamespace(battery=battery, timezone=tz, fox_ess=SimpleNamespace(device_sn=SN))


def forced(groups):
    """The forced periods among groups as Fox holds them."""
    return [g for g in groups if g["workMode"] in ("ForceCharge", "ForceDischarge")]


def covered(groups):
    """Every minute the groups cover."""
    return {
        m
        for p in (Period.from_fox(g) for g in groups)
        for m in range(p.start, p.end + 1)
    }


def golden(name: str, cloud: FoxCloud, outcome, note: str) -> None:
    actual = {
        "scenario": note,
        "requests": cloud.requests,
        "outcome": outcome.status.value if outcome else None,
        "message": outcome.message if outcome else None,
        "inverter_after": {"enable": cloud.enable, "groups": cloud.groups},
    }
    path = GOLDEN / f"{name}.json"
    if os.environ.get("UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(actual, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    assert path.exists(), f"No golden file {path.name}: run with UPDATE_GOLDEN=1 and review it"
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert actual == expected, (
        f"Requests for {name} differ from {path.name}. If the change is intended, run with "
        "UPDATE_GOLDEN=1 and have a person review the diff."
    )


def apply_scenario(cloud, plan, system=None, control=None):
    """Read, propose and apply through the real client, checking the dry run predicted it."""
    system = system or system_in()
    control = control or InverterControl()
    client = cloud.client()
    proposal = service.propose(system, control, plan, service.read_schedule(client, SN))
    predicted = service.apply_requests(SN, proposal)
    before = len(cloud.requests)
    outcome = service.apply(None, system, control, client, proposal, ControlTrigger.manual)
    sent = [(r["path"], r["body"]) for r in cloud.requests[before:] if r["path"] != SCHEDULER_PATH]
    assert sent == predicted, "The dry run's requests differ from what apply sent"
    # The calls shown for authorising are exactly what was sent, reads included: the apply
    # endpoint's own read, then what service.apply sent.
    shown = [(c.method, c.path, c.body) for c in service.apply_calls(SN, proposal)]
    made = [(r["method"], r["path"], r["body"]) for r in cloud.requests[before - 1 :]]
    if outcome.status != ControlStatus.skipped:
        assert shown == made, "The calls shown for authorising differ from what was sent"
    return proposal, outcome, control


# --- the server switch -----------------------------------------------------------------------


def test_writes_are_blocked_by_default_before_anything_is_sent():
    assert settings.inverter_writes is False
    cloud = FoxCloud([group((16, 0), (18, 59))])
    client = cloud.client()
    with pytest.raises(FoxEssWritesBlocked, match="SBP_INVERTER_WRITES"):
        client.set_scheduler_groups(SN, [])
    with pytest.raises(FoxEssWritesBlocked):
        client.set_scheduler_enabled(SN, False)
    with pytest.raises(FoxEssWritesBlocked):
        client.post(SCHEDULER_SET_PATH, {"deviceSN": SN, "groups": []})
    assert cloud.requests == []
    # Reads still work.
    assert client.scheduler(SN)["enable"] == 1


def test_read_only_client_is_blocked_even_when_writes_are_on(writes_on):
    cloud = FoxCloud()
    with pytest.raises(FoxEssWritesBlocked, match="read-only"):
        cloud.client(writes_allowed=False).set_scheduler_enabled(SN, True)
    assert cloud.requests == []


def test_a_blocked_apply_is_logged_and_records_nothing(logged):
    cloud = FoxCloud()
    _, outcome, control = apply_scenario_blocked(cloud)
    assert outcome.status == ControlStatus.failed
    assert "switched off on this server" in outcome.message
    assert cloud.writes() == []
    assert control.written_groups is None


def apply_scenario_blocked(cloud):
    system, control = system_in(), InverterControl()
    client = cloud.client()
    plan = plan_for(utc(2026, 1, 11, 2, 0), utc(2026, 1, 11, 5, 0))
    proposal = service.propose(system, control, plan, service.read_schedule(client, SN))
    return proposal, service.apply(None, system, control, client, proposal, "manual"), control


def test_signature_matches_a_value_worked_out_by_hand(monkeypatch):
    monkeypatch.setattr("app.importers.fox_ess.time.time", lambda: 1_700_000_000.0)
    headers = FoxEssClient(KEY)._headers(SCHEDULER_SET_PATH)
    assert headers["timestamp"] == "1700000000000"
    # md5 of the path, key and timestamp joined by the four characters \r\n, as Fox's samples do.
    assert headers["signature"] == "9dbfd3ad1f6b73894a6ee9f8dee391d8"


# --- golden scenarios (Layer 2 rows) ---------------------------------------------------------


def test_s1_charge_only_onto_an_empty_schedule_that_was_off(writes_on, logged):
    cloud = FoxCloud(enable=0)
    plan = plan_for(utc(2026, 1, 11, 2, 0), utc(2026, 1, 11, 5, 30), target=79.2)
    _, outcome, _ = apply_scenario(cloud, plan)
    assert outcome.status == ControlStatus.ok, outcome.message
    golden("S1-charge-only", cloud, outcome, "Charge 02:00–05:30 to 79.2%, empty schedule off")


def test_s3_hold_then_charge(writes_on, logged):
    cloud = FoxCloud()
    plan = plan_for(
        utc(2026, 1, 11, 3, 40),
        utc(2026, 1, 11, 5, 30),
        hold=(utc(2026, 1, 11, 0, 30), utc(2026, 1, 11, 3, 40)),
    )
    _, outcome, _ = apply_scenario(cloud, plan)
    assert outcome.status == ControlStatus.ok, outcome.message
    golden("S3-hold-then-charge", cloud, outcome, "Hold 00:30–03:40, charge 03:40–05:30 to 80%")


def test_s5_discharge_hold_charge(writes_on, logged):
    cloud = FoxCloud()
    plan = plan_for(
        utc(2026, 1, 11, 3, 0),
        utc(2026, 1, 11, 4, 0),
        hold=(utc(2026, 1, 11, 0, 0), utc(2026, 1, 11, 3, 0)),
        drain=(utc(2026, 1, 10, 22, 30), utc(2026, 1, 11, 0, 0)),
    )
    _, outcome, _ = apply_scenario(cloud, plan)
    assert outcome.status == ControlStatus.ok, outcome.message
    golden(
        "S5-discharge-hold-charge",
        cloud,
        outcome,
        "Discharge 22:30–00:00, hold 00:00–03:00, charge 03:00–04:00 to 80%",
    )


def test_s6_nothing_to_do_only_reads(writes_on, logged):
    # The inverter already holds exactly tonight's whole day: one read, no write.
    cloud = FoxCloud([group((16, 0), (18, 59))])
    plan = plan_for(None, None, charge_kwh=0)
    _, _, control = apply_scenario(cloud, plan)
    cloud.requests.clear()
    _, outcome, _ = apply_scenario(cloud, plan, control=control)
    assert outcome.status == ControlStatus.skipped
    assert cloud.writes() == []
    golden(
        "S6-nothing-to-do", cloud, outcome, "Tonight's whole day already there: one read, no write"
    )


def test_t1_window_crossing_midnight(writes_on, logged):
    cloud = FoxCloud()
    plan = plan_for(utc(2026, 1, 10, 23, 40), utc(2026, 1, 11, 5, 30))
    _, outcome, _ = apply_scenario(cloud, plan)
    assert outcome.status == ControlStatus.ok, outcome.message
    golden("T1-across-midnight", cloud, outcome, "Charge 23:40–05:30, split at midnight")


def test_t5_local_wall_clock_in_summer(writes_on, logged):
    cloud = FoxCloud()
    plan = plan_for(utc(2026, 6, 11, 1, 0), utc(2026, 6, 11, 3, 0))
    _, outcome, _ = apply_scenario(cloud, plan, system_in("Europe/London"))
    assert outcome.status == ControlStatus.ok, outcome.message
    (charge,) = forced(cloud.groups)
    assert (charge["startHour"], charge["endHour"]) == (2, 3)
    golden("T5-london-summer", cloud, outcome, "01:00–03:00 UTC is written as 02:00–04:00 BST")


def test_e2_users_periods_are_replaced_and_put_back_by_restore(writes_on, logged):
    # Whole-day control: the user's periods go, saved first; their installer limits carry on.
    own = [
        group((7, 0), (8, 59), "SelfUse", maxSoc="100", pvLimit=4000, unknownField="kept"),
        group((16, 0), (18, 59), "Feedin", fdSoc=10, fdPwr=0, minSocOnGrid=10),
    ]
    cloud = FoxCloud(own)
    plan = plan_for(utc(2026, 1, 11, 2, 0), utc(2026, 1, 11, 5, 0))
    _, outcome, control = apply_scenario(cloud, plan)
    assert outcome.status == ControlStatus.ok, outcome.message
    assert [(g["workMode"], g["startHour"]) for g in cloud.groups] == [
        ("SelfUse", 0),
        ("ForceCharge", 2),
        ("SelfUse", 5),
    ]
    self_use = [g for g in cloud.groups if g["workMode"] == "SelfUse"]
    assert all(g["extraParam"].get("pvLimit") == 4000 for g in self_use)
    assert covered(cloud.groups) == set(range(24 * 60))
    golden(
        "E2-users-periods-replaced",
        cloud,
        outcome,
        "User's two periods replaced by the whole day; their solar limit kept",
    )
    # Restore puts them back field for field, numeric strings sent as numbers (D10).
    service.restore(None, system_in(), control, cloud.client())
    for g in own:
        assert v3_group(g) in cloud.groups
    assert cloud.groups[0]["extraParam"]["maxSoc"] == 100


def test_e5_last_nights_charge_is_replaced(writes_on, logged):
    cloud = FoxCloud()
    _, _, control = apply_scenario(cloud, plan_for(utc(2026, 1, 11, 1, 0), utc(2026, 1, 11, 2, 0)))
    cloud.requests.clear()
    plan = plan_for(utc(2026, 1, 12, 4, 0), utc(2026, 1, 12, 5, 0))
    _, outcome, _ = apply_scenario(cloud, plan, control=control)
    assert [(g["startHour"], g["endHour"]) for g in forced(cloud.groups)] == [(4, 4)]
    golden(
        "E5-replace-last-night", cloud, outcome, "Yesterday's 01:00–02:00 replaced by 04:00–05:00"
    )


def test_a2_clear_turns_the_planners_charge_into_self_use(writes_on, logged):
    own = group((16, 0), (18, 59), "Feedin", fdSoc=10, fdPwr=0, minSocOnGrid=10)
    cloud = FoxCloud([own])
    _, _, control = apply_scenario(cloud, plan_for(utc(2026, 1, 11, 2, 0), utc(2026, 1, 11, 5, 0)))
    cloud.requests.clear()
    outcome = service.clear(None, system_in(), control, cloud.client())
    assert outcome.status == ControlStatus.ok, outcome.message
    assert forced(cloud.groups) == []
    assert covered(cloud.groups) == set(range(24 * 60))
    golden("A2-clear", cloud, outcome, "Remove the planner's charge: its hours run in self-use")


def test_a4_restore_puts_back_the_original(writes_on, logged):
    own = group((16, 0), (18, 59), "Feedin", fdSoc=10, fdPwr=0, minSocOnGrid=10)
    cloud = FoxCloud([own], enable=0)
    _, _, control = apply_scenario(cloud, plan_for(utc(2026, 1, 11, 2, 0), utc(2026, 1, 11, 5, 0)))
    cloud.requests.clear()
    outcome = service.restore(None, system_in(), control, cloud.client())
    assert outcome.status == ControlStatus.ok, outcome.message
    assert (cloud.groups, cloud.enable) == ([own], 0)
    golden("A4-restore", cloud, outcome, "Original schedule back, switched off as it was")


# --- the defects from the test plan, each fixed ----------------------------------------------


def test_d1_hold_only_night_applies(writes_on, logged):
    cloud = FoxCloud()
    plan = plan_for(
        None, None, charge_kwh=0, hold=(utc(2026, 1, 11, 0, 30), utc(2026, 1, 11, 5, 30))
    )
    _, outcome, _ = apply_scenario(cloud, plan)
    assert outcome.status == ControlStatus.ok, outcome.message
    assert outcome.message == "Hold 00:30–05:30 written and read back as written."
    golden("S2-hold-only", cloud, outcome, "Hold 00:30–05:30, no charge (D1)")


def test_d3_restoring_an_empty_schedule_sends_an_empty_list(writes_on, logged):
    # D3: an empty list is how every period is removed.
    cloud = FoxCloud()
    _, _, control = apply_scenario(cloud, plan_for(utc(2026, 1, 11, 2, 0), utc(2026, 1, 11, 5, 0)))
    cloud.requests.clear()
    outcome = service.restore(None, system_in(), control, cloud.client())
    assert outcome.status == ControlStatus.ok, outcome.message
    assert (cloud.groups, cloud.enable) == ([], 0)
    golden("A3-restore-empty", cloud, outcome, "Restore an empty schedule: empty list, off (D3)")


def test_d4_failed_switch_on_still_records_what_was_written(writes_on, logged):
    cloud = FoxCloud(enable=0)
    cloud.errors[SCHEDULER_FLAG_PATH] = [44098]
    _, outcome, control = apply_scenario_unchecked(cloud)
    assert outcome.status == ControlStatus.failed
    assert "The periods were written, but the next step failed" in outcome.message
    assert cloud.groups, "the periods did reach the inverter"
    assert control.written_groups, "so the app must remember them for Remove"
    # And Remove does take them out.
    assert service.clear(None, system_in(), control, cloud.client()).status == ControlStatus.ok
    assert forced(cloud.groups) == []


def test_d4_a_lost_reply_counts_as_maybe_written(writes_on, logged):
    cloud = FoxCloud(enable=1)
    real = cloud.handle

    def times_out(request):
        response = real(request)
        if request.url.path == SCHEDULER_SET_PATH:
            raise httpx.ReadTimeout("timed out", request=request)
        return response

    cloud.handle = times_out
    _, outcome, control = apply_scenario_unchecked(cloud)
    assert outcome.status == ControlStatus.failed
    assert "may have been written" in outcome.message
    assert cloud.groups and control.written_groups == forced(cloud.groups)
    # Nothing is known of what arrived, so the latest manual schedule saved isn't replaced.
    assert control.written_schedule is None


def test_d4_a_refused_write_records_nothing(writes_on, logged):
    cloud = FoxCloud()
    cloud.errors[SCHEDULER_SET_PATH] = [44098]
    _, outcome, control = apply_scenario_unchecked(cloud)
    assert outcome.status == ControlStatus.failed
    assert "didn't accept the write" in outcome.message
    assert control.written_groups is None
    assert [r["path"] for r in cloud.writes()] == [SCHEDULER_SET_PATH]


def apply_scenario_unchecked(cloud):
    system, control = system_in(), InverterControl()
    client = cloud.client()
    plan = plan_for(utc(2026, 1, 11, 2, 0), utc(2026, 1, 11, 5, 0))
    proposal = service.propose(system, control, plan, service.read_schedule(client, SN))
    outcome = service.apply(None, system, control, client, proposal, ControlTrigger.manual)
    return proposal, outcome, control


def test_d6_hold_level_is_at_least_the_minimum():
    props = PROPERTIES | {"minsocongrid": {"range": {"min": 10, "max": 100}}}
    schedule = Schedule.from_fox(FoxCloud(properties=props).client().scheduler(SN))
    plan = plan_for(
        utc(2026, 1, 11, 3, 0),
        utc(2026, 1, 11, 5, 0),
        hold=(utc(2026, 1, 11, 0, 30), utc(2026, 1, 11, 3, 0)),
    )
    proposal = service.propose(system_in(min_soc=20), InverterControl(), plan, schedule)
    failed = [c for c in check(proposal, "UTC") if c.status == "fail"]
    assert failed == []
    assert {(p.fd_soc, p.min_soc_on_grid) for p in proposal.added} == {(20, 20), (80, 20)}


def test_d6_owner_minimum_of_10_even_when_the_inverter_accepts_5():
    props = PROPERTIES | {
        "fdsoc": {"range": {"min": 5, "max": 100}},
        "minsocongrid": {"range": {"min": 5, "max": 100}},
    }
    schedule = Schedule.from_fox(FoxCloud(properties=props).client().scheduler(SN))
    plan = plan_for(
        utc(2026, 1, 11, 3, 0),
        utc(2026, 1, 11, 5, 0),
        target=7,
        hold=(utc(2026, 1, 11, 0, 30), utc(2026, 1, 11, 3, 0)),
        drain=(utc(2026, 1, 10, 23, 0), utc(2026, 1, 11, 0, 30)),
    )
    proposal = service.propose(system_in(min_soc=5), InverterControl(), plan, schedule)
    assert {(p.fd_soc, p.min_soc_on_grid) for p in proposal.added} == {(10, 10)}


def test_d11_newer_rate_limit_code_is_retried():
    cloud = FoxCloud()
    cloud.errors[SCHEDULER_PATH] = [40404]
    assert cloud.client().scheduler(SN)["enable"] == 1


# --- invariants on generated cases (Layer 4) -------------------------------------------------


def random_case(rng: random.Random):
    tz = rng.choice(["UTC", "Europe/London", "Europe/Berlin", "America/New_York"])
    base = datetime(2026, rng.choice([1, 3, 6, 10]), rng.randint(5, 25), tzinfo=timezone.utc)
    # The user's own periods: up to four, not overlapping each other, away from 22:00–07:00.
    own, hour = [], 8
    for _ in range(rng.randint(0, 4)):
        if hour > 20:
            break
        length = rng.randint(1, 3)
        extra = rng.choice([{}, {"maxSoc": 100}, {"pvLimit": 5000}])
        own.append(
            group((hour, 0), (hour + length - 1, 59), rng.choice(["SelfUse", "Feedin"]), **extra)
        )
        hour += length + rng.randint(0, 2)
    opens = base + timedelta(hours=rng.choice([22, 23, 24, 25]), minutes=rng.choice([0, 30]))
    closes = opens + timedelta(hours=rng.randint(3, 6))
    charge_from = opens + (closes - opens) * rng.random()
    charge_from = charge_from.replace(minute=charge_from.minute // 5 * 5, second=0, microsecond=0)
    hold = (opens, charge_from) if charge_from > opens and rng.random() < 0.6 else None
    drain = (
        (opens - timedelta(minutes=rng.choice([30, 60, 90])), opens) if rng.random() < 0.3 else None
    )
    charge_kwh = rng.choice([0, 1.5, 6.0])
    plan = plan_for(
        charge_from,
        closes,
        target=rng.uniform(15, 100),
        charge_kwh=charge_kwh,
        hold=hold,
        drain=drain,
    )
    return tz, own, rng.choice([0, 1]), plan


@pytest.mark.parametrize("seed", range(150))
def test_generated_plans_keep_the_invariants_end_to_end(seed, writes_on, logged):
    rng = random.Random(seed)
    tz, own, enable, plan = random_case(rng)
    cloud = FoxCloud(own, enable=enable)
    system = system_in(tz)
    proposal, outcome, _ = apply_scenario(cloud, plan, system)
    failed = [c for c in check(proposal, tz) if c.status == "fail"]
    assert failed == [], failed
    if outcome.status == ControlStatus.skipped:
        assert cloud.writes() == []
        return
    assert outcome.status == ControlStatus.ok, outcome.message
    # What's on the inverter is the proposal, and the user's untouched periods are as they were.
    assert cloud.groups == [p.to_fox() for p in proposal.after]
    removed = {id(p) for p in proposal.removed}
    for p in proposal.schedule.periods:
        if id(p) not in removed:
            assert v3_group(p.raw) in cloud.groups


# --- dry run ---------------------------------------------------------------------------------


def test_dry_run_reads_once_writes_nothing_and_matches_apply(writes_on, logged):
    own = group((16, 0), (18, 59), "Feedin", fdSoc=10, fdPwr=0, minSocOnGrid=10)
    cloud = FoxCloud([own, group((7, 0), (7, 59), "ForceDischarge") | {"enable": 0}])
    system = system_in("Europe/London")
    plan = plan_for(
        utc(2026, 1, 11, 3, 0),
        utc(2026, 1, 11, 5, 0),
        hold=(utc(2026, 1, 11, 0, 30), utc(2026, 1, 11, 3, 0)),
    )
    dry = dryrun.build(system, None, cloud.client(writes_allowed=False), plan)
    assert [r["path"] for r in cloud.requests] == [SCHEDULER_PATH]
    assert not dry.failed
    # V3 hides the switched-off period, so the preview can only say it may be lost.
    assert any("doesn't show switched-off periods" in n for n in dry.proposal.notes)

    text = dryrun.render(dry, system)
    assert "Nothing was sent to the inverter" in text
    assert SN not in text and "…0001" in text
    assert "Finish with SAFE or NOT SAFE." in text
    # The schedule is on already, so the one V3 write is all; no switch call.
    ((path, groups_body),) = dry.requests
    assert path == SCHEDULER_SET_PATH and groups_body["isDefault"] is False
    assert json.dumps(groups_body["groups"], indent=2).replace("\n", "\n  ") in text

    # Applying the same plan sends exactly what the dry run showed.
    cloud.requests.clear()
    apply_scenario(cloud, plan, system)
    assert [(r["path"], r["body"]) for r in cloud.writes()] == dry.requests


def test_d2_switched_off_periods_that_are_reported_are_warned_about():
    raw = {
        "enable": "1",
        "groups": [
            group((16, 0), (18, 59), "Feedin"),
            group((7, 0), (7, 59), "ForceDischarge") | {"enable": 0},
            # V1's empty padding isn't a period.
            group((0, 0), (0, 0)) | {"enable": 0},
        ],
        "properties": PROPERTIES,
    }
    schedule = Schedule.from_fox(raw)
    assert [p.label() for p in schedule.switched_off] == ["07:00–08:00"]
    plan = plan_for(utc(2026, 1, 11, 2, 0), utc(2026, 1, 11, 5, 0))
    proposal = service.propose(system_in(), InverterControl(), plan, schedule)
    assert any("switched-off periods will be lost" in n for n in proposal.notes)
    assert any(c.status == "warn" and "D2" in c.name for c in check(proposal, "UTC"))


@pytest.mark.parametrize("value, on", [("0", False), ("1", True), (0, False), (1, True)])
def test_d9_master_switch_read_as_string_or_number(value, on):
    assert Schedule.from_fox({"enable": value, "groups": []}).enabled is on


def test_d7_max_soc_on_every_forced_period_when_reported():
    props = PROPERTIES | {"maxsoc": {"range": {"min": 10, "max": 100}}}
    schedule = Schedule.from_fox(FoxCloud(properties=props).client().scheduler(SN))
    plan = plan_for(
        utc(2026, 1, 11, 3, 0),
        utc(2026, 1, 11, 5, 0),
        hold=(utc(2026, 1, 11, 0, 30), utc(2026, 1, 11, 3, 0)),
        drain=(utc(2026, 1, 10, 23, 0), utc(2026, 1, 11, 0, 30)),
    )
    proposal = service.propose(system_in(), InverterControl(), plan, schedule)
    assert {p.to_fox()["extraParam"]["maxSoc"] for p in proposal.added} == {100}


def test_d8_stages_meeting_on_a_second_dont_overlap():
    meet = utc(2026, 1, 11, 3, 0) + timedelta(seconds=20)
    plan = plan_for(meet, utc(2026, 1, 11, 5, 0), hold=(utc(2026, 1, 11, 0, 30), meet))
    schedule = Schedule.from_fox(FoxCloud().client().scheduler(SN))
    proposal = service.propose(system_in(), InverterControl(), plan, schedule)
    hold, charge = proposal.added
    assert (hold.end, charge.start) == (3 * 60, 3 * 60 + 1)


def test_all_day_default_period_is_never_sent_and_is_checked(writes_on, logged):
    default = group((0, 0), (23, 59), "SelfUse", minSocOnGrid=10, maxSoc=100)
    cloud = FoxCloud([group((16, 0), (18, 59), "Feedin")], default=default)
    plan = plan_for(utc(2026, 1, 11, 2, 0), utc(2026, 1, 11, 5, 0))
    proposal, outcome, _ = apply_scenario(cloud, plan)
    assert outcome.status == ControlStatus.ok, outcome.message
    sent = [Period.from_fox(g) for g in cloud.groups]
    assert all(not p.all_day for p in sent)
    # D13: Fox dropped the default, and the whole day is written, so nothing is left uncovered.
    assert cloud.default is None
    assert [(p.label(), p.work_mode) for p in sent] == [
        ("00:00–02:00", "SelfUse"),
        ("02:00–05:00", "ForceCharge"),
        ("05:00–00:00", "SelfUse"),
    ]
    assert any("whole day is written" in n for n in proposal.notes)
    golden(
        "E9-all-day-default",
        cloud,
        outcome,
        "Fox's all-day default isn't sent; the whole day is written instead",
    )

    # A write that still leaves a time with no period is reported, not passed.
    cloud.requests.clear()
    control = InverterControl(written_groups=[a.to_fox() for a in proposal.added])
    schedule_with_default = proposal.schedule
    outcome = service.clear(None, system_in(), control, cloud.client())
    assert outcome.status == ControlStatus.ok  # it read no default before this write
    action, _ = service._run_write(
        None, system_in(), cloud.client(), "test", "manual", schedule_with_default, [], True, "done"
    )
    assert action.status == ControlStatus.mismatch
    assert "all-day SelfUse period" in action.message


def test_schedule_on_already_is_not_switched_again(writes_on, logged):
    cloud = FoxCloud(enable=1)
    apply_scenario(cloud, plan_for(utc(2026, 1, 11, 2, 0), utc(2026, 1, 11, 5, 0)))
    assert [r["path"] for r in cloud.writes()] == [SCHEDULER_SET_PATH]


def test_old_v1_original_is_restored_in_v3_shape(writes_on, logged):
    from tests.test_control import v1_group

    cloud = FoxCloud([group((9, 0), (9, 59), "SelfUse")])
    control = InverterControl(
        original_schedule={
            "enable": 1,
            "groups": [
                v1_group((16, 0), (18, 59), "Feedin", fdSoc="10", minSocOnGrid=10),
                v1_group((0, 0), (0, 0)) | {"enable": 0},
            ],
        }
    )
    outcome = service.restore(None, system_in(), control, cloud.client())
    assert outcome.status == ControlStatus.ok, outcome.message
    assert cloud.groups == [
        {
            "startHour": 16,
            "startMinute": 0,
            "endHour": 18,
            "endMinute": 59,
            "workMode": "Feedin",
            "extraParam": {"fdSoc": 10, "minSocOnGrid": 10},
        }
    ]


@pytest.mark.parametrize("path", [SCHEDULER_PATH, SCHEDULER_SET_PATH, SCHEDULER_FLAG_PATH])
@pytest.mark.parametrize("errno", [40257, 41201, 41808])
def test_layer3_errors_at_each_call_leave_a_known_state(writes_on, logged, path, errno):
    """Layer 3: a Fox error at each call. Retried errors pass; others fail, nothing is lost
    track of, and Remove still works afterwards."""
    cloud = FoxCloud(enable=0)
    calls = {"n": 0}
    real = cloud.handle

    def fail_once(request):
        if request.url.path == path and not calls["n"]:
            calls["n"] += 1
            request.read()
            cloud.requests.append({"method": "POST", "path": path, "body": {}})
            return httpx.Response(200, json={"errno": errno, "msg": "injected"})
        return real(request)

    cloud.handle = fail_once
    system, control = system_in(), InverterControl()
    client = cloud.client()
    plan = plan_for(utc(2026, 1, 11, 2, 0), utc(2026, 1, 11, 5, 0))
    try:
        proposal = service.propose(system, control, plan, service.read_schedule(client, SN))
    except FoxEssError:
        assert path == SCHEDULER_PATH and errno != 41201
        assert cloud.writes() == []
        return
    outcome = service.apply(None, system, control, client, proposal, ControlTrigger.manual)
    if errno == 41201:
        assert outcome.status == ControlStatus.ok, outcome.message
    else:
        assert outcome.status == ControlStatus.failed
    on_inverter = [g for g in cloud.groups]
    if on_inverter:
        assert control.written_groups, "periods on the inverter must be known to Remove"
    service.clear(None, system, control, cloud.client())
    assert forced(cloud.groups) == []
