from sqlalchemy.exc import OperationalError

from app.database import get_db
from app.main import app


def test_health_reports_ok_when_the_database_answers(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_is_503_when_the_database_is_down(client):
    class BrokenSession:
        def execute(self, *_args, **_kwargs):
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    app.dependency_overrides[get_db] = lambda: BrokenSession()
    resp = client.get("/api/health")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "Database unavailable"
