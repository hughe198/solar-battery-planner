"""Layer 1 of docs/inverter-api-test-plan.md: the exact requests sent to Fox ESS.

Unlike test_control.py, which swaps the Fox client for a fake, these run the real FoxEssClient,
signing and all, against a fake Fox cloud behind httpx.MockTransport, and record every request.
Each scenario's requests are compared with a golden file in tests/golden/inverter/, so any
change to what reaches an inverter shows up as a diff for a person to approve.

To accept a deliberate change: UPDATE_GOLDEN=1 pytest tests/test_fox_requests.py, then review the
diff of tests/golden/inverter/ before committing it.

Known defects are strict xfail tests: they fail today and must start passing when fixed, at
which point pytest reports them and the marker comes off.
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
from app.control.schedule import Schedule
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
    """Fox's cloud as far as the v1 scheduler goes: holds one inverter's schedule, checks each
    request is signed, and records every request in order."""

    def __init__(self, groups=(), enable=1, properties=PROPERTIES, max_groups=8):
        self.groups = copy.deepcopy(list(groups))
        self.enable = enable
        self.properties = properties
        self.max_groups = max_groups
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
            filler = [group((0, 0), (0, 0)) | {"enable": 0}] * (self.max_groups - len(self.groups))
            result = {
                "enable": self.enable,
                "maxGroupCount": self.max_groups,
                "groups": copy.deepcopy(self.groups) + filler,
                "properties": self.properties,
            }
            return httpx.Response(200, json={"errno": 0, "result": result})
        if path == SCHEDULER_SET_PATH:
            self.groups = copy.deepcopy(body["groups"])
        elif path == SCHEDULER_FLAG_PATH:
            self.enable = body["enable"]
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"errno": 0, "result": None})

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
    assert headers["signature"] == "0a5de6a3261abf839ad86a78fb592d73"


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
    cloud = FoxCloud([group((16, 0), (18, 59))])
    _, outcome, _ = apply_scenario(cloud, plan_for(None, None, charge_kwh=0))
    assert outcome.status == ControlStatus.skipped
    assert cloud.writes() == []
    golden("S6-nothing-to-do", cloud, outcome, "No charge or hold: one read, no write")


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
    assert (cloud.groups[0]["startHour"], cloud.groups[0]["endHour"]) == (2, 3)
    golden("T5-london-summer", cloud, outcome, "01:00–03:00 UTC is written as 02:00–04:00 BST")


def test_e2_users_periods_go_back_field_for_field(writes_on, logged):
    own = [
        group((7, 0), (8, 59), "SelfUse", maxSoc="100", pvLimit=4000, unknownField="kept"),
        group((16, 0), (18, 59), "Feedin", fdSoc=10, fdPwr=0, minSocOnGrid=10),
    ]
    cloud = FoxCloud(own)
    plan = plan_for(utc(2026, 1, 11, 2, 0), utc(2026, 1, 11, 5, 0))
    _, outcome, _ = apply_scenario(cloud, plan)
    assert outcome.status == ControlStatus.ok, outcome.message
    for g in own:
        assert g in cloud.groups
    golden(
        "E2-users-periods-kept",
        cloud,
        outcome,
        "User's two periods kept exactly, unknown fields too",
    )


def test_e5_last_nights_charge_is_replaced(writes_on, logged):
    cloud = FoxCloud()
    _, _, control = apply_scenario(cloud, plan_for(utc(2026, 1, 11, 1, 0), utc(2026, 1, 11, 2, 0)))
    cloud.requests.clear()
    plan = plan_for(utc(2026, 1, 12, 4, 0), utc(2026, 1, 12, 5, 0))
    _, outcome, _ = apply_scenario(cloud, plan, control=control)
    assert [(g["startHour"], g["endHour"]) for g in cloud.groups] == [(4, 4)]
    golden(
        "E5-replace-last-night", cloud, outcome, "Yesterday's 01:00–02:00 replaced by 04:00–05:00"
    )


def test_a2_clear_keeps_the_users_periods(writes_on, logged):
    own = group((16, 0), (18, 59), "Feedin", fdSoc=10, fdPwr=0, minSocOnGrid=10)
    cloud = FoxCloud([own])
    _, _, control = apply_scenario(cloud, plan_for(utc(2026, 1, 11, 2, 0), utc(2026, 1, 11, 5, 0)))
    cloud.requests.clear()
    outcome = service.clear(None, system_in(), control, cloud.client())
    assert outcome.status == ControlStatus.ok, outcome.message
    assert cloud.groups == [own]
    golden("A2-clear", cloud, outcome, "Remove the planner's charge, keep the user's period")


def test_a4_restore_puts_back_the_original(writes_on, logged):
    own = group((16, 0), (18, 59), "Feedin", fdSoc=10, fdPwr=0, minSocOnGrid=10)
    cloud = FoxCloud([own], enable=0)
    _, _, control = apply_scenario(cloud, plan_for(utc(2026, 1, 11, 2, 0), utc(2026, 1, 11, 5, 0)))
    cloud.requests.clear()
    outcome = service.restore(None, system_in(), control, cloud.client())
    assert outcome.status == ControlStatus.ok, outcome.message
    assert (cloud.groups, cloud.enable) == ([own], 0)
    golden("A4-restore", cloud, outcome, "Original schedule back, switched off as it was")


# --- known defects: strict xfail until fixed -------------------------------------------------


@pytest.mark.xfail(strict=True, raises=TypeError, reason="D1: hold-only apply crashes")
def test_d1_hold_only_night_applies(writes_on, logged):
    cloud = FoxCloud()
    plan = plan_for(
        None, None, charge_kwh=0, hold=(utc(2026, 1, 11, 0, 30), utc(2026, 1, 11, 5, 30))
    )
    _, outcome, _ = apply_scenario(cloud, plan)
    assert outcome.status == ControlStatus.ok


@pytest.mark.xfail(strict=True, reason="D3: removing the last period only switches off")
def test_d3_clearing_the_last_period_removes_it(writes_on, logged):
    cloud = FoxCloud()
    _, _, control = apply_scenario(cloud, plan_for(utc(2026, 1, 11, 2, 0), utc(2026, 1, 11, 5, 0)))
    service.clear(None, system_in(), control, cloud.client())
    assert cloud.groups == []


@pytest.mark.xfail(strict=True, reason="D4: periods written before a failed flag call are lost")
def test_d4_failed_switch_on_still_records_what_was_written(writes_on, logged):
    cloud = FoxCloud(enable=0)
    cloud.errors[SCHEDULER_FLAG_PATH] = [44098]
    _, outcome, control = apply_scenario_unchecked(cloud)
    assert outcome.status == ControlStatus.failed
    assert cloud.groups, "the periods did reach the inverter"
    assert control.written_groups, "so the app must remember them for Remove"


def apply_scenario_unchecked(cloud):
    system, control = system_in(), InverterControl()
    client = cloud.client()
    plan = plan_for(utc(2026, 1, 11, 2, 0), utc(2026, 1, 11, 5, 0))
    proposal = service.propose(system, control, plan, service.read_schedule(client, SN))
    outcome = service.apply(None, system, control, client, proposal, ControlTrigger.manual)
    return proposal, outcome, control


@pytest.mark.xfail(strict=True, reason="D6: hold fdSoc below minSocOnGrid")
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


@pytest.mark.xfail(strict=True, raises=FoxEssError, reason="D11: 40404 isn't retried")
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
    if charge_kwh == 0:
        # A hold or discharge with no charge hits D1. Drop this when D1 is fixed.
        hold = drain = None
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
            assert {**p.raw, "enable": 1} in cloud.groups


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
    assert any(c.status == "warn" and "D2" in c.name for c in dry.checks)

    text = dryrun.render(dry, system)
    assert "Nothing was sent to the inverter" in text
    assert SN not in text and "…0001" in text
    assert "Finish with SAFE or NOT SAFE." in text
    (_, groups_body), (_, flag_body) = dry.requests
    assert json.dumps(groups_body["groups"], indent=2).replace("\n", "\n  ") in text
    assert flag_body["enable"] == 1

    # Applying the same plan sends exactly what the dry run showed.
    cloud.requests.clear()
    apply_scenario(cloud, plan, system)
    assert [(r["path"], r["body"]) for r in cloud.writes()] == dry.requests
