from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.models import PastTariff, RateDays, RateDirection, System, Tariff, TariffBand
from app.tariffs import TariffTimeline
from tests.conftest import register

SYSTEM = {
    "name": "Home",
    "latitude": 51.5,
    "longitude": -0.12,
    "timezone": "Europe/London",
    "battery": {"capacity_kwh": 10.4, "max_charge_kw": 5, "max_discharge_kw": 5},
    "tariff": {
        "name": "Octopus Go",
        "bands": [
            {"direction": "import", "start": "23:30", "end": "05:30", "rate": 7.5},
            {"direction": "import", "start": "05:30", "end": "23:30", "rate": 27.5},
            {"direction": "export", "start": "00:00", "end": "00:00", "rate": 15},
        ],
    },
    "arrays": [
        {"name": "East", "kwp": 2.4, "tilt_degrees": 35, "azimuth_degrees": 90},
        {"name": "West", "kwp": 2.4, "tilt_degrees": 35, "azimuth_degrees": 270},
    ],
}


def test_no_system_yet(client):
    headers = register(client)
    assert client.get("/api/system", headers=headers).status_code == 404


def test_create_and_update_system(client):
    headers = register(client)
    resp = client.put("/api/system", json=SYSTEM, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["planning_mode"] == "maximise_own_usage"
    assert body["battery"]["min_soc_percent"] == 10
    assert len(body["arrays"]) == 2

    updated = {**SYSTEM, "planning_mode": "maximise_export_revenue", "arrays": SYSTEM["arrays"][:1]}
    resp = client.put("/api/system", json=updated, headers=headers)
    assert resp.json()["planning_mode"] == "maximise_export_revenue"
    assert len(resp.json()["arrays"]) == 1
    assert resp.json()["id"] == body["id"]
    assert client.get("/api/auth/me", headers=headers).json()["has_system"] is True


def with_rate(cheap: float, effective: str | None = None) -> dict:
    bands = [dict(b) for b in SYSTEM["tariff"]["bands"]]
    bands[0]["rate"] = cheap
    system = {**SYSTEM, "tariff": {**SYSTEM["tariff"], "bands": bands}}
    return {**system, "tariff_effective_from": effective} if effective else system


def history(client, headers):
    resp = client.get("/api/system/tariff-history", headers=headers)
    assert resp.status_code == 200, resp.text
    return [
        (p["valid_from"], p["valid_until"], p["bands"][0]["rate"]) for p in resp.json()
    ]


def test_changing_rates_keeps_the_old_tariff_until_the_day_they_changed(client):
    headers = register(client)
    client.put("/api/system", json=SYSTEM, headers=headers)
    assert history(client, headers) == [(None, None, 7.5)]

    client.put("/api/system", json=with_rate(9, "2026-03-01"), headers=headers)
    client.put("/api/system", json=with_rate(11, "2026-06-01"), headers=headers)

    assert history(client, headers) == [
        ("2026-06-01", None, 11),
        ("2026-03-01", "2026-06-01", 9),
        (None, "2026-03-01", 7.5),
    ]


def test_saving_without_a_rate_change_keeps_no_history(client):
    headers = register(client)
    client.put("/api/system", json=SYSTEM, headers=headers)
    renamed = {**SYSTEM, "name": "Renamed", "tariff": {**SYSTEM["tariff"], "name": "New name"}}
    client.put("/api/system", json={**renamed, "tariff_effective_from": "2026-01-01"}, headers=headers)
    assert history(client, headers) == [(None, None, 7.5)]


def test_a_rate_change_defaults_to_today(client):
    headers = register(client)
    client.put("/api/system", json=SYSTEM, headers=headers)
    client.put("/api/system", json=with_rate(9), headers=headers)
    today = datetime.now(ZoneInfo("Europe/London")).date().isoformat()
    assert history(client, headers) == [(today, None, 9), (None, today, 7.5)]


def test_an_earlier_date_replaces_what_applied_from_then_on(client):
    headers = register(client)
    client.put("/api/system", json=SYSTEM, headers=headers)
    client.put("/api/system", json=with_rate(9, "2026-03-01"), headers=headers)
    client.put("/api/system", json=with_rate(11, "2026-06-01"), headers=headers)

    client.put("/api/system", json=with_rate(10, "2026-04-01"), headers=headers)
    assert history(client, headers) == [
        ("2026-04-01", None, 10),
        ("2026-03-01", "2026-04-01", 9),
        (None, "2026-03-01", 7.5),
    ]

    # Same day as the current tariff began: a correction, with nothing new to keep.
    client.put("/api/system", json=with_rate(10.5, "2026-04-01"), headers=headers)
    assert history(client, headers)[0] == ("2026-04-01", None, 10.5)
    assert len(history(client, headers)) == 3

    # On the day an older one ended, that one's period goes too.
    client.put("/api/system", json=with_rate(8, "2026-03-01"), headers=headers)
    assert history(client, headers) == [("2026-03-01", None, 8), (None, "2026-03-01", 7.5)]

    # Before everything: a correction to the whole history.
    client.put("/api/system", json=with_rate(7, "2020-01-01"), headers=headers)
    assert history(client, headers)[0][2] == 7
    assert history(client, headers)[-1][1] == "2020-01-01"


def test_a_tariff_cant_start_in_the_future(client):
    headers = register(client)
    client.put("/api/system", json=SYSTEM, headers=headers)
    later = (datetime.now(ZoneInfo("Europe/London")).date() + timedelta(days=2)).isoformat()
    resp = client.put("/api/system", json=with_rate(9, later), headers=headers)
    assert resp.status_code == 422
    assert history(client, headers) == [(None, None, 7.5)]


def test_past_days_are_priced_at_the_tariff_in_force_then():
    def bands(cheap: float) -> list[TariffBand]:
        return [
            TariffBand(
                direction=RateDirection.import_,
                days=RateDays.all,
                start=time(23, 30),
                end=time(5, 30),
                rate=cheap,
            ),
            TariffBand(
                direction=RateDirection.import_,
                days=RateDays.all,
                start=time(5, 30),
                end=time(23, 30),
                rate=27.5,
            ),
            TariffBand(
                direction=RateDirection.export,
                days=RateDays.all,
                start=time(0),
                end=time(0),
                rate=15,
            ),
        ]

    night = datetime(2026, 5, 31, 23, 0, tzinfo=timezone.utc)  # 00:00 BST on 1 June.
    system = System(
        timezone="Europe/London",
        tariff=Tariff(name="New", standing_charge_pence_per_day=0, bands=bands(9)),
        tariff_history=[
            PastTariff(
                valid_until=night.replace(tzinfo=None),
                name="Old",
                standing_charge_pence_per_day=0,
                bands=[
                    {
                        "direction": b.direction.value,
                        "days": b.days.value,
                        "start": b.start.isoformat(),
                        "end": b.end.isoformat(),
                        "rate": b.rate,
                    }
                    for b in bands(7.5)
                ],
            )
        ],
    )
    timeline = TariffTimeline.from_system(system)

    assert timeline.rate_at(night - timedelta(minutes=30), "import") == 7.5
    assert timeline.rate_at(night, "import") == 9
    assert timeline.rate_at(night.replace(tzinfo=None), "import") == 9
    assert timeline.rate_at(night - timedelta(days=30), "export") == 15


def test_systems_are_per_user(client):
    hugh = register(client)
    other = register(client, email="other@example.com")
    client.put("/api/system", json=SYSTEM, headers=hugh)
    assert client.get("/api/system", headers=other).status_code == 404


def test_validation(client):
    headers = register(client)
    bad = {**SYSTEM, "arrays": []}
    assert client.put("/api/system", json=bad, headers=headers).status_code == 422
    bad = {**SYSTEM, "timezone": "Mars/Olympus"}
    assert client.put("/api/system", json=bad, headers=headers).status_code == 422
    bad = {**SYSTEM, "tariff": {"bands": SYSTEM["tariff"]["bands"][1:]}}
    assert client.put("/api/system", json=bad, headers=headers).status_code == 422
