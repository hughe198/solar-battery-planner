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
