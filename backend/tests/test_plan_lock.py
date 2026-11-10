from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.api.plan import current_plan, next_day
from app.control.nightly import lock_due
from app.database import get_session_factory
from app.main import app
from app.models import LockedPlan, System
from app.planner.locking import BEFORE_WINDOW, dump_plan, load_plan, lock_at, set_time_before
from app.planner.simulator import Cycle, cycle_for
from app.tariffs import TariffSchedule
from tests.conftest import register
from tests.test_forecast_api import weather  # noqa: F401 (fixture)
from tests.test_imports_api import UTC_SYSTEM


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def cycle(start):
    return Cycle(start, start + timedelta(hours=6), start + timedelta(hours=24))


def test_locks_just_before_the_window_by_default():
    system = SimpleNamespace(plan_lock_time=None, timezone="Europe/London")
    assert lock_at(system, cycle(utc(2026, 11, 10, 23, 30))) == utc(2026, 11, 10, 23, 20)


def test_a_set_time_is_the_last_one_before_the_window():
    system = SimpleNamespace(plan_lock_time=time(21, 0), timezone="Europe/London")
    assert lock_at(system, cycle(utc(2026, 11, 10, 23, 30))) == utc(2026, 11, 10, 21, 0)
    # A window that opens after midnight: still the evening before.
    assert lock_at(system, cycle(utc(2026, 11, 11, 0, 30))) == utc(2026, 11, 10, 21, 0)
    # In summer 21:00 in London is 20:00 UTC.
    assert lock_at(system, cycle(utc(2026, 7, 10, 22, 30))) == utc(2026, 7, 10, 20, 0)


def test_a_set_time_too_close_to_the_window_falls_back_to_just_before_it():
    # 23:25 is after 23:20, so it would be the day before: too early, so just before instead.
    latest = utc(2026, 11, 10, 23, 20)
    assert set_time_before(time(23, 25), latest, "Europe/London") == latest
    assert set_time_before(time(23, 20), latest, "Europe/London") == latest
    assert set_time_before(time(6, 0), latest, "Europe/London") == utc(2026, 11, 10, 6, 0)


@pytest.fixture
def headers(client, weather):  # noqa: F811
    h = register(client)
    assert client.put("/api/system", json=UTC_SYSTEM, headers=h).status_code == 200
    return h


@pytest.fixture
def db(client):
    factory = app.dependency_overrides[get_session_factory]()
    with factory() as session:
        yield session


def times(system):
    day = next_day(system)
    c = cycle_for(day, TariffSchedule.from_system(system), system.timezone)
    return day, c, c.night_start - BEFORE_WINDOW


def test_plan_follows_the_forecast_until_it_locks_then_stays(headers, db, weather):  # noqa: F811
    system = db.query(System).one()
    day, c, locks = times(system)

    plan, lock = current_plan(db, system, weather, day, locks - timedelta(hours=3))
    assert lock.locks_at == locks and lock.locked_at is None
    db.commit()
    assert db.query(LockedPlan).count() == 0

    plan, lock = current_plan(db, system, weather, day, locks + timedelta(minutes=1))
    db.commit()
    assert lock.locked_at == locks + timedelta(minutes=1)
    kept = db.query(LockedPlan).one()
    assert kept.day == day

    # Later asks get the kept plan as it was, without a new forecast (here, one that fails).
    weather.fail = True
    again, lock = current_plan(db, system, weather, day, locks + timedelta(hours=1))
    assert again == load_plan(kept.plan)
    assert lock.locked_at == locks + timedelta(minutes=1)


def test_no_lock_once_the_window_has_closed(headers, db, weather):  # noqa: F811
    system = db.query(System).one()
    day, c, _ = times(system)
    _, lock = current_plan(db, system, weather, day, c.night_end)
    assert lock.locked_at is None and db.query(LockedPlan).count() == 0


def test_kept_plan_round_trips(headers, db, weather):  # noqa: F811
    system = db.query(System).one()
    day, _, _ = times(system)
    plan, _ = current_plan(db, system, weather, day, datetime(2000, 1, 1, tzinfo=timezone.utc))
    assert load_plan(dump_plan(plan)) == plan


def test_lock_due_keeps_each_plan_on_time_once(headers, db, weather):  # noqa: F811
    system = db.query(System).one()
    _, _, locks = times(system)
    assert lock_due(db, weather, locks - timedelta(minutes=1)) == []
    assert [s.id for s in lock_due(db, weather, locks)] == [system.id]
    assert lock_due(db, weather, locks + timedelta(minutes=1)) == []
    assert db.query(LockedPlan).count() == 1


def test_plan_api_says_when_it_locks(client, headers):
    plan = client.get("/api/plan", headers=headers).json()
    lock = client.get("/api/plan/lock", headers=headers).json()
    assert lock["mode"] == "before_window" and lock["time"] is None
    assert plan["locks_at"] == lock["locks_at"]
    assert date.fromisoformat(lock["day"]) == date.fromisoformat(plan["day"])


def test_choose_a_set_lock_time(client, headers):
    resp = client.put("/api/plan/lock", json={"mode": "set_time", "time": "21:00"}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "set_time" and body["time"] == "21:00"
    assert "T21:00" in body["locks_at"]
    assert client.put("/api/plan/lock", json={"mode": "set_time"}, headers=headers).status_code == 422
    bad = client.put("/api/plan/lock", json={"mode": "set_time", "time": "25:00"}, headers=headers)
    assert bad.status_code == 422
    back = client.put("/api/plan/lock", json={"mode": "before_window"}, headers=headers).json()
    assert back["mode"] == "before_window" and back["time"] is None


def test_choosing_a_later_lock_time_lets_a_kept_plan_go(client, headers, db, weather):  # noqa: F811
    system = db.query(System).one()
    day, c, locks = times(system)
    db.add(LockedPlan(system_id=system.id, day=day, locked_at=locks, plan={}))
    db.commit()
    # Just before the window is still to come (the test's clock is real time, well before it)
    # unless the window is about to open; only check when it is.
    if datetime.now(timezone.utc) < locks:
        client.put("/api/plan/lock", json={"mode": "before_window"}, headers=headers)
        db.expire_all()
        assert db.query(LockedPlan).count() == 0
