"""Operator CLI.

    python -m app.cli seed-admin --email admin@example.org

The password is read from the terminal, or from ADMIN_PASSWORD when stdin is not
a TTY (container start scripts). It is never accepted as an argument: argv lands
in shell history, `ps` output, and container inspect.
"""

import argparse
import getpass
import os
import sys
from collections.abc import Iterable
from typing import Any

from pydantic import TypeAdapter, ValidationError
from pydantic.networks import EmailStr
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models.document import Document
from app.models.extraction import Extraction
from app.models.user import User, UserRole
from app.repositories.user_repository import UserRepository


def _read_password(min_length: int) -> str:
    env_password = os.environ.get("ADMIN_PASSWORD")
    if env_password:
        password = env_password
    elif sys.stdin.isatty():
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Confirm: "):
            sys.exit("Passwords do not match.")
    else:
        sys.exit("No TTY and ADMIN_PASSWORD is unset.")

    if len(password) < min_length:
        sys.exit(f"Password must be at least {min_length} characters.")
    return password


_email_adapter = TypeAdapter(EmailStr)


def _validate_email(raw: str) -> str:
    """Same rule the login endpoint applies.

    Without this the CLI happily seeds `admin@ocr.local`, which `EmailStr`
    rejects as a special-use domain -- creating an account that exists in the
    database and can never authenticate.
    """
    try:
        return str(_email_adapter.validate_python(raw.strip())).lower()
    except ValidationError:
        sys.exit(f"Not a valid email address: {raw}")


def seed_admin(email: str) -> int:
    settings = get_settings()
    email = _validate_email(email)

    with SessionLocal() as session:
        users = UserRepository(session)
        if users.get_by_email(email):
            print(f"User already exists: {email}. Nothing to do.")
            return 0

        password = _read_password(settings.min_password_length)
        users.add(
            User(
                email=email,
                password_hash=hash_password(password),
                role=UserRole.ADMIN,
                is_active=True,
            )
        )
        session.commit()

    print(f"Created administrator: {email}")
    return 0


def reset_password(email: str) -> int:
    """Set a new password for an existing account and revoke its old tokens.

    Same password handling as seed-admin: read from the terminal, or from
    ADMIN_PASSWORD when stdin is not a TTY. Never an argument -- argv leaks.
    Bumping token_version invalidates every access and refresh token the user
    still holds, so a forgotten password cannot be used behind an old token.
    """
    settings = get_settings()
    email = _validate_email(email)

    with SessionLocal() as session:
        users = UserRepository(session)
        user = users.get_by_email(email)
        if user is None:
            sys.exit(f"No such user: {email}")

        password = _read_password(settings.min_password_length)
        user.password_hash = hash_password(password)
        user.token_version += 1
        session.commit()

    print(f"Password reset for {email}. Existing tokens are now revoked.")
    return 0


# (doc_type, field, n, exact, fuzzy, empty)
BenchRow = tuple[str, str, int, int, int, int]


def bench_tally(rows: Iterable[tuple[str, dict[str, Any]]]) -> list[BenchRow]:
    """Tally grounding tiers per (doc_type, field) from stored extractions.

    A field is `exact` at confidence 1.0, `fuzzy` at 0.7, `empty` when no value
    survived (either the guard nulled it or the model never returned one -- the
    stored shape cannot distinguish the two). Insurance subfields aggregate
    across slots as `insurance.<subfield>`. Counts only -- no PHI.
    """

    def tier(field: dict[str, Any]) -> str:
        if not field.get("value"):
            return "empty"
        return "exact" if field.get("confidence", 0.0) >= 1.0 else "fuzzy"

    counts: dict[tuple[str, str], dict[str, int]] = {}

    def bump(doc_type: str, name: str, which: str) -> None:
        row = counts.setdefault((doc_type, name), {"exact": 0, "fuzzy": 0, "empty": 0})
        row[which] += 1

    for doc_type, fields in rows:
        for name, field in fields.items():
            if name == "insurance":
                for slot in field:
                    for sub, sub_field in slot.items():
                        bump(doc_type, f"insurance.{sub}", tier(sub_field))
            else:
                bump(doc_type, name, tier(field))

    return [
        (
            doc_type,
            name,
            row["exact"] + row["fuzzy"] + row["empty"],
            row["exact"],
            row["fuzzy"],
            row["empty"],
        )
        for (doc_type, name), row in sorted(counts.items())
    ]


def bench() -> int:
    """Grounding-tier baseline over every stored extraction, as CSV on stdout.

    The before/after metric for any OCR or prompt change: re-process the same
    sample set, run `bench` again, diff the tier distribution. Field names and
    counts only -- no document values leave the database.
    """
    with SessionLocal() as session:
        stmt = select(Document.doc_type, Extraction.fields).join(
            Extraction, Extraction.document_id == Document.id
        )
        rows = [(doc_type.value, fields) for doc_type, fields in session.execute(stmt)]

    print("doc_type,field,n,exact,fuzzy,empty")
    for doc_type, name, n, exact, fuzzy, empty in bench_tally(rows):
        print(f"{doc_type},{name},{n},{exact},{fuzzy},{empty}")
    print(f"# extractions: {len(rows)}", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed-admin", help="Create the single administrator account.")
    seed.add_argument("--email", required=True)

    reset = sub.add_parser("reset-password", help="Set a new password for an existing account.")
    reset.add_argument("--email", required=True)

    sub.add_parser("bench", help="Grounding-tier baseline over stored extractions (CSV).")

    args = parser.parse_args()
    if args.command == "seed-admin":
        return seed_admin(args.email)
    if args.command == "reset-password":
        return reset_password(args.email)
    if args.command == "bench":
        return bench()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
