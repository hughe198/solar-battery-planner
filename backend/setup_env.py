#!/usr/bin/env python3
"""Interactive helper that writes backend/.env for local or self-hosted deployment.

Walks through every setting the backend reads (app/config.py), explains each one, suggests a
value and generates the two secrets with the `secrets` module. Standard library only, so it
runs before `pip install`.

    python setup_env.py              # ask about each setting
    python setup_env.py --defaults   # accept every suggestion without asking
    python setup_env.py --output /path/to/.env --force

An existing .env is never overwritten without confirmation (or --force), and its values are
offered as the defaults, so re-running keeps your secrets unless you choose to change them.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PREFIX = "SBP_"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / ".env"


@dataclass(frozen=True)
class Var:
    field: str  # name of the field on app.config.Settings
    title: str
    help: str
    default: str = ""  # suggested value; ignored when `secret` is set
    secret: bool = False  # suggest a freshly generated value
    kind: str = "str"  # str | int | bool | origins | url
    advanced: bool = False  # only asked about when the user opts in

    @property
    def env(self) -> str:
        return PREFIX + self.field.upper()


VARS: list[Var] = [
    Var(
        "database_url",
        "Database",
        "SQLAlchemy URL for the database. SQLite is fine for development or one small server; "
        "the file is created next to where you start uvicorn. For a multi-user deployment use "
        "Postgres, e.g. postgresql+psycopg://user:password@localhost:5432/solar "
        "(and `pip install psycopg`).",
        default="sqlite:///./solar.db",
        kind="url",
    ),
    Var(
        "jwt_secret",
        "Login token secret",
        "Signs login tokens. Anyone who knows it can forge a login as any user, so it must be "
        "long and random and never committed. Changing it later just logs everyone out.",
        secret=True,
    ),
    Var(
        "credentials_secret",
        "Stored API key secret",
        "Encrypts the Fox ESS API keys users save. Keep it stable: if it changes, saved keys "
        "can't be decrypted and every user has to enter their key again. Back it up.",
        secret=True,
    ),
    Var(
        "access_token_minutes",
        "Login lifetime (minutes)",
        "How long a login lasts before the user has to sign in again. 10080 is 7 days.",
        default="10080",
        kind="int",
    ),
    Var(
        "cors_origins",
        "Allowed browser origins",
        "Comma-separated origins allowed to call the API from a browser. The Angular dev "
        "server is http://localhost:4200. Add your site's address if you serve the frontend "
        "from somewhere else.",
        default="http://localhost:4200",
        kind="origins",
    ),
    Var(
        "auto_apply",
        "Nightly automatic inverter apply",
        "Runs the nightly automatic apply of the charge plan to opted-in inverters inside this "
        "server process. Turn on for exactly one server process (or run "
        "`python -m app.control.nightly` from cron instead). Users still have to opt in and "
        "pass the write test first. Leave off until you have tested inverter control.",
        default="false",
        kind="bool",
    ),
    Var(
        "jwt_algorithm",
        "Login token algorithm",
        "JWT signing algorithm. Leave as HS256 unless you know you need something else.",
        default="HS256",
        advanced=True,
    ),
    Var(
        "fox_ess_base_url",
        "Fox ESS API address",
        "Base URL of the Fox ESS Open API.",
        default="https://www.foxesscloud.com",
        kind="url",
        advanced=True,
    ),
    Var(
        "open_meteo_archive_url",
        "Open-Meteo archive API",
        "Weather reanalysis for older days. Point at a paid or self-hosted Open-Meteo for "
        "commercial use.",
        default="https://archive-api.open-meteo.com/v1/archive",
        kind="url",
        advanced=True,
    ),
    Var(
        "open_meteo_recent_url",
        "Open-Meteo historical forecast API",
        "Weather for the last week (the archive lags by several days).",
        default="https://historical-forecast-api.open-meteo.com/v1/forecast",
        kind="url",
        advanced=True,
    ),
    Var(
        "open_meteo_forecast_url",
        "Open-Meteo forecast API",
        "Weather forecast for the days being planned.",
        default="https://api.open-meteo.com/v1/forecast",
        kind="url",
        advanced=True,
    ),
]


def generate_secret() -> str:
    """48 random bytes, URL-safe base64 (64 characters): safe to paste into .env unquoted."""
    return secrets.token_urlsafe(48)


# ---------------------------------------------------------------- reading and writing .env


def parse_env(text: str) -> dict[str, str]:
    """Parse the simple KEY=value lines this script writes (comments and blanks skipped)."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        values[key] = value
    return values


def to_env_value(var: Var, answer: str) -> str:
    """Convert a user answer into the string pydantic-settings expects."""
    if var.kind == "origins":
        origins = [o.strip().rstrip("/") for o in answer.split(",") if o.strip()]
        return json.dumps(origins)
    if var.kind == "bool":
        return "true" if answer.strip().lower() in {"true", "yes", "y", "1", "on"} else "false"
    return answer.strip()


def from_env_value(var: Var, value: str) -> str:
    """Turn a value read from an existing .env back into the form we ask for."""
    if var.kind == "origins":
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        if isinstance(parsed, list):
            return ", ".join(str(o) for o in parsed)
    return value


def validate(var: Var, answer: str) -> str | None:
    """Return an error message, or None if the answer is acceptable."""
    answer = answer.strip()
    if not answer:
        return "A value is needed."
    if var.kind == "int":
        if not answer.isdigit() or int(answer) <= 0:
            return "Enter a whole number above 0."
    elif var.kind == "bool":
        if answer.lower() not in {"true", "false", "yes", "no", "y", "n", "1", "0", "on", "off"}:
            return "Enter yes or no."
    elif var.kind == "origins":
        for origin in (o.strip() for o in answer.split(",")):
            if origin and not origin.startswith(("http://", "https://")):
                return f"{origin!r} should start with http:// or https://."
    elif (
        var.kind == "url"
        and var.field != "database_url"
        and not answer.startswith(("http://", "https://"))
    ):
        return "Enter a full URL starting with http:// or https://."
    if var.secret and len(answer) < 32:
        return "Use at least 32 characters (press Enter to accept the generated one)."
    if any(c in answer for c in "\n\r"):
        return "Values can't contain line breaks."
    return None


def render(values: dict[str, str]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Solar Battery Planner backend settings.",
        f"# Generated by setup_env.py on {stamp}. Keep this file out of git.",
        "",
    ]
    for var in VARS:
        lines.append(f"# {var.title}")
        value = values[var.env]
        # Quote values with spaces, "#" or quotes so they are read whole.
        if any(c in value for c in " #'\"") and not value.startswith("["):
            value = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
        lines.append(f"{var.env}={value}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- interaction


def ask(prompt: str, input_fn=input) -> str:
    try:
        return input_fn(prompt)
    except EOFError:
        return ""


def ask_yes_no(prompt: str, default: bool, input_fn=input) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        answer = ask(prompt + suffix, input_fn).strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("  Please answer y or n.")


def ask_var(var: Var, suggestion: str, existing: str | None, input_fn=input) -> str:
    print()
    print(f"{var.title}  ({var.env})")
    for line in _wrap(var.help):
        print(f"  {line}")
    if var.secret:
        if existing:
            print("  Current value kept unless you type 'new' or paste a replacement.")
        else:
            print("  A random value has been generated. Press Enter to use it, or paste your own.")
    shown = _mask(suggestion) if var.secret else suggestion
    while True:
        answer = ask(f"  Value [{shown}]: ", input_fn).strip()
        if var.secret and answer.lower() == "new":
            answer = generate_secret()
            if var.field == "credentials_secret" and existing:
                print("  Warning: saved Fox ESS keys will need to be entered again.")
                if not ask_yes_no("  Replace the stored API key secret anyway?", False, input_fn):
                    answer = ""
        if not answer:
            answer = suggestion
        error = validate(var, answer)
        if error is None:
            return answer
        print(f"  {error}")


def _mask(secret: str) -> str:
    return secret[:6] + "…" + secret[-4:] if len(secret) > 12 else "…"


def _wrap(text: str, width: int = 88) -> list[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    if line:
        lines.append(line)
    return lines


def collect(
    existing: dict[str, str], interactive: bool, include_advanced: bool | None, input_fn=input
) -> dict[str, str]:
    """Work out a value for every variable: existing value, else suggestion, else ask."""
    if interactive and include_advanced is None:
        print()
        include_advanced = ask_yes_no(
            "Also review advanced settings (token algorithm, API addresses)?", False, input_fn
        )
    values: dict[str, str] = {}
    for var in VARS:
        current = existing.get(var.env)
        current = from_env_value(var, current) if current else None
        if current and var.secret and validate(var, current):
            current = None  # e.g. the old dev-only default: too short, so replace it
        suggestion = current or (generate_secret() if var.secret else var.default)
        if interactive and (include_advanced or not var.advanced):
            answer = ask_var(var, suggestion, current, input_fn)
        else:
            answer = suggestion
        values[var.env] = to_env_value(var, answer)
    return values


def main(argv: list[str] | None = None, input_fn=input) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="where to write")
    parser.add_argument("--defaults", action="store_true", help="accept every suggestion")
    parser.add_argument("--advanced", action="store_true", help="also ask about advanced settings")
    parser.add_argument("--force", action="store_true", help="overwrite without asking")
    parser.add_argument("--print", action="store_true", help="print to stdout instead of writing")
    args = parser.parse_args(argv)

    output: Path = args.output
    interactive = not args.defaults
    existing: dict[str, str] = {}

    print("Solar Battery Planner: backend settings")
    if output.exists() and not args.print:
        existing = parse_env(output.read_text())
        print(f"\n{output} already exists. Its values are used as the defaults.")
        if not args.force and not ask_yes_no("Overwrite it when done?", False, input_fn):
            print("Nothing written.")
            return 1

    values = collect(existing, interactive, True if args.advanced else None, input_fn)
    unknown = sorted(k for k in existing if k not in values)
    text = render(values)
    if unknown:
        # The backend refuses to start if .env holds keys it doesn't know, so keep them as
        # comments rather than dropping them.
        text += "# Commented out: not settings the backend reads (it won't start with them).\n"
        text += "".join(f"# {k}={existing[k]}\n" for k in unknown)
        print(f"\nCommented out unknown settings: {', '.join(unknown)}.")

    if args.print:
        sys.stdout.write(text)
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text)
    try:
        output.chmod(0o600)  # secrets inside: owner read/write only
    except OSError:
        pass
    print(f"\nWrote {output}.")
    print("Back up SBP_CREDENTIALS_SECRET somewhere safe; it can't be recovered.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled. Nothing written.")
        sys.exit(130)
