import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.control import service
from app.database import Base, get_db, get_session_factory
from app.main import app


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """Tests don't wait on Fox (D15's re-reads), and don't take the write block from the
    developer's .env: writes are off unless a test turns them on."""
    monkeypatch.setattr(service, "_wait", lambda seconds: None)
    monkeypatch.setattr(settings, "inverter_writes", False)


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_db():
        with TestSession() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_session_factory] = lambda: TestSession
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def register(client, email="hugh@example.com", password="sunshine123"):
    resp = client.post("/api/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}
