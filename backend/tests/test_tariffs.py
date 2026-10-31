from datetime import date, datetime, time, timezone

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.migrations import upgrade
from app.models import RateDays, RateDirection, Tariff
from app.tariffs import PRESETS, Band, TariffSchedule, coverage_problems
from tests.conftest import register
from tests.test_system import SYSTEM


def band(direction, start, end, rate, days="all"):
    return Band(
        RateDirection(direction), RateDays(days), time.fromisoformat(start),
        time.fromisoformat(end), rate,
    )


# Four import and four export rates, with a different weekend import.
FOUR_RATE = [
    band("import", "02:00", "05:00", 15, "weekdays"),
    band("import", "05:00", "16:00", 25, "weekdays"),
    band("import", "16:00", "19:00", 35, "weekdays"),
    band("import", "19:00", "02:00", 22, "weekdays"),
    band("import", "00:00", "00:00", 20, "weekends"),
    band("export", "02:00", "05:00", 5),
    band("export", "05:00", "16:00", 15),
    band("export", "16:00", "19:00", 30),
    band("export", "19:00", "02:00", 12),
]


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_rate_at_uses_local_time_and_day_type():
    s = TariffSchedule.from_bands(FOUR_RATE, "Europe/London")
    # Wednesday 3 June 2026, BST: 01:30 UTC is 02:30 local.
    assert s.rate_at(utc(2026, 6, 3, 1, 30), "import") == 15
    assert s.rate_at(utc(2026, 6, 3, 0, 30), "import") == 22
    assert s.rate_at(utc(2026, 6, 3, 15, 0), "import") == 35
    assert s.rate_at(utc(2026, 6, 3, 15, 0), "export") == 30
    # Naive means UTC.
    assert s.rate_at(datetime(2026, 6, 3, 1, 30), "import") == 15
    # Saturday.
    assert s.rate_at(utc(2026, 6, 6, 1, 30), "import") == 20
    assert s.rate_at(utc(2026, 6, 6, 1, 30), RateDirection.export) == 5
    # Winter, GMT: 02:00 UTC is 02:00 local.
    assert s.rate_at(utc(2026, 12, 2, 2, 0), "import") == 15
    assert s.rate_at(utc(2026, 12, 2, 1, 30), "import") == 22


def test_day_rates_cover_a_local_day_including_clock_changes():
    s = TariffSchedule.from_bands(FOUR_RATE, "Europe/London")
    rates = s.day_rates(date(2026, 6, 3), "import")
    assert len(rates) == 48
    assert rates[0] == (utc(2026, 6, 2, 23, 0), 22)
    assert rates[4] == (utc(2026, 6, 3, 1, 0), 15)
    assert len(s.day_rates(date(2026, 3, 29), "import")) == 46
    assert len(s.day_rates(date(2026, 10, 25), "import")) == 50


def test_coverage_problems_name_gaps_and_overlaps():
    assert coverage_problems(FOUR_RATE) == []
    gappy = [b for b in FOUR_RATE if b.start != time(19) or b.direction != "import"] + [
        band("export", "18:00", "20:00", 1)
    ]
    problems = coverage_problems(gappy)
    assert "Import has no rate on weekdays for 19:00-02:00" in problems
    assert "Export has more than one rate for 18:00-20:00" in problems
    assert len(problems) == 2
    with pytest.raises(ValueError):
        TariffSchedule.from_bands(gappy)


@pytest.mark.parametrize("preset", PRESETS, ids=lambda p: p.id)
def test_presets_are_complete(preset):
    assert coverage_problems(preset.bands) == []


def as_json(bands):
    return [
        {"direction": b.direction.value, "days": b.days.value, "start": b.start.isoformat(),
         "end": b.end.isoformat(), "rate": b.rate}
        for b in bands
    ]


def test_save_and_read_multi_rate_tariff(client):
    headers = register(client)
    tariff = {"name": "Hugh's", "standing_charge_pence_per_day": 48.5, "bands": as_json(FOUR_RATE)}
    resp = client.put("/api/system", json={**SYSTEM, "tariff": tariff}, headers=headers)
    assert resp.status_code == 200, resp.text
    saved = resp.json()["tariff"]
    assert saved["name"] == "Hugh's"
    assert len(saved["bands"]) == 9
    assert saved["bands"][0] == {
        "direction": "import", "days": "weekdays", "start": "02:00:00", "end": "05:00:00",
        "rate": 15.0,
    }


def test_rejects_incomplete_or_off_grid_tariffs(client):
    headers = register(client)
    gap = {"bands": as_json(FOUR_RATE[1:])}
    resp = client.put("/api/system", json={**SYSTEM, "tariff": gap}, headers=headers)
    assert resp.status_code == 422
    assert "Import has no rate on weekdays for 02:00-05:00" in resp.text
    odd = as_json(FOUR_RATE)
    odd[0]["start"] = "02:15"
    resp = client.put("/api/system", json={**SYSTEM, "tariff": {"bands": odd}}, headers=headers)
    assert resp.status_code == 422


def test_presets_endpoint(client):
    presets = client.get("/api/system/tariff-presets").json()
    assert {p["id"] for p in presets} >= {"flat", "octopus_flux", "octopus_go", "octopus_cosy"}


def test_upgrades_step_one_tariffs():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE tariffs (id INTEGER PRIMARY KEY, system_id INTEGER UNIQUE, "
            "day_import_rate FLOAT, cheap_import_rate FLOAT, export_rate FLOAT, "
            "cheap_start TIME, cheap_end TIME)"
        ))
        conn.execute(text(
            "INSERT INTO tariffs VALUES (1, 7, 27.5, 7.5, 15, '23:30:00.000000', '05:30:00.000000')"
        ))
    upgrade(engine)
    upgrade(engine)  # second run is a no-op
    assert "day_import_rate" not in {c["name"] for c in inspect(engine).get_columns("tariffs")}
    with Session(engine) as db:
        tariff = db.query(Tariff).one()
        assert tariff.system_id == 7
        s = TariffSchedule.from_bands(tariff.bands, "Europe/London")
    assert s.rate_at(utc(2026, 12, 2, 23, 30), "import") == 7.5
    assert s.rate_at(utc(2026, 12, 2, 5, 30), "import") == 27.5
    assert s.rate_at(utc(2026, 12, 2, 12, 0), "export") == 15
