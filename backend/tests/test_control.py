import copy
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.api.control import get_control_client_factory
from app.api.imports import get_fox_client_factory
from app.config import settings
from app.control import service, testing
from app.control.nightly import run_once
from app.control.schedule import Period, Schedule, charge_periods, fill_gaps, matches, merge
from app.main import app
from app.models import (
    Battery,
    ControlStatus,
    ControlTrigger,
    InverterControl,
    LockedPlan,
    PlanningMode,
)
from tests.conftest import register
from tests.test_forecast_api import weather  # noqa: F401 (fixture)
from tests.test_imports_api import UTC_SYSTEM, FakeFox

PROPERTIES = {
    "fdsoc": {"range": {"min": 10, "max": 100}},
    "fdpwr": {"range": {"min": 0, "max": 6000}},
    "minsocongrid": {"range": {"min": 10, "max": 100}},
}


SETTINGS = {"fdSoc", "fdPwr", "minSocOnGrid", "maxSoc", "importLimit", "exportLimit", "pvLimit"}


def group(start, end, mode="SelfUse", **extra):
    """A period as Fox's Scheduler V3 reports it: settings in extraParam, no `enable`."""
    settings_ = {k: v for k, v in extra.items() if k in SETTINGS}
    g = {
        "startHour": start[0],
        "startMinute": start[1],
        "endHour": end[0],
        "endMinute": end[1],
        "workMode": mode,
        **{k: v for k, v in extra.items() if k not in SETTINGS},
    }
    if settings_:
        g["extraParam"] = settings_
    return g


def v1_group(start, end, mode="SelfUse", **extra):
    """A period as Fox's deprecated V1 reported it, as stored before the move to V3."""
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
        return {
            "enable": self.enable,
            "maxGroupCount": 8,
            "groups": copy.deepcopy(self.groups),
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
    assert second.to_fox() == {
        "startHour": 0,
        "startMinute": 0,
        "endHour": 5,
        "endMinute": 29,
        "workMode": "ForceCharge",
        "extraParam": {"fdSoc": 80, "fdPwr": 3600, "minSocOnGrid": 10},
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
    after, removed, trimmed = merge(own, added)
    assert removed == [own[0]]
    # D5: what's left of the overlapped period outside the charge is kept.
    assert [p.label() for p in trimmed] == ["00:00–01:00"]
    assert [p.label() for p in after] == ["00:00–01:00", "01:00–05:00", "16:00–19:00"]


def test_merge_replaces_overlapped_force_charge_wholly():
    # D12: the owner's force charge 02:45-05:00 (stored ending 05:00, so through 05:01) and
    # the planner's 02:10-05:00 (ending 04:59). Trimming left a one-minute force charge to the
    # owner's 67% at 05:00, repeating every night. A forced period isn't trimmed: it's replaced.
    mine = Period.from_fox(group((2, 45), (5, 0), "ForceCharge", fdSoc=67, fdPwr=12000))
    self_use = Period.from_fox(group((5, 1), (16, 0)))
    added = charge_periods(utc(2026, 11, 6, 2, 10), utc(2026, 11, 6, 5, 0), "UTC", 44, 3000, 10)
    after, removed, trimmed = merge([mine, self_use], added)
    assert removed == [mine]
    assert trimmed == []
    assert [(p.label(), p.work_mode) for p in after] == [
        ("02:10–05:00", "ForceCharge"),
        ("05:01–16:01", "SelfUse"),
    ]


def test_merge_drops_force_discharge_outside_the_charge_too():
    # A forced period left partly outside the charge would force the battery at a time the
    # plan didn't choose, e.g. a grid charge at the day rate, so all of it goes.
    mine = Period.from_fox(group((1, 0), (3, 59), "ForceDischarge", fdSoc=20))
    added = charge_periods(utc(2026, 1, 10, 2, 0), utc(2026, 1, 10, 5, 0), "UTC", 80, 3000, 10)
    after, removed, trimmed = merge([mine], added)
    assert (removed, trimmed) == ([mine], [])
    assert [p.label() for p in after] == ["02:00–05:00"]


ALL_DAY = group((0, 0), (23, 59), "SelfUse", fdSoc=10, fdPwr=0, minSocOnGrid=5, pvLimit=20000)


def test_fill_gaps_copies_the_all_day_period_into_every_uncovered_time():
    # D13: Fox drops its all-day default on any write (owner's inverter, 5 November 2026), so
    # the times only it covered are written as periods with its settings.
    periods = [Period.from_fox(group((0, 0), (1, 59))), Period.from_fox(group((19, 1), (23, 59)))]
    after, filled = fill_gaps(periods, Period.from_fox(ALL_DAY))
    assert [p.label() for p in filled] == ["02:00–19:01"]
    assert [p.label() for p in after] == ["00:00–02:00", "02:00–19:01", "19:01–00:00"]
    sent = filled[0].to_fox()
    assert sent["workMode"] == "SelfUse"
    assert sent["extraParam"] == {"fdSoc": 10, "fdPwr": 0, "minSocOnGrid": 5, "pvLimit": 20000}


def test_fill_gaps_leaves_gaps_when_no_all_day_period_was_seen():
    # Gaps then run in the inverter's main work mode, which a write doesn't change.
    periods = [Period.from_fox(group((16, 0), (18, 59), "Feedin"))]
    assert fill_gaps(periods, None) == (periods, [])
    assert fill_gaps([], Period.from_fox(ALL_DAY)) == ([], [])


def owners_schedule(with_default=True):
    """The owner's schedule from step 1 on 5 November 2026."""
    groups = [
        group((0, 0), (1, 59)),
        group((2, 45), (5, 0), "ForceCharge", fdSoc=67, fdPwr=12000, minSocOnGrid=5),
        group((5, 1), (16, 0)),
        group((19, 1), (23, 59)),
    ]
    return [ALL_DAY, *groups] if with_default else groups


def test_propose_writes_the_whole_day_in_place_of_the_owners_schedule():
    # Whole-day control (owner's choice, 9 November 2026): the planner's stages and self-use
    # at every other minute, at the planner's levels, in place of whatever was there.
    schedule = Schedule.from_fox(FakeInverter(owners_schedule()).scheduler("x"))
    plan = plan_for(utc(2026, 11, 6, 2, 10), utc(2026, 11, 6, 5, 0))
    proposal = service.propose(system_with(), InverterControl(), plan, schedule)
    assert [(p.label(), p.work_mode) for p in proposal.after] == [
        ("00:00–02:10", "SelfUse"),
        ("02:10–05:00", "ForceCharge"),
        ("05:00–00:00", "SelfUse"),
    ]
    # Every one of the owner's periods goes, the all-day default too.
    assert len(proposal.removed) == len(owners_schedule())
    assert proposal.filled == [p for p in proposal.after if p.work_mode == "SelfUse"]
    # Down to the planner's 10%, not the owner's old 5%, keeping the installer's solar limit.
    for p in proposal.filled:
        assert (p.min_soc_on_grid, p.fd_soc, p.fd_pwr) == (10, 10, 0)
        assert p.to_fox()["extraParam"]["pvLimit"] == 20000
    assert proposal.saves_manual
    assert any("whole day is written" in n for n in proposal.notes)
    assert any("saved first as your latest manual schedule" in n for n in proposal.notes)


def test_self_use_carries_the_installers_limits_from_the_saved_schedule():
    # Nothing on the inverter but a forced period: the limits come from the manual schedule.
    only_forced = [group((2, 45), (5, 0), "ForceCharge", fdSoc=67, fdPwr=12000)]
    schedule = Schedule.from_fox(FakeInverter(only_forced).scheduler("x"))
    saved = Schedule.from_fox(FakeInverter(owners_schedule()).scheduler("x")).to_snapshot()
    control = InverterControl(original_schedule=saved)
    plan = plan_for(utc(2026, 11, 6, 2, 10), utc(2026, 11, 6, 5, 0))
    proposal = service.propose(system_with(), control, plan, schedule)
    assert all(p.to_fox()["extraParam"]["pvLimit"] == 20000 for p in proposal.filled)


def test_a_change_by_hand_is_noticed_against_what_the_planner_wrote():
    schedule = Schedule.from_fox(FakeInverter(owners_schedule()).scheduler("x"))
    # Nothing written yet: only the very first schedule is saved, as before.
    assert service.changed_by_hand(schedule, InverterControl())
    assert not service.changed_by_hand(
        schedule, InverterControl(original_schedule=schedule.to_snapshot())
    )
    # Once the planner has written, any difference from that is the owner's.
    written = InverterControl(
        original_schedule={"groups": []}, written_schedule=schedule.to_snapshot()
    )
    assert not service.changed_by_hand(schedule, written)
    edited = Schedule.from_fox(
        FakeInverter([*owners_schedule(), group((16, 1), (19, 0))]).scheduler("x")
    )
    assert service.changed_by_hand(edited, written)


def test_matches_reports_differences():
    written = charge_periods(utc(2026, 1, 10, 1, 0), utc(2026, 1, 10, 5, 0), "UTC", 80, 3000, 10)
    assert matches(written, written) == []
    changed = Period.from_fox(written[0].to_fox() | {"extraParam": {"fdSoc": 100}})
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
    assert any("use 10%, the lowest it writes" in n for n in proposal.notes)
    assert any("repeat every day" in n for n in proposal.notes)
    assert [p.label() for p in proposal.after] == ["00:00–02:00", "02:00–05:30", "05:30–00:00"]


def test_propose_keeps_the_grid_charge_under_the_owners_maximum():
    system = system_with()
    system.battery.max_grid_charge_percent = 95
    schedule = Schedule.from_fox(FakeInverter().scheduler("x"))
    proposal = service.propose(
        system,
        InverterControl(),
        plan_for(utc(2026, 1, 11, 2, 0), utc(2026, 1, 11, 5, 0), target=99),
        schedule,
    )
    assert proposal.added[0].fd_soc == 95 and proposal.target_soc_percent == 95
    assert any("your grid charge maximum" in n for n in proposal.notes)


def test_propose_replaces_last_nights_charge_even_when_it_doesnt_overlap():
    last_night = group((1, 0), (1, 59), "ForceCharge", fdSoc=90, fdPwr=3600, minSocOnGrid=10)
    schedule = Schedule.from_fox(FakeInverter([last_night]).scheduler("x"))
    control = InverterControl(written_groups=[last_night])
    proposal = service.propose(
        system_with(), control, plan_for(utc(2026, 1, 11, 4, 0), utc(2026, 1, 11, 5, 0)), schedule
    )
    assert [p.label() for p in proposal.after] == ["00:00–04:00", "04:00–05:00", "05:00–00:00"]
    assert [p.label() for p in proposal.removed] == ["01:00–02:00"]


def test_no_charge_writes_a_day_of_self_use():
    schedule = Schedule.from_fox(FakeInverter().scheduler("x"))
    proposal = service.propose(
        system_with(), InverterControl(), plan_for(None, None, charge_kwh=0), schedule
    )
    assert proposal.added == []
    # Never as one 00:00-23:59 period, which Fox treats as its default.
    assert [(p.label(), p.work_mode) for p in proposal.after] == [
        ("00:00–12:00", "SelfUse"),
        ("12:00–00:00", "SelfUse"),
    ]
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
    labels = [(p.label(), p.work_mode, p.fd_soc) for p in proposal.after]
    assert labels == [
        ("00:00–03:40", "ForceCharge", 10),
        ("03:40–05:30", "ForceCharge", 80),
        ("05:30–23:30", "SelfUse", 10),
        ("23:30–00:00", "ForceCharge", 10),
    ]
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
    assert [(p.label(), p.work_mode, p.fd_soc) for p in proposal.after] == [
        ("00:00–00:30", "SelfUse", 10),
        ("00:30–05:30", "ForceCharge", 10),
        ("05:30–00:00", "SelfUse", 10),
    ]
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
        ("04:00–23:00", "SelfUse", 10, 0),
        ("23:00–00:00", "ForceDischarge", 10, 3600),
    ]
    assert proposal.discharge_soc_percent == 10
    assert any("stops at 10% rather than 5%" in n for n in proposal.notes)


def test_a_full_schedule_is_replaced_not_refused():
    # The owner's periods no longer count towards the limit: they're replaced.
    groups = [group((h, 0), (h, 29)) for h in range(1, 9)]
    schedule = Schedule.from_fox(FakeInverter(groups).scheduler("x"))
    proposal = service.propose(
        system_with(),
        InverterControl(),
        plan_for(utc(2026, 1, 11, 12, 0), utc(2026, 1, 11, 13, 0)),
        schedule,
    )
    assert len(proposal.after) == 3 and len(proposal.removed) == 8


# --- API -------------------------------------------------------------------------------------


@pytest.fixture
def writes_on(monkeypatch):
    monkeypatch.setattr(settings, "inverter_writes", True)


@pytest.fixture
def inverter(writes_on):
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
    assert test_group["extraParam"]["fdPwr"] == testing.TEST_POWER_W
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
    # The calls shown for authorising name the same write, and read before and after.
    calls = preview["calls"]
    assert [c["writes"] for c in calls][0] is False and calls[-1]["writes"] is False
    assert next(c for c in calls if c["writes"])["body"]["groups"] == inverter.groups
    assert done.json()["sent"]["groups"] == preview["request"]["groups"]

    resp = client.put("/api/control", json={"enabled": True, "auto_apply": True}, headers=headers)
    assert resp.status_code == 200 and resp.json()["auto_apply"] is True

    # Remove takes the planner's forced periods out; their times run in self-use, so the day
    # stays covered.
    cleared = client.post("/api/control/clear", headers=headers).json()
    assert cleared["status"] == "ok", cleared["message"]
    assert all(g["workMode"] == "SelfUse" for g in inverter.groups)
    minutes = {
        m
        for p in (Period.from_fox(g) for g in inverter.groups)
        for m in range(p.start, p.end + 1)
    }
    assert minutes == set(range(24 * 60))

    # Restore puts back the owner's own schedule and stops the nightly apply.
    restored = client.post("/api/control/restore", headers=headers).json()
    assert restored["status"] == "ok", restored["message"]
    assert "nightly apply is turned off" in restored["message"]
    assert inverter.groups == original and inverter.enable == 1
    assert client.get("/api/control", headers=headers).json()["auto_apply"] is False

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
        real_set(
            sn,
            [
                g | {"extraParam": g["extraParam"] | {"fdPwr": 0}}
                if g["workMode"] == "ForceCharge"
                else g
                for g in groups
            ],
        )

    inverter.set_scheduler_groups = ignores_power
    result = step(client, headers, 3)
    assert result["status"] == "mismatch"
    assert "charge power" in result["message"]
    assert client.get("/api/control", headers=headers).json()["test_level"] == 2
    # The test period is still recorded, so it can be removed.
    assert client.post("/api/control/clear", headers=headers).json()["status"] == "ok"


def test_write_test_keeps_the_whole_day_covered_when_fox_drops_its_default(
    client, headers, inverter
):
    # D13, as on the owner's inverter: step 2 wrote the periods back, Fox dropped its all-day
    # default, and 02:00-02:45 and 16:01-19:01 were left with no period.
    inverter.groups = owners_schedule()
    opt_in(client, headers)
    step(client, headers, 1)
    result = step(client, headers, 2)
    assert result["status"] == "ok", result["message"]
    assert "02:00–02:45" in result["message"] and "16:01–19:01" in result["message"]
    periods = [Period.from_fox(g) for g in inverter.groups]
    assert not any(p.all_day for p in periods)
    assert {m for p in periods for m in range(p.start, p.end + 1)} == set(range(24 * 60))

    assert step(client, headers, 3)["status"] == "ok"
    removed = step(client, headers, 4)
    assert removed["status"] == "ok", removed["message"]
    # Every minute runs as it did in step 1, though Fox now lists the gaps as periods.
    by_minute = {m: p for p in (Period.from_fox(g) for g in inverter.groups) for m in range(p.start, p.end + 1)}
    default = Period.from_fox(ALL_DAY)
    for m in (2 * 60 + 30, 17 * 60):
        p = by_minute[m]
        assert (p.work_mode, p.min_soc_on_grid, p.fd_soc) == (
            default.work_mode,
            default.min_soc_on_grid,
            default.fd_soc,
        )


def test_read_back_waits_for_fox_to_catch_up(client, headers, inverter, monkeypatch):
    # D15: twice on 5 November 2026 the read straight after a write showed Fox's old copy,
    # and a read a minute later showed the write exactly as sent. A good write was logged as
    # a mismatch, and the write test didn't move on.
    waits = []
    monkeypatch.setattr(service, "_wait", waits.append)
    inverter.groups = owners_schedule()
    opt_in(client, headers)
    step(client, headers, 1)
    real_scheduler, stale = inverter.scheduler, {"reads": 0}

    def lagging(sn):
        if stale["reads"]:
            stale["reads"] -= 1
            return stale["old"]
        return real_scheduler(sn)

    real_set = inverter.set_scheduler_groups

    def remember_old(sn, groups):
        stale["old"], stale["reads"] = real_scheduler(sn), 2
        real_set(sn, groups)

    inverter.scheduler, inverter.set_scheduler_groups = lagging, remember_old
    result = step(client, headers, 2)
    assert result["status"] == "ok", result["message"]
    assert len(waits) == 2  # two stale reads, each followed by a wait


def test_read_back_still_reports_a_real_mismatch_after_waiting(client, headers, inverter, monkeypatch):
    waits = []
    monkeypatch.setattr(service, "_wait", waits.append)
    opt_in(client, headers)
    step(client, headers, 1)
    inverter.set_scheduler_groups = lambda sn, groups: None  # Fox accepts and ignores it
    result = step(client, headers, 2)
    assert result["status"] in ("ok", "mismatch")  # step 2 writes back what's there
    result = step(client, headers, 3)
    assert result["status"] == "mismatch"
    assert len(waits) == len(service.READ_BACK_WAITS)


def test_dry_run_reads_once_writes_nothing_and_counts_each_night(
    client, headers, inverter, monkeypatch
):
    # Trust ladder stage 3, shortened to three nights by the owner on 5 November 2026: run on
    # the Automation page, with writes off, between the write test and applying.
    monkeypatch.setattr(settings, "inverter_writes", False)
    opt_in(client, headers)
    resp = client.post("/api/control/dry-run", headers=headers)
    assert resp.status_code == 200, resp.text
    run = resp.json()
    assert run["action"]["kind"] == "dry_run" and run["action"]["status"] == "ok"
    assert run["checks"] and all(c["status"] in ("pass", "warn") for c in run["checks"])
    assert run["requests"][0]["path"] == "/op/v3/device/scheduler/enable"  # would send
    assert inverter.writes == []
    client.post("/api/control/dry-run", headers=headers)  # again the same evening
    status = client.get("/api/control", headers=headers).json()
    assert status["dry_run_nights_needed"] == 3
    assert [n["day"] for n in status["dry_run_nights"]] == [run["action"]["day"]]
    assert status["dry_run_nights"][0]["passed"] is True
    # The night hasn't happened yet, so there's no verdict on it.
    assert status["dry_run_nights"][0]["verdict"] is None


def test_dry_run_needs_control_turned_on(client, headers, inverter):
    assert client.post("/api/control/dry-run", headers=headers).status_code == 403


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
        # Not until the plan locks, by default ten minutes before the window opens.
        early = cycle.night_start - timedelta(hours=1)
        assert run_once(db, inverter, weather, early) == []
        locks = cycle.night_start - timedelta(minutes=10)
        assert run_once(db, inverter, weather, locks - timedelta(minutes=1)) == []
        # The owner changes the schedule by hand during the day: the nightly apply saves that
        # as the latest manual schedule, then writes the whole day again.
        inverter.groups = [{**inverter.groups[0], "workMode": "Feedin"}]
        (action,) = run_once(db, inverter, weather, locks)
        assert action.trigger == ControlTrigger.auto
        assert action.status == ControlStatus.ok, action.message
        db.refresh(control)
        assert control.original_schedule["groups"][0]["workMode"] == "Feedin"
        assert run_once(db, inverter, weather, locks + timedelta(minutes=5)) == []
        # What was sent is the plan as it locked, which is what Tonight now shows.
        kept = db.query(LockedPlan).one()
        assert kept.day == cycle.night_end.date()
        target = kept.plan["recommendations"][0]["target_soc_percent"]
        assert action.target_soc_percent == pytest.approx(target, abs=1)


def unlock_apply(client, headers):
    opt_in(client, headers)
    for n in (1, 2, 3, 4):
        step(client, headers, n)


def test_send_at_the_last_minute_needs_the_server_to_send_it(client, headers, inverter):
    unlock_apply(client, headers)
    resp = client.post("/api/control/last-minute", headers=headers)
    assert resp.status_code == 409 and "SBP_AUTO_APPLY" in resp.json()["detail"]
    assert client.get("/api/control", headers=headers).json()["can_send_last_minute"] is False


def test_send_at_the_last_minute_sends_once_when_the_plan_locks(
    client, headers, inverter, weather, monkeypatch  # noqa: F811
):
    from app.database import get_session_factory
    from app.planner.simulator import cycle_for
    from app.tariffs import TariffSchedule

    monkeypatch.setattr(settings, "auto_apply", True)
    opt_in(client, headers)
    # Locked until the write test reaches step 4.
    assert client.post("/api/control/last-minute", headers=headers).status_code == 409
    for n in (1, 2, 3, 4):
        step(client, headers, n)
    status = client.post("/api/control/last-minute", headers=headers).json()
    assert status["can_send_last_minute"] is True
    day = status["last_minute_day"]
    assert "at the last minute" in status["actions"][0]["message"]
    writes = len(inverter.writes)

    factory = app.dependency_overrides[get_session_factory]()
    with factory() as db:
        control = db.query(InverterControl).one()
        assert control.auto_apply is False  # not full automation
        system = control.system
        cycle = cycle_for(date.fromisoformat(day), TariffSchedule.from_system(system), "UTC")
        locks = cycle.night_start - timedelta(minutes=10)
        assert run_once(db, inverter, weather, locks - timedelta(minutes=30)) == []
        (action,) = run_once(db, inverter, weather, locks + timedelta(minutes=1))
        assert action.status == ControlStatus.ok, action.message
        assert len(inverter.writes) > writes
        assert control.last_minute_day is None
        # Once only: the next night isn't sent unless asked again.
        assert run_once(db, inverter, weather, locks + timedelta(days=1)) == []


def test_cancel_send_at_the_last_minute(client, headers, inverter, monkeypatch):
    monkeypatch.setattr(settings, "auto_apply", True)
    unlock_apply(client, headers)
    assert client.post("/api/control/last-minute", headers=headers).json()["last_minute_day"]
    status = client.delete("/api/control/last-minute", headers=headers).json()
    assert status["last_minute_day"] is None
    assert status["actions"][0]["message"].startswith("Cancelled")


def test_apply_with_nothing_to_change_writes_nothing(monkeypatch):
    monkeypatch.setattr(
        service, "_log", lambda db, system, kind, trigger, status, message, **kw: status
    )
    system = system_with()
    system.fox_ess = SimpleNamespace(device_sn="INV1")
    control = InverterControl()
    plan = plan_for(None, None, charge_kwh=0)
    # The inverter already holds exactly the whole day the plan would write.
    empty = Schedule.from_fox(FakeInverter().scheduler("x"))
    tonight = service.propose(system, control, plan, empty)
    inverter = FakeInverter([p.to_fox() for p in tonight.after])
    proposal = service.propose(system, control, plan, Schedule.from_fox(inverter.scheduler("x")))
    assert not proposal.changes
    status = service.apply(None, system, control, inverter, proposal, ControlTrigger.manual)
    assert status == ControlStatus.skipped
    assert inverter.writes == []


def test_writes_off_refuses_every_write_but_still_reads(client, headers, inverter, monkeypatch):
    opt_in(client, headers)
    monkeypatch.setattr(settings, "inverter_writes", False)
    assert client.get("/api/control", headers=headers).json()["writes_allowed"] is False
    step(client, headers, 1)  # only reads
    for path, body in (
        ("/api/control/test", {"step": 2}),
        ("/api/control/apply", {"fingerprint": "x"}),
        ("/api/control/clear", None),
        ("/api/control/restore", None),
    ):
        resp = client.post(path, json=body, headers=headers)
        assert resp.status_code == 409, path
        assert "switched off on this server" in resp.json()["detail"]
    assert inverter.writes == []


def test_nightly_does_nothing_while_writes_are_off():
    # Returns before touching the database, the plan or the inverter.
    assert settings.inverter_writes is False
    assert run_once(None, FakeInverter(), None) == []


def send_by_hand(client, headers):
    preview = client.get("/api/control/preview", headers=headers).json()
    done = client.post(
        "/api/control/apply", json={"fingerprint": preview["fingerprint"]}, headers=headers
    ).json()
    assert done["status"] == "ok", done["message"]
    return preview


def test_sending_by_hand_fixes_tonights_plan_and_is_shown_until_the_window_ends(
    client, headers, inverter
):
    unlock_apply(client, headers)
    tonight = client.get("/api/control", headers=headers).json()["tonight"]
    assert tonight["sent"] is None and tonight["fixed_by"] is None
    assert tonight["will_send"] is None

    preview = send_by_hand(client, headers)
    tonight = client.get("/api/control", headers=headers).json()["tonight"]
    # What was sent, when and how, stays on the page.
    assert tonight["sent"]["trigger"] == "manual" and tonight["sent"]["status"] == "ok"
    assert [p["start"] for p in tonight["sent"]["periods"]] == [
        p["start"] for p in preview["after"]
    ]
    # The plan stops moving: Tonight and Home show what the inverter has.
    assert tonight["fixed_by"] == "sent" and tonight["locked_at"]
    lock = client.get("/api/plan/lock", headers=headers).json()
    assert lock["fixed_by"] == "sent"

    # Changing when plans lock doesn't let go of a plan that was sent.
    client.put("/api/plan/lock", json={"mode": "set_time", "time": "23:59"}, headers=headers)
    assert client.get("/api/plan/lock", headers=headers).json()["fixed_by"] == "sent"


def test_a_sent_plan_can_follow_the_forecast_again(client, headers, inverter):
    unlock_apply(client, headers)
    send_by_hand(client, headers)
    writes = len(inverter.writes)
    status = client.delete("/api/control/fixed", headers=headers).json()
    assert status["tonight"]["fixed_by"] is None
    # The inverter keeps what was sent, and the page still says so.
    assert status["tonight"]["sent"]["status"] == "ok"
    assert len(inverter.writes) == writes
    assert "follow the forecast again" in status["actions"][0]["message"]
    # Only a plan fixed by a send can be let go.
    assert client.delete("/api/control/fixed", headers=headers).status_code == 409


def test_restore_after_sending_means_tonight_isnt_sent(client, headers, inverter):
    unlock_apply(client, headers)
    send_by_hand(client, headers)
    client.post("/api/control/restore", headers=headers)
    assert client.get("/api/control", headers=headers).json()["tonight"]["sent"] is None


def test_tonight_says_when_it_will_be_sent(client, headers, inverter, monkeypatch):
    monkeypatch.setattr(settings, "auto_apply", True)
    unlock_apply(client, headers)
    status = client.post("/api/control/last-minute", headers=headers).json()
    assert status["tonight"]["will_send"] == "last_minute"
    assert status["tonight"]["sent"] is None


def test_each_recent_night_is_coloured_by_how_it_went():
    from app.api.control import _rating

    def sent(status):
        return SimpleNamespace(status=status)

    def verdict(says="about right", peak=0.0):
        return SimpleNamespace(verdict=says, peak_import_kwh=peak)

    assert _rating(None, None) is None  # nothing sent from the app
    assert _rating(sent(ControlStatus.ok), None) is None  # waiting for the day's readings
    assert _rating(sent(ControlStatus.failed), None) == "poor"
    assert _rating(sent(ControlStatus.ok), verdict()) == "good"
    assert _rating(sent(ControlStatus.ok), verdict("too little")) == "fair"
    assert _rating(sent(ControlStatus.ok), verdict("more than needed")) == "fair"
    assert _rating(sent(ControlStatus.mismatch), verdict()) == "fair"
    # Running short at the peak rate is what the owner most wants to avoid.
    assert _rating(sent(ControlStatus.ok), verdict("too little", peak=0.8)) == "poor"


def test_the_last_seven_nights_are_listed_with_tonights_send(client, headers, inverter):
    unlock_apply(client, headers)
    send_by_hand(client, headers)
    status = client.get("/api/control", headers=headers).json()
    nights = status["recent_nights"]
    assert status["recent_nights_needed"] == 7 and len(nights) == 7
    assert nights[0]["day"] == status["tonight"]["day"]
    assert nights[0]["sent"]["status"] == "ok"
    assert nights[0]["rating"] is None  # not metered yet
    assert all(n["sent"] is None for n in nights[1:])
