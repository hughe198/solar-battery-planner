from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect

from app import migrations


def test_prepare_creates_tables_and_runs_twice():
    engine = create_engine("sqlite://")
    migrations.prepare(engine)
    migrations.prepare(engine)  # what a second worker, or a restart, does
    assert inspect(engine).has_table("usage_records")


class FakeConnection:
    def __init__(self, log):
        self.log = log

    def execute(self, statement, params=None):
        self.log.append(str(statement).split("(")[0].removeprefix("SELECT "))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.log.append("close")


def fake_engine(dialect, log):
    return SimpleNamespace(
        dialect=SimpleNamespace(name=dialect), connect=lambda: FakeConnection(log)
    )


def test_postgres_prepares_inside_the_advisory_lock(monkeypatch):
    log = []
    monkeypatch.setattr(migrations, "upgrade", lambda engine: log.append("upgrade"))
    monkeypatch.setattr(_metadata(), "create_all", lambda engine: log.append("create_all"))
    migrations.prepare(fake_engine("postgresql", log))
    assert log == [
        "pg_advisory_lock",
        "upgrade",
        "create_all",
        "pg_advisory_unlock",
        "close",
    ]


def test_lock_is_released_when_preparing_fails(monkeypatch):
    log = []

    def broken(engine):
        raise RuntimeError("bad migration")

    monkeypatch.setattr(migrations, "upgrade", broken)
    with pytest.raises(RuntimeError):
        migrations.prepare(fake_engine("postgresql", log))
    assert log == ["pg_advisory_lock", "pg_advisory_unlock", "close"]


def test_sqlite_takes_no_lock(monkeypatch):
    log = []
    monkeypatch.setattr(migrations, "upgrade", lambda engine: log.append("upgrade"))
    monkeypatch.setattr(_metadata(), "create_all", lambda engine: log.append("create_all"))
    migrations.prepare(fake_engine("sqlite", log))
    assert log == ["upgrade", "create_all"]


def _metadata():
    from app.database import Base

    return Base.metadata
