import copy
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.api.control import get_control_client_factory
from app.api.imports import get_fox_client_factory
from app.control import service, testing
from app.control.nightly import run_once
from app.control.schedule import Period, Schedule, charge_periods, matches, merge
from app.main import app
from app.models import Battery, ControlStatus, ControlTrigger, InverterControl, PlanningMode
from tests.conftest import register
from tests.test_forecast_api import weather  # noqa: F401 (fixture)
from tests.test_imports_api import UTC_SYSTEM, FakeFox

PROPERTIES = {
    "fdsoc": {"range": {"min": 10, "max": 100}},
    "fdpwr": {"range": {"min": 0, "max": 6000}},
    "minsocongrid": {"range": {"min": 10, "max": 100}},
}


def group(start, end, mode="SelfUse", **extra):
    return {
        "enable": 1,
        "startHour": start[0],
        "startMinute": start[1],
        "endHour": end[0],
        "endMinute": end[1],
        "workMode": mode,
        **extra,
    }


class FakeInverter:
    """Holds a schedule the way Fox's cloud does, and records every write."""

    def __init__(self, groups=None, enable=1, properties=PROPERTIES):
        self.enable = enable
        self.groups = copy.deepcopy(groups or [])
        self.properties = properties
        self.writes = []
        self.fail_writes = False

    def __call__(self, api_key):
        return self

    def scheduler(self, sn):
        filler = [group((0, 0), (0, 0)) | {"enable": 0}] * (8 - len(self.groups))
        return {
            "enable": self.enable,
            "maxGroupCount": 8,
            "groups": copy.deepcopy(self.groups) + filler,
            "properties": self.properties,
        }

    def set_scheduler_groups(self, sn, groups):
        from app.importers.fox_ess import FoxEssError

        if self.fail_writes:
            raise FoxEssError("Fox ESS error 44098: Write Failed")
        self.writes.append(("groups", copy.deepcopy(groups)))
        self.groups = copy.deepcopy(groups)

    def set_scheduler_enabled(self, sn, enabled):
        self.writes.append(("enable", enabled))
        self.enable = int(enabled)


# --- schedule building -----------------------------------------------------------------------


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_charge_period_splits_at_midnight_with_inclusive_ends():
    first, second = charge_periods(
        utc(2026, 1, 10, 23, 40), utc(2026, 1, 11, 5, 30), "Europe/London", 80, 3600, 10
    )
    assert (first.start, first.end) == (23 * 60 + 40, 23 * 60 + 59)
    assert (second.start, second.end) == (0, 5 * 60 + 29)
    assert first.label() == "23:40–00:00"
    assert second.to_fox() | {} == {
        "enable": 1,
        "startHour": 0,
        "startMinute": 0,
        "endHour": 5,
        "endMinute": 29,
        "workMode": "ForceCharge",
        "fdSoc": 80,
        "fdPwr": 3600,
        "minSocOnGrid": 10,
    }


def test_charge_period_uses_local_time():
    # 01:00 UTC in June is 02:00 in London.
    (period,) = charge_periods(
        utc(2026, 6, 10, 1, 0), utc(2026, 6, 10, 3, 0), "Europe/London", 60, 3000, 10
    )
    assert (period.start, period.end) == (120, 239)


def test_merge_replaces_only_overlapping_periods():
    own = [Period.from_fox(group((0, 0), (1, 59))), Period.from_fox(group((16, 0), (18, 59)))]
    added = charge_periods(utc(2026, 1, 10, 1, 0), utc(2026, 1, 10, 5, 0), "UTC", 80, 3000, 10)
    after, removed = merge(own, added)
    assert removed == [own[0]]
    assert [p.label() for p in after] == ["01:00–05:00", "16:00–19:00"]


def test_matches_reports_differences():
    written = charge_periods(utc(2026, 1, 10, 1, 0), utc(2026, 1, 10, 5, 0), "UTC", 80, 3000, 10)
    assert matches(written, written) == []
    changed = Period.from_fox(written[0].to_fox() | {"fdSoc": 100})
    assert "charge level" in matches(written, [changed])[0]
    assert "missing" in matches(written, [])[0]


def plan_for(start, stop, target=80.0, charge_kwh=4.0, hold=None, drain=None):
    rec = SimpleNamespace(
        discharge_start=drain[0] if drain else None,
        discharge_stop=drain[1] if drain else None,
        discharge_to_percent=5.0 if drain else None,
        hold_start=hold[0] if hold else None,
        hold_stop=hold[1] if hold else None,
        mode=PlanningMode.maximise_own_usage,
        target_soc_percent=target,
        charge_kwh=charge_kwh,
        force_charge_start=start,
        force_charge_stop=stop,
        force_charge_reaches_target=True,
    )
    return SimpleNamespace(day=date(2026, 1, 11), recommendations=[rec])


def system_with(min_soc=5.0):
    battery = Battery(
        capacity_kwh=10, min_soc_percent=min_soc, max_charge_kw=3.6, max_discharge_kw=3.6
    )
    return SimpleNamespace(battery=battery, timezone="UTC")


def test_propose_clamps_to_the_inverters_ranges_and_explains():
    schedule = Schedule.from_fox(FakeInverter([group((16, 0), (18, 59))]).scheduler("x"))
    proposal = service.propose(
        system_with(min_soc=5),
        InverterControl(),
        plan_for(utc(2026, 1, 11, 2, 0), utc(2026, 1, 11, 5, 30), target=79.2),
        schedule,
    )
    (added,) = proposal.added
    assert (added.fd_soc, added.fd_pwr, added.min_soc_on_grid) == (80, 3600, 10)
    assert any("accepts 10% or more" in n for n in proposal.notes)
    assert any("repeat every day" in n for n in proposal.notes)
    assert [p.label() for p in proposal.after] == ["02:00–05:30", "16:00–19:00"]


def test_propose_replaces_last_nights_charge_even_when_it_doesnt_overlap():
    last_night = group((1, 0), (1, 59), "ForceCharge", fdSoc=90, fdPwr=3600, minSocOnGrid=10)
    schedule = Schedule.from_fox(FakeInverter([last_night]).scheduler("x"))
    control = InverterControl(written_groups=[last_night])
    proposal = service.propose(
        system_with(), control, plan_for(utc(2026, 1, 11, 4, 0), utc(2026, 1, 11, 5, 0)), schedule
    )
    assert [p.label() for p in proposal.after] == ["04:00–05:00"]
    assert [p.label() for p in proposal.removed] == ["01:00–02:00"]


def test_no_charge_writes_no_force_charge():
    schedule = Schedule.from_fox(FakeInverter().scheduler("x"))
    proposal = service.propose(
        system_with(), InverterControl(), plan_for(None, None, charge_kwh=0), schedule
    )
    assert proposal.added == [] and proposal.after == []
    assert "No charge is needed" in proposal.notes[0]


def test_hold_before_the_charge_is_a_forced_charge_to_the_floor():
    schedule = Schedule.from_fox(FakeInverter().scheduler("x"))
    proposal = service.propose(
        system_with(),
        InverterControl(),
        plan_for(
            utc(2026, 1, 11, 3, 40),
            utc(2026, 1, 11, 5, 30),
            hold=(utc(2026, 1, 10, 23, 30), utc(2026, 1, 11, 3, 40)),
        ),
        schedule,
    )
    labels = [(p.label(), p.fd_soc) for p in proposal.after]
    assert labels == [("00:00–03:40", 10), ("03:40–05:30", 80), ("23:30–00:00", 10)]
    assert proposal.hold_stop == proposal.start
    assert any("battery is held" in n for n in proposal.notes)


def test_no_charge_still_holds_the_battery_through_the_window():
    schedule = Schedule.from_fox(FakeInverter().scheduler("x"))
    proposal = service.propose(
        system_with(),
        InverterControl(),
        plan_for(None, None, charge_kwh=0, hold=(utc(2026, 1, 11, 0, 30), utc(2026, 1, 11, 5, 30))),
        schedule,
    )
    assert [(p.label(), p.fd_soc) for p in proposal.after] == [("00:30–05:30", 10)]
    assert proposal.start is None and proposal.target_soc_percent is None


def test_discharge_before_a_free_window_is_a_forced_discharge():
    schedule = Schedule.from_fox(FakeInverter().scheduler("x"))
    proposal = service.propose(
        system_with(min_soc=5),
        InverterControl(),
        plan_for(
            utc(2026, 1, 11, 3, 0),
            utc(2026, 1, 11, 4, 0),
            hold=(utc(2026, 1, 11, 0, 0), utc(2026, 1, 11, 3, 0)),
            drain=(utc(2026, 1, 10, 23, 0), utc(2026, 1, 11, 0, 0)),
        ),
        schedule,
    )
    modes = [(p.label(), p.work_mode, p.fd_soc, p.fd_pwr) for p in proposal.after]
    assert modes == [
        ("00:00–03:00", "ForceCharge", 10, 3600),
        ("03:00–04:00", "ForceCharge", 80, 3600),
        ("23:00–00:00", "ForceDischarge", 10, 3600),
    ]
    assert proposal.discharge_soc_percent == 10
    assert any("stops at 10% rather than 5%" in n for n in proposal.notes)


def test_full_schedule_is_refused():
    groups = [group((h, 0), (h, 29)) for h in range(1, 9)]
    schedule = Schedule.from_fox(FakeInverter(groups).scheduler("x"))
    with pytest.raises(service.ControlError):
        service.propose(
            system_with(),
            InverterControl(),
            plan_for(utc(2026, 1, 11, 12, 0), utc(2026, 1, 11, 13, 0)),
            schedule,
        )


# --- API -------------------------------------------------------------------------------------


@pytest.fixture
def inverter():
    fake = FakeInverter([group((16, 0), (18, 59), "Feedin", fdSoc=10, fdPwr=0, minSocOnGrid=10)])
    app.dependency_overrides[get_control_client_factory] = lambda: fake
    return fake


@pytest.fixture
def headers(client, weather, inverter):  # noqa: F811
    app.dependency_overrides[get_fox_client_factory] = lambda: FakeFox
    h = register(client)
    assert client.put("/api/system", json=UTC_SYSTEM, headers=h).status_code == 200
    resp = client.put(
        "/api/import/fox-ess", json={"api_key": "good-key-123", "device_sn": "inv0001"}, headers=h
    )
    assert resp.status_code == 200, resp.text
    return h


def opt_in(client, headers):
    resp = client.put("/api/control", json={"enabled": True, "accept": True}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def step(client, headers, n):
    resp = client.post("/api/control/test", json={"step": n}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_control_is_off_by_default_and_needs_explicit_consent(client, headers, inverter):
    status = client.get("/api/control", headers=headers).json()
    assert status["enabled"] is False and status["can_apply"] is False
    assert client.get("/api/control/preview", headers=headers).status_code == 403
    assert client.post("/api/control/test", json={"step": 1}, headers=headers).status_code == 403
    resp = client.put("/api/control", json={"enabled": True}, headers=headers)
    assert resp.status_code == 422
    status = opt_in(client, headers)
    assert status["enabled"] is True and status["consented_at"]
    assert status["actions"][0]["kind"] == "opt_in"
    assert inverter.writes == []


def test_write_test_ladder_then_apply_clear_restore(client, headers, inverter):
    opt_in(client, headers)
    original = copy.deepcopy(inverter.groups)

    # Applying is locked until the write test reaches step 4, and steps run in order.
    preview = client.get("/api/control/preview", headers=headers)
    assert preview.status_code == 200, preview.text
    apply = {"fingerprint": preview.json()["fingerprint"]}
    assert client.post("/api/control/apply", json=apply, headers=headers).status_code == 409
    assert client.post("/api/control/test", json={"step": 3}, headers=headers).status_code == 409

    assert step(client, headers, 1)["status"] == "ok"
    assert inverter.writes == []  # step 1 only reads
    assert step(client, headers, 2)["status"] == "ok"
    assert inverter.groups == original

    added = step(client, headers, 3)
    assert added["status"] == "ok", added["message"]
    assert len(inverter.groups) == 2
    test_group = next(g for g in inverter.groups if g["workMode"] == "ForceCharge")
    assert test_group["fdPwr"] == testing.TEST_POWER_W
    assert (test_group["endHour"] * 60 + test_group["endMinute"]) - (
        test_group["startHour"] * 60 + test_group["startMinute"]
    ) == testing.TEST_MINUTES - 1

    removed = step(client, headers, 4)
    assert removed["status"] == "ok", removed["message"]
    assert inverter.groups == original
    status = client.get("/api/control", headers=headers).json()
    assert status["test_level"] == 4 and status["can_apply"] is True
    assert status["can_auto_apply"] is False  # needs a manual apply first

    # Auto-apply stays locked until a plan has been applied by hand.
    resp = client.put("/api/control", json={"enabled": True, "auto_apply": True}, headers=headers)
    assert resp.status_code == 409

    # A stale fingerprint is refused; the current one writes exactly what was previewed.
    bad = client.post("/api/control/apply", json={"fingerprint": "stale"}, headers=headers)
    assert bad.status_code == 409
    preview = client.get("/api/control/preview", headers=headers).json()
    assert preview["added"] and preview["target_soc_percent"] >= 10
    done = client.post(
        "/api/control/apply", json={"fingerprint": preview["fingerprint"]}, headers=headers
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "ok", done.json()["message"]
    assert inverter.groups == preview["request"]["groups"]
    assert done.json()["sent"]["groups"] == preview["request"]["groups"]

    resp = client.put("/api/control", json={"enabled": True, "auto_apply": True}, headers=headers)
    assert resp.status_code == 200 and resp.json()["auto_apply"] is True

    cleared = client.post("/api/control/clear", headers=headers).json()
    assert cleared["status"] in ("ok", "skipped")
    assert inverter.groups == original

    restored = client.post("/api/control/restore", headers=headers).json()
    assert restored["status"] == "ok", restored["message"]
    assert inverter.groups == original and inverter.enable == 1

    log = client.get("/api/control", headers=headers).json()["actions"]
    assert {a["kind"] for a in log} >= {"opt_in", "test", "apply", "clear", "restore"}


def test_failed_write_is_logged_and_unlocks_nothing(client, headers, inverter):
    opt_in(client, headers)
    step(client, headers, 1)
    inverter.fail_writes = True
    result = step(client, headers, 2)
    assert result["status"] == "failed"
    assert "44098" in result["message"]
    assert client.get("/api/control", headers=headers).json()["test_level"] == 1


def test_read_back_mismatch_is_reported(client, headers, inverter):
    opt_in(client, headers)
    step(client, headers, 1)
    step(client, headers, 2)

    real_set = inverter.set_scheduler_groups

    def ignores_power(sn, groups):
        real_set(sn, [g | {"fdPwr": 0} if g["workMode"] == "ForceCharge" else g for g in groups])

    inverter.set_scheduler_groups = ignores_power
    result = step(client, headers, 3)
    assert result["status"] == "mismatch"
    assert "charge power" in result["message"]
    assert client.get("/api/control", headers=headers).json()["test_level"] == 2
    # The test period is still recorded, so it can be removed.
    assert client.post("/api/control/clear", headers=headers).json()["status"] == "ok"


def test_opt_out_stops_writes_without_touching_the_inverter(client, headers, inverter):
    opt_in(client, headers)
    step(client, headers, 1)
    resp = client.put("/api/control", json={"enabled": False}, headers=headers)
    assert resp.json()["enabled"] is False
    assert resp.json()["actions"][0]["kind"] == "opt_out"
    assert client.post("/api/control/test", json={"step": 2}, headers=headers).status_code == 403
    assert inverter.writes == []


def test_nightly_applies_once_in_the_evening(client, headers, inverter, weather):  # noqa: F811
    from app.database import get_session_factory

    opt_in(client, headers)
    for n in (1, 2, 3, 4):
        step(client, headers, n)
    preview = client.get("/api/control/preview", headers=headers).json()
    client.post("/api/control/apply", json={"fingerprint": preview["fingerprint"]}, headers=headers)
    client.put("/api/control", json={"enabled": True, "auto_apply": True}, headers=headers)

    factory = app.dependency_overrides[get_session_factory]()
    with factory() as db:
        control = db.query(InverterControl).one()
        system = control.system
        from app.planner.simulator import cycle_for
        from app.tariffs import TariffSchedule

        today = datetime.now(timezone.utc).date()
        cycle = cycle_for(today + timedelta(days=1), TariffSchedule.from_system(system), "UTC")
        early = cycle.night_start - timedelta(hours=5)
        assert run_once(db, inverter, weather, early) == []
        evening = cycle.night_start - timedelta(hours=1)
        (action,) = run_once(db, inverter, weather, evening)
        assert action.trigger == ControlTrigger.auto
        assert action.status == ControlStatus.ok, action.message
        assert run_once(db, inverter, weather, evening + timedelta(minutes=10)) == []


def test_apply_with_nothing_to_change_writes_nothing(monkeypatch):
    monkeypatch.setattr(
        service, "_log", lambda db, system, kind, trigger, status, message, **kw: status
    )
    inverter = FakeInverter()
    schedule = Schedule.from_fox(inverter.scheduler("x"))
    system = system_with()
    system.fox_ess = SimpleNamespace(device_sn="INV1")
    control = InverterControl()
    proposal = service.propose(system, control, plan_for(None, None, charge_kwh=0), schedule)
    status = service.apply(None, system, control, inverter, proposal, ControlTrigger.manual)
    assert status == ControlStatus.skipped
    assert inverter.writes == []
