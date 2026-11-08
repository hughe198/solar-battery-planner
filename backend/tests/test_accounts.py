import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import accounts
from app.database import Base
from app.models import User
from app.security import verify_password


@pytest.fixture
def db(monkeypatch):
    """An empty in-memory database in place of the real one."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(accounts, "engine", engine)
    monkeypatch.setattr(accounts, "SessionLocal", Session)
    return Session


def typed(*answers):
    """Stands in for the hidden prompt, answering in turn."""
    queue = list(answers)
    return lambda prompt: queue.pop(0)


def password_of(db, email):
    with db() as s:
        return s.scalar(select(User).where(User.email == email)).password_hash


def test_creates_the_first_account_with_sign_ups_closed(db, capsys):
    code = accounts.main(["create", "Owner@Example.com"], typed("sunshine123", "sunshine123"))
    assert code == 0
    assert "Account created for owner@example.com" in capsys.readouterr().out
    assert verify_password("sunshine123", password_of(db, "owner@example.com"))


def test_sets_a_new_password(db):
    accounts.main(["create", "owner@example.com"], typed("sunshine123", "sunshine123"))
    code = accounts.main(["password", "owner@example.com"], typed("moonlight99", "moonlight99"))
    assert code == 0
    assert verify_password("moonlight99", password_of(db, "owner@example.com"))


@pytest.mark.parametrize(
    ("argv", "answers", "message"),
    [
        (["create", "owner@example.com"], ("sunshine123", "sunshine124"), "didn't match"),
        (["create", "owner@example.com"], ("short", "short"), "8 to 128 characters"),
        (["password", "nobody@example.com"], (), "no account for nobody@example.com"),
        (["create", "not-an-email"], (), "Not a valid email address"),
    ],
)
def test_changes_nothing_when_something_is_wrong(db, capsys, argv, answers, message):
    assert accounts.main(argv, typed(*answers)) != 0
    assert message in capsys.readouterr().err
    with db() as s:
        assert s.scalar(select(User)) is None


def test_wont_make_a_second_account_for_one_email(db, capsys):
    accounts.main(["create", "owner@example.com"], typed("sunshine123", "sunshine123"))
    assert accounts.main(["create", "owner@example.com"], typed()) == 1
    assert "already an account" in capsys.readouterr().err
