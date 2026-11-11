"""add keyset pagination indexes on documents and claims

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-18 11:00:00.000000+00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_documents_created_at_id", "documents", ["created_at", "id"])
    op.create_index("ix_claims_created_at_id", "claims", ["created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_claims_created_at_id", table_name="claims")
    op.drop_index("ix_documents_created_at_id", table_name="documents")
