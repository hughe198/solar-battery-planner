from datetime import date, datetime, timedelta

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
