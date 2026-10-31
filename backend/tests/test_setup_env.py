import stat

import pytest

import setup_env
from app.config import Settings


def answers(*replies):
    it = iter(replies)
    return lambda prompt="": next(it, "")


def load(path):
    return Settings(_env_file=path)


def test_covers_every_setting():
    assert {v.field for v in setup_env.VARS} == set(Settings.model_fields)


def test_defaults_write_a_file_settings_can_read(tmp_path):
    out = tmp_path / ".env"
    assert setup_env.main(["--output", str(out), "--defaults"]) == 0
    s = load(out)
    assert s.database_url == "sqlite:///./solar.db"
    assert s.cors_origins == ["http://localhost:4200"]
    assert s.auto_apply is False
    assert len(s.jwt_secret) >= 64 and len(s.credentials_secret) >= 64
    assert s.jwt_secret != s.credentials_secret
    assert "dev-only" not in s.jwt_secret
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_interactive_answers(tmp_path):
    out = tmp_path / ".env"
    replies = answers(
        "n",  # no advanced settings
        "postgresql+psycopg://u:p#ss@localhost/solar",
        "",  # generated JWT secret
        "short",  # rejected
        "x" * 40,  # own credentials secret
        "60",
        "http://localhost:4200, https://solar.example.com/",
        "yes",
    )
    assert setup_env.main(["--output", str(out)], input_fn=replies) == 0
    s = load(out)
    assert s.database_url == "postgresql+psycopg://u:p#ss@localhost/solar"
    assert s.credentials_secret == "x" * 40
    assert s.access_token_minutes == 60
    assert s.cors_origins == ["http://localhost:4200", "https://solar.example.com"]
    assert s.auto_apply is True


def test_existing_file_needs_confirmation(tmp_path):
    out = tmp_path / ".env"
    out.write_text("SBP_JWT_SECRET=keep\n")
    assert setup_env.main(["--output", str(out), "--defaults"], input_fn=answers("n")) == 1
    assert out.read_text() == "SBP_JWT_SECRET=keep\n"


def test_rerun_keeps_secrets_and_extra_lines(tmp_path):
    out = tmp_path / ".env"
    setup_env.main(["--output", str(out), "--defaults"])
    before = load(out)
    with out.open("a") as f:
        f.write("MY_OTHER_VAR=1\n")
    assert setup_env.main(["--output", str(out), "--defaults"], input_fn=answers("y")) == 0
    after = load(out)
    assert after.jwt_secret == before.jwt_secret
    assert after.credentials_secret == before.credentials_secret
    assert "# MY_OTHER_VAR=1" in out.read_text()


def test_replacing_credentials_secret_needs_confirmation(tmp_path):
    out = tmp_path / ".env"
    setup_env.main(["--output", str(out), "--defaults"])
    old = load(out).credentials_secret
    # overwrite: yes, advanced: no, db: keep, jwt: keep, credentials: new -> decline
    replies = answers("y", "n", "", "", "new", "n", "", "", "")
    assert setup_env.main(["--output", str(out)], input_fn=replies) == 0
    assert load(out).credentials_secret == old


@pytest.mark.parametrize("value", ["has space", 'quote"d', "back\\slash", "it's"])
def test_awkward_values_round_trip(tmp_path, value):
    out = tmp_path / ".env"
    values = {v.env: setup_env.to_env_value(v, v.default or "s" * 40) for v in setup_env.VARS}
    values["SBP_DATABASE_URL"] = value
    out.write_text(setup_env.render(values))
    assert load(out).database_url == value
    assert setup_env.parse_env(out.read_text())["SBP_DATABASE_URL"] == value
