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
