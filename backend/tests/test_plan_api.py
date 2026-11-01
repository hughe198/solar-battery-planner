from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from app.models import Battery
from app.planner.simulator import BatterySpec, simulate
from tests.conftest import register
from tests.test_forecast_api import tomorrow, weather  # noqa: F401 (fixture)
from tests.test_imports_api import UTC_SYSTEM

HEADER = "start,load_kwh,solar_kwh,grid_import_kwh,grid_export_kwh,battery_soc_percent\n"
FIRST_DAY = date(2024, 6, 1)
DAYS = 10


@pytest.fixture
def headers(client, weather):  # noqa: F811
    h = register(client)
    assert client.put("/api/system", json=UTC_SYSTEM, headers=h).status_code == 200
    return h


def load(slot: int) -> float:
    return 0.5 if 34 <= slot < 42 else 0.15


def solar(slot: int) -> float:
    return 0.8 if 16 <= slot < 34 else 0.0


def metered_days(charge_to_percent: float) -> str:
    """Days metered as if the battery behaved like the simulator, charged to a fixed level
    every night in the 23:30 to 05:30 window."""
    battery = BatterySpec.of(
        Battery(**UTC_SYSTEM["battery"], min_soc_percent=10, round_trip_efficiency_percent=90)
    )
    level = battery.reserve_kwh
    rows = []
    start = datetime.combine(FIRST_DAY, datetime.min.time()) - timedelta(minutes=30)

    def row(moment, ld, pv, imp, exp, stored):
        soc = battery.percent(stored)
        rows.append(f"{moment:%Y-%m-%dT%H:%M},{ld},{pv},{imp:.4f},{exp:.4f},{soc:.2f}\n")

    for d in range(DAYS):
        target = max(level, battery.capacity_kwh * charge_to_percent / 100)
        night = [start + timedelta(minutes=30 * k) for k in range(12)]
        step = (target - level) / 12
        for k, moment in enumerate(night):
            slot = (k - 1) % 48
            draw = battery.grid_kwh_to_store(step)
            row(moment, load(slot), 0, load(slot) + draw, 0, level + step * (k + 1))
        day_slots = list(range(11, 47))
        sim = simulate(
            battery,
            level,
            np.array([target]),
            np.zeros(0),
            np.zeros(0),
            np.array([load(s) for s in day_slots], dtype=float),
            np.array([solar(s) for s in day_slots], dtype=float),
        )
        for t, slot in enumerate(day_slots):
            moment = start + timedelta(minutes=30 * (12 + t))
            row(
                moment,
                load(slot),
                solar(slot),
                sim.day_import_kwh[0, t],
                sim.day_export_kwh[0, t],
                sim.stored_kwh[0, t + 1],
            )
        level = float(sim.end_kwh[0])
        start += timedelta(days=1)
    return HEADER + "".join(rows)


def upload(client, headers, csv: str):
    resp = client.post("/api/import/csv", files={"file": ("u.csv", csv)}, headers=headers)
    assert resp.status_code == 200, resp.text


def test_plan_recommends_a_charge_for_each_mode(client, headers):
    resp = client.get("/api/plan?start_soc_percent=20", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["day"] in (tomorrow().isoformat(), (tomorrow() - timedelta(days=1)).isoformat())
    assert body["mode"] == "maximise_own_usage"
    assert body["start_soc_percent"] == 20
    assert [r["mode"] for r in body["recommendations"]] == [
        "maximise_own_usage",
        "maximise_export_revenue",
    ]
    own = body["recommendations"][0]
    assert 20 <= own["target_soc_percent"] <= body["max_soc_percent"]
    assert own["reasons"]
    assert own["expected"]["load_kwh"] > 0
    assert body["candidates"][0]["target_soc_percent"] == 20
    assert body["timeline"][0]["cheap"] is True
    assert any(not p["cheap"] for p in body["timeline"])


def timeline_for(client, headers, bands=None):
    if bands:
        system = {**UTC_SYSTEM, "tariff": {"name": "Test", "bands": bands}}
        assert client.put("/api/system", json=system, headers=headers).status_code == 200
    day = tomorrow()
    resp = client.get(f"/api/plan?day={day}", headers=headers)
    assert resp.status_code == 200, resp.text
    points = resp.json()["timeline"]
    starts = [datetime.fromisoformat(p["start"]).replace(tzinfo=None) for p in points]
    assert all(b - a == timedelta(minutes=30) for a, b in zip(starts, starts[1:]))
    midnight = datetime.combine(day, datetime.min.time())
    return points, starts, midnight


def test_plan_includes_the_window_hours_before_midnight(client, headers):
    points, starts, midnight = timeline_for(client, headers)  # Window 23:30 to 05:30.
    assert starts[0] == midnight - timedelta(minutes=30)
    assert starts[-1] == midnight + timedelta(hours=23)
    assert sum(p["cheap"] for p in points) == 12


def test_plan_runs_past_midnight_to_the_next_window(client, headers):
    points, starts, midnight = timeline_for(
        client,
        headers,
        [
            {"direction": "import", "start": "02:00", "end": "05:00", "rate": 7.5},
            {"direction": "import", "start": "05:00", "end": "02:00", "rate": 27.5},
            {"direction": "export", "start": "00:00", "end": "00:00", "rate": 15},
        ],
    )
    assert starts[0] == midnight + timedelta(hours=2)
    assert starts[-1] == midnight + timedelta(hours=25, minutes=30)
    assert points[-1]["load_kwh"] > 0


def battery_readings(day: date, first_hour: int, hours: int, soc: float) -> str:
    rows = []
    for k in range(hours * 2):
        moment = datetime.combine(day, datetime.min.time()) + timedelta(
            hours=first_hour, minutes=30 * k
        )
        rows.append(f"{moment:%Y-%m-%dT%H:%M},0.2,0,0.2,0,{soc}\n")
    return HEADER + "".join(rows)


def test_plan_predicts_the_start_from_the_latest_reading(client, headers):
    today = datetime.now(timezone.utc).date()
    day = today + timedelta(days=1)
    upload(client, headers, battery_readings(today, 6, 6, 60))  # Last reading ends at 12:00.

    body = client.get(f"/api/plan?day={day}", headers=headers).json()

    assert body["start_soc_source"] == "measured"
    assert body["reading_soc_percent"] == 60
    assert body["reading_at"].startswith(f"{today}T12:00")
    lead = body["lead_in"]
    assert lead[0]["start"].startswith(f"{today}T12:00")
    assert lead[-1]["start"].startswith(f"{today}T23:00")
    assert not any(p["cheap"] for p in lead)
    assert body["start_soc_percent"] == lead[-1]["soc_percent"]
    assert lead[-1]["soc_percent"] != 60
    assert body["timeline"][0]["start"].startswith(f"{today}T23:30")


def test_plan_ignores_a_reading_from_before_the_last_charge_window_closed(client, headers):
    today = datetime.now(timezone.utc).date()
    day = today + timedelta(days=1)
    # Ends at 05:00 today, while the 23:30 to 05:30 window it would have to predict across is open.
    upload(client, headers, battery_readings(today, 3, 2, 60))

    body = client.get(f"/api/plan?day={day}", headers=headers).json()

    assert body["start_soc_source"] == "assumed"
    assert body["start_soc_percent"] == 10
    assert body["reading_at"] is None
    assert body["lead_in"] == []


def test_a_given_start_level_overrides_the_reading(client, headers):
    today = datetime.now(timezone.utc).date()
    day = today + timedelta(days=1)
    upload(client, headers, battery_readings(today, 6, 6, 60))

    body = client.get(f"/api/plan?day={day}&start_soc_percent=25", headers=headers).json()

    assert body["start_soc_source"] == "given"
    assert body["start_soc_percent"] == 25
    assert body["lead_in"] == []


def test_plan_rejects_far_days(client, headers):
    far = tomorrow() + timedelta(days=9)
    assert client.get(f"/api/plan?day={far}", headers=headers).status_code == 422


def test_replay_without_soc_data_is_empty(client, headers):
    resp = client.get("/api/plan/replay", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["days"] == []
    assert resp.json()["review"] is None


def test_replay_matches_metered_days_and_reviews_over_charging(client, headers):
    upload(client, headers, metered_days(charge_to_percent=100))
    body = client.get("/api/plan/replay", headers=headers).json()
    # The first day has no SoC from the evening before.
    assert len(body["days"]) == DAYS - 1
    assert body["import_error_percent"] < 1
    assert body["export_error_percent"] < 1
    assert body["soc_error_percent"] < 0.5

    latest = body["days"][-1]
    assert latest["day"] == (FIRST_DAY + timedelta(days=DAYS - 1)).isoformat()
    assert latest["soc_after_charge_percent"] == pytest.approx(100, abs=0.1)
    # The sun covers the day from 08:00; a full battery forces solar out to the grid.
    assert latest["best_soc_percent"] < 60
    assert latest["missed_saving_pence"] > 10
    assert latest["wasted_export_kwh"] > 1
    comments = " ".join(body["review"]["comments"])
    assert "used 10.0 kWh" in comments
    assert "averaged" in comments
    assert "more than the day needed" in comments
    assert "would have saved" in comments
    assert body["missed_saving_pence"] > 50


def test_replay_of_one_day_has_its_half_hours_and_the_best_charge(client, headers):
    upload(client, headers, metered_days(charge_to_percent=100))
    day = FIRST_DAY + timedelta(days=4)

    resp = client.get(f"/api/plan/replay/{day}", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["day"]["day"] == day.isoformat()
    assert body["comments"][0].startswith(f"On {day:%A} {day.day} {day:%B}")
    assert body["day"]["charge_start"].startswith(f"{day - timedelta(days=1)}T23:30")
    # The chart opens at 21:00 the evening before, for context, outside the 48 half hours scored.
    lead_up = [p for p in body["points"] if p["lead_up"]]
    assert lead_up[0]["start"].startswith(f"{day - timedelta(days=1)}T21:00")
    assert len(lead_up) == 5
    assert not any(p["cheap"] or p["best_soc_percent"] is not None for p in lead_up)
    points = [p for p in body["points"] if not p["lead_up"]]
    assert len(points) == 48
    assert points[0]["start"].startswith(f"{day - timedelta(days=1)}T23:30")
    assert points[0]["start"].endswith(("Z", "+00:00"))
    assert sum(p["cheap"] for p in points) == 12
    # Metered levels throughout; the best charge's only once the window has closed.
    assert all(p["soc_percent"] is not None for p in points)
    assert all((p["best_soc_percent"] is None) == p["cheap"] for p in points)
    first_after = next(p for p in points if not p["cheap"])
    assert first_after["best_soc_percent"] < first_after["soc_percent"]


def next_window(soc: list[float], load: float = 0.1, imports: float = 0.1) -> str:
    """Half hours of the window after the metered days, which opens at 23:30 on the last day."""
    start = datetime.combine(FIRST_DAY + timedelta(days=DAYS - 1), datetime.min.time()) + timedelta(
        hours=23, minutes=30
    )
    return "".join(
        f"{start + timedelta(minutes=30 * k):%Y-%m-%dT%H:%M},{load},0,{imports},0,{level}\n"
        for k, level in enumerate(soc)
    )


def test_replay_has_no_cycle_in_progress_when_the_data_ends_with_one(client, headers):
    upload(client, headers, metered_days(charge_to_percent=100))
    assert client.get("/api/plan/replay", headers=headers).json()["in_progress"] is None


def test_replay_reports_a_held_cycle_before_it_can_be_scored(client, headers):
    upload(client, headers, metered_days(charge_to_percent=40))
    last = client.get("/api/plan/replay", headers=headers).json()["days"][-1]
    assert last["day"] == (FIRST_DAY + timedelta(days=DAYS - 1)).isoformat()

    rows = client.get(f"/api/plan/replay/{last['day']}", headers=headers).json()["points"]
    level = rows[-1]["soc_percent"]
    upload(client, headers, HEADER + next_window([level] * 12))

    body = client.get("/api/plan/replay", headers=headers).json()
    progress = body["in_progress"]
    next_day = FIRST_DAY + timedelta(days=DAYS)
    assert progress["day"] == next_day.isoformat()
    assert progress["outcome"] == "held"
    assert progress["grid_only"] is True
    assert progress["window_finished"] is True
    assert progress["soc_after_percent"] == pytest.approx(level, abs=0.1)
    assert progress["charge_start"].startswith(f"{next_day - timedelta(days=1)}T23:30")
    assert progress["charge_end"].startswith(f"{next_day}T05:30")
    assert progress["scored_after"].startswith(f"{next_day}T23:30")
    assert sum(p["lead_up"] for p in progress["points"]) == 5
    assert sum(p["cheap"] for p in progress["points"]) == 12
    # Still only the complete cycles are scored.
    assert body["days"][-1]["day"] == last["day"]


@pytest.mark.parametrize(
    "rise, outcome, grid_only, finished, count",
    [
        (30, "charged", True, True, 12),
        (-20, "discharged", False, True, 12),
        (0, "held", True, False, 4),
    ],
)
def test_replay_describes_what_an_in_progress_window_did(
    client, headers, rise, outcome, grid_only, finished, count
):
    upload(client, headers, metered_days(charge_to_percent=40))
    last = client.get("/api/plan/replay", headers=headers).json()["days"][-1]["day"]
    rows = client.get(f"/api/plan/replay/{last}", headers=headers).json()["points"]
    level = rows[-1]["soc_percent"]
    imports = 0.0 if outcome == "discharged" else 0.5 if outcome == "charged" else 0.1
    upload(client, headers, HEADER + next_window([level + rise] * count, imports=imports))

    progress = client.get("/api/plan/replay", headers=headers).json()["in_progress"]

    assert progress["outcome"] == outcome
    assert progress["grid_only"] is grid_only
    assert progress["window_finished"] is finished


def test_replay_of_a_day_without_data_is_not_found(client, headers):
    upload(client, headers, metered_days(charge_to_percent=100))
    assert client.get("/api/plan/replay/2020-01-01", headers=headers).status_code == 404
    assert client.get(f"/api/plan/replay/{FIRST_DAY}", headers=headers).status_code == 404


def test_replay_finds_a_good_decision_about_right(client, headers):
    upload(client, headers, metered_days(charge_to_percent=10))
    body = client.get("/api/plan/replay?mode=maximise_own_usage", headers=headers).json()
    latest = body["days"][-1]
    # Overnight use comes from the cheap grid, and solar refills the battery for the evening.
    assert latest["missed_saving_pence"] < 30
    assert body["review"]["comments"]


def test_replay_export_mode(client, headers):
    upload(client, headers, metered_days(charge_to_percent=100))
    body = client.get("/api/plan/replay?mode=maximise_export_revenue", headers=headers).json()
    assert body["mode"] == "maximise_export_revenue"
    # Export at 15p beats charging at 7.5p, so filling up was right for revenue.
    assert body["days"][-1]["best_soc_percent"] > 90


def test_history_summarises_the_last_week(client, headers):
    assert client.get("/api/history", headers=headers).json() == {"points": [], "days": []}
    upload(client, headers, metered_days(charge_to_percent=100))
    body = client.get("/api/history", headers=headers).json()
    assert len(body["days"]) == 7
    assert len(body["points"]) == 7 * 48 - 1  # the last day's 23:30 is the next night's
    last = body["days"][-1]
    assert last["day"] == (FIRST_DAY + timedelta(days=DAYS - 1)).isoformat()
    assert 0 <= last["self_sufficiency_percent"] <= 100
    assert last["max_soc_percent"] == pytest.approx(100, abs=0.1)
    cheap = [p for p in body["points"] if p["cheap"]]
    assert cheap and all(p["start"][11:16] >= "23:30" or p["start"][11:16] < "05:30" for p in cheap)
    # Overnight charging is bought at the cheap rate.
    assert last["import_cost_pence"] < last["import_kwh"] * 27.5


def test_plan_and_replay_on_a_single_rate_tariff(client, headers):
    flat = {
        **UTC_SYSTEM,
        "tariff_effective_from": "2020-01-01",  # Before the metered days below.
        "tariff": {
            "name": "Flat",
            "bands": [
                {"direction": "import", "start": "00:00", "end": "00:00", "rate": 25},
                {"direction": "export", "start": "00:00", "end": "00:00", "rate": 15},
            ],
        },
    }
    assert client.put("/api/system", json=flat, headers=headers).status_code == 200
    plan = client.get("/api/plan?start_soc_percent=30", headers=headers).json()
    own = plan["recommendations"][0]
    assert own["target_soc_percent"] == 30
    assert own["charge_kwh"] == 0
    assert not any(p["cheap"] for p in plan["timeline"])
    upload(client, headers, metered_days(charge_to_percent=10))
    body = client.get("/api/plan/replay", headers=headers).json()
    assert body["days"]
    assert "no cheaper overnight rate" in " ".join(body["review"]["comments"])
