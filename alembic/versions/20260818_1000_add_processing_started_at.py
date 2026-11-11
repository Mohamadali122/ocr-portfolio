"""add processing_started_at to documents and claims

Revision ID: a1b2c3d4e5f6
Revises: 5f2a9d6e1c3b
Create Date: 2026-08-18 10:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "5f2a9d6e1c3b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "claims",
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE documents SET processing_started_at = created_at "
        "WHERE processing_started_at IS NULL"
    )
    op.execute(
        "UPDATE claims SET processing_started_at = created_at WHERE processing_started_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("claims", "processing_started_at")
    op.drop_column("documents", "processing_started_at")
