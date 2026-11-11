"""add app role

Revision ID: b6212144c3f8
Revises: 37193a2a6ea8
Create Date: 2026-07-11 08:00:00.000000+00:00

Creates the restricted runtime role the application connects as
(`app/db/session.py`), separate from the owner role Alembic uses
(`app/core/config.py::Settings.database_url`). Idempotent: safe to
re-run against a database where the role already exists, so rotating
`POSTGRES_APP_PASSWORD` and re-running `alembic upgrade head` is how
that password is changed.

The role gets full CRUD on every table except `audit_logs`, which gets only
SELECT and INSERT. That is the database enforcing "no audit record can be
modified" -- until now it was true only because `AuditRepository` happens not
to expose an update or delete method.
"""

from collections.abc import Sequence

from alembic import op

from app.core.config import get_settings

revision: str = "b6212144c3f8"
down_revision: str | None = "37193a2a6ea8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FULL_CRUD_TABLES = ("users", "documents", "ocr_results", "extractions")


def upgrade() -> None:
    settings = get_settings()
    app_user = settings.postgres_app_user
    app_db = settings.postgres_db
    # Role names and the database name come from Settings, i.e. operator
    # config, never request input -- DDL cannot bind them as query parameters,
    # so they are quoted identifiers instead. The password is a SQL string
    # literal with embedded quotes doubled, standard escaping for that form.
    escaped_password = settings.postgres_app_password.get_secret_value().replace("'", "''")

    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{app_user}') THEN
                CREATE ROLE "{app_user}" LOGIN PASSWORD '{escaped_password}';
            ELSE
                ALTER ROLE "{app_user}" WITH LOGIN PASSWORD '{escaped_password}';
            END IF;
        END
        $$;
        """
    )

    op.execute(f'GRANT CONNECT ON DATABASE "{app_db}" TO "{app_user}"')
    op.execute(f'GRANT USAGE ON SCHEMA public TO "{app_user}"')
    op.execute(
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON {", ".join(_FULL_CRUD_TABLES)} '
        f'TO "{app_user}"'
    )
    # No UPDATE, no DELETE: append-only, enforced here rather than by convention.
    op.execute(f'GRANT SELECT, INSERT ON audit_logs TO "{app_user}"')
    # INSERT into a BigInteger-identity PK needs nextval() on its sequence.
    # The UUID-keyed tables above generate client-side and need no sequence.
    op.execute(f'GRANT USAGE, SELECT ON audit_logs_id_seq TO "{app_user}"')


def downgrade() -> None:
    settings = get_settings()
    app_user = settings.postgres_app_user

    # DROP OWNED revokes every privilege granted to the role in this database
    # (and drops anything it owns, though it owns nothing here) so DROP ROLE
    # does not fail with "role cannot be dropped because some objects depend on
    # it".
    op.execute(f'DROP OWNED BY "{app_user}"')
    op.execute(f'DROP ROLE IF EXISTS "{app_user}"')
