"""Engine, session factory, and the `get_db` dependency.

This is the only module that constructs a Session. Repositories receive one;
services receive repositories. Nothing else touches SQLAlchemy directly.

Connects as the restricted app role (`app_database_url`), not the migration
owner (`database_url`). Alembic uses the owner directly in `alembic/env.py`;
this process never needs DDL privileges, and the app role is the one that
cannot UPDATE or DELETE `audit_logs` -- see the `add_app_role` migration.
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_settings = get_settings()

engine = create_engine(
    _settings.app_database_url,
    echo=_settings.db_echo,
    pool_size=_settings.db_pool_size,
    max_overflow=_settings.db_pool_max_overflow,
    pool_pre_ping=True,  # a recycled connection after a db restart otherwise 500s once
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency. Commits on success, rolls back on any exception."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
