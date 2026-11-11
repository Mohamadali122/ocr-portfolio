import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import SessionLocal, engine
from app.models.audit_log import AuditLog
from app.models.user import User, UserRole

TEST_PASSWORD = "a-sufficiently-long-password"


def _database_is_reachable() -> bool:
    try:
        with engine.connect():
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _database_is_reachable(), reason="requires Postgres; run inside `docker compose exec api`"
)

# The app role (which `engine`/`SessionLocal` connect as, since Phase 7's
# `add_app_role` migration) has no DELETE on audit_logs -- that is the point.
# Test cleanup that removes a fixture's own audit rows is exactly the kind of
# out-of-band operation the owner role, not the running app, is meant to do.
# Exported for other integration test modules with the same cleanup shape.
OwnerSessionLocal = sessionmaker(bind=create_engine(get_settings().database_url))


@pytest.fixture
def admin() -> Iterator[User]:
    """A real row in a real database, removed afterwards.

    Audit rows reference the user with ON DELETE RESTRICT, so they must go first.
    That ordering is the schema working as intended: a user with history cannot
    be deleted out from under it.
    """
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    with SessionLocal() as session:
        user = User(
            email=email,
            password_hash=hash_password(TEST_PASSWORD),
            role=UserRole.ADMIN,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        user_id = user.id

    with SessionLocal() as session:
        yield session.get(User, user_id)  # type: ignore[misc]

    with OwnerSessionLocal() as session:
        session.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        session.execute(delete(User).where(User.id == user_id))
        session.commit()


@pytest.fixture
def prebiller() -> Iterator[User]:
    """A non-admin user, for asserting `require_role` actually rejects someone."""
    email = f"prebiller-{uuid.uuid4().hex[:8]}@example.com"
    with SessionLocal() as session:
        user = User(
            email=email,
            password_hash=hash_password(TEST_PASSWORD),
            role=UserRole.PREBILLER,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        user_id = user.id

    with SessionLocal() as session:
        yield session.get(User, user_id)  # type: ignore[misc]

    with OwnerSessionLocal() as session:
        session.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        session.execute(delete(User).where(User.id == user_id))
        session.commit()


@pytest.fixture
def audit_rows() -> "callable[[uuid.UUID | None], list[AuditLog]]":  # type: ignore[valid-type]
    def _fetch(user_id: uuid.UUID | None) -> list[AuditLog]:
        with SessionLocal() as session:
            stmt = select(AuditLog).where(AuditLog.user_id == user_id)
            return list(session.execute(stmt).scalars())

    return _fetch
