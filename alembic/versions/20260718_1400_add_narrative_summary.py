"""add extractions.narrative_summary

Revision ID: c4e9a17f52d8
Revises: 0d027cd96ddb
Create Date: 2026-07-18 14:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e9a17f52d8"
down_revision: str | None = "0d027cd96ddb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, no default: absent means "no narrative was summarized" (non-EPCR
    # documents, pre-existing rows, or a failed best-effort summary call).
    op.add_column("extractions", sa.Column("narrative_summary", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("extractions", "narrative_summary")
