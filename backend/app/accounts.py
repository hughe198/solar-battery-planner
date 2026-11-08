"""Make an account, or set a new password, from the server's command line.

With sign-ups closed (SBP_REGISTRATION_OPEN=false, the default) this is how the owner makes
their first account. Run it from the backend folder, as the user the service runs as, so it
reads the same settings and database:

    python -m app.accounts create you@example.com
    python -m app.accounts password you@example.com

The password is typed at a hidden prompt, twice, so it never appears in the shell's history.
"""

import argparse
import sys
from collections.abc import Callable
from getpass import getpass

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import select

from app import migrations
from app.database import Base, SessionLocal, engine
from app.models import User
from app.security import hash_password

MIN_PASSWORD, MAX_PASSWORD = 8, 128


def read_password(prompt: Callable[[str], str] = getpass) -> str:
    """A new password typed twice; raises ValueError if they differ or it's the wrong length."""
    first = prompt(f"New password ({MIN_PASSWORD} to {MAX_PASSWORD} characters): ")
    if not MIN_PASSWORD <= len(first) <= MAX_PASSWORD:
        raise ValueError(f"The password must be {MIN_PASSWORD} to {MAX_PASSWORD} characters.")
    if prompt("Type it again: ") != first:
        raise ValueError("The two passwords didn't match.")
    return first


def main(argv: list[str] | None = None, prompt: Callable[[str], str] = getpass) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.accounts", description="Make an account, or set a new password."
    )
    parser.add_argument(
        "action", choices=["create", "password"], help="make an account, or set a new password"
    )
    parser.add_argument("email")
    args = parser.parse_args(argv)

    try:
        email = TypeAdapter(EmailStr).validate_python(args.email).lower()
    except ValidationError:
        print(f"Not a valid email address: {args.email}", file=sys.stderr)
        return 2

    # As the server does on startup, so this works on a database it hasn't opened yet.
    migrations.upgrade(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        if args.action == "create" and user is not None:
            print(
                f"There is already an account for {email}. To change its password: "
                f"python -m app.accounts password {email}",
                file=sys.stderr,
            )
            return 1
        if args.action == "password" and user is None:
            print(f"There is no account for {email}.", file=sys.stderr)
            return 1
        try:
            password = read_password(prompt)
        except ValueError as exc:
            print(f"Nothing changed. {exc}", file=sys.stderr)
            return 1
        if user is None:
            db.add(User(email=email, password_hash=hash_password(password)))
            done = f"Account created for {email}. Sign in with it on the site."
        else:
            user.password_hash = hash_password(password)
            done = f"Password changed for {email}."
        db.commit()
    print(done)
    return 0


if __name__ == "__main__":
    sys.exit(main())
