from tests.conftest import register


def test_register_then_login(client):
    register(client)
    resp = client.post(
        "/api/auth/login", json={"email": "HUGH@example.com", "password": "sunshine123"}
    )
    assert resp.status_code == 200
    me = client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {resp.json()['access_token']}"}
    )
    assert me.json() == {"id": 1, "email": "hugh@example.com", "has_system": False}


def test_duplicate_email_rejected(client):
    register(client)
    resp = client.post(
        "/api/auth/register", json={"email": "hugh@example.com", "password": "another123"}
    )
    assert resp.status_code == 409


def test_wrong_password(client):
    register(client)
    resp = client.post("/api/auth/login", json={"email": "hugh@example.com", "password": "nope12345"})
    assert resp.status_code == 401


def test_me_requires_token(client):
    assert client.get("/api/auth/me").status_code == 401
    bad = client.get("/api/auth/me", headers={"Authorization": "Bearer junk"})
    assert bad.status_code == 401


def test_sign_ups_can_be_closed(client, monkeypatch):
    from app.config import settings

    assert client.get("/api/auth/options").json() == {"registration_open": True}
    headers = register(client)  # the owner's account, made while open
    monkeypatch.setattr(settings, "registration_open", False)
    assert client.get("/api/auth/options").json() == {"registration_open": False}
    resp = client.post(
        "/api/auth/register", json={"email": "stranger@example.com", "password": "letmein123"}
    )
    assert resp.status_code == 403
    assert "aren't being taken" in resp.json()["detail"]
    # Existing accounts still sign in.
    resp = client.post(
        "/api/auth/login", json={"email": "hugh@example.com", "password": "sunshine123"}
    )
    assert resp.status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 200


def test_sign_ups_are_closed_unless_the_settings_open_them():
    from app.config import Settings

    assert Settings(_env_file=None).registration_open is False
