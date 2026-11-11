"""add claims, claim_documents, claim_tags; audit_logs.claim_id

Revision ID: 5f2a9d6e1c3b
Revises: c4e9a17f52d8
Create Date: 2026-07-27 23:30:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.config import get_settings

revision: str = "5f2a9d6e1c3b"
down_revision: str | None = "c4e9a17f52d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "claims",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "processing",
                "completed",
                "partial",
                "failed",
                name="claimstatus",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("patient_last_name", sa.String(length=100), nullable=True),
        sa.Column("patient_first_name", sa.String(length=100), nullable=True),
        sa.Column("patient_dob", sa.Date(), nullable=True),
        sa.Column("run_number", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_claims_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claims")),
    )
    op.create_index(op.f("ix_claims_created_by"), "claims", ["created_by"])
    op.create_index(op.f("ix_claims_status"), "claims", ["status"])
    op.create_index("ix_claims_patient_name", "claims", ["patient_last_name", "patient_first_name"])
    op.create_index("ix_claims_run_number", "claims", ["run_number"])

    op.create_table(
        "claim_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint("position >= 0", name=op.f("ck_claim_documents_position_non_negative")),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claims.id"],
            name=op.f("fk_claim_documents_claim_id_claims"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_claim_documents_document_id_documents"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_documents")),
        sa.UniqueConstraint("claim_id", "document_id", name="uq_claim_documents_claim_document"),
        sa.UniqueConstraint("claim_id", "position", name="uq_claim_documents_claim_position"),
    )
    op.create_index(op.f("ix_claim_documents_claim_id"), "claim_documents", ["claim_id"])
    op.create_index(op.f("ix_claim_documents_document_id"), "claim_documents", ["document_id"])

    op.create_table(
        "claim_tags",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("tag_name", sa.String(length=64), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "auto",
                "reviewer",
                name="claimtagsource",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="auto",
            nullable=False,
        ),
        sa.Column("rule_id", sa.String(length=64), nullable=True),
        sa.Column("confidence", sa.Float(), server_default="1.0", nullable=False),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("dismissed_by", sa.Uuid(), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name=op.f("ck_claim_tags_claim_tag_confidence_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claims.id"],
            name=op.f("fk_claim_tags_claim_id_claims"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["dismissed_by"],
            ["users.id"],
            name=op.f("fk_claim_tags_dismissed_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_tags")),
        sa.UniqueConstraint("claim_id", "tag_name", name="uq_claim_tags_claim_tag_name"),
    )
    op.create_index(op.f("ix_claim_tags_claim_id"), "claim_tags", ["claim_id"])

    op.add_column("audit_logs", sa.Column("claim_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_audit_logs_claim_id"), "audit_logs", ["claim_id"])

    # New tables, same restricted runtime role as every other one (see
    # `add_app_role`): the role's grants are per-table, not schema-wide, so a
    # table created after that migration ran starts with no grant at all --
    # `app/db/session.py` connects as this role for every request.
    app_user = get_settings().postgres_app_user
    op.execute(
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON claims, claim_documents, claim_tags TO "{app_user}"'
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_audit_logs_claim_id"), table_name="audit_logs")
    op.drop_column("audit_logs", "claim_id")

    op.drop_index(op.f("ix_claim_tags_claim_id"), table_name="claim_tags")
    op.drop_table("claim_tags")

    op.drop_index(op.f("ix_claim_documents_document_id"), table_name="claim_documents")
    op.drop_index(op.f("ix_claim_documents_claim_id"), table_name="claim_documents")
    op.drop_table("claim_documents")

    op.drop_index("ix_claims_run_number", table_name="claims")
    op.drop_index("ix_claims_patient_name", table_name="claims")
    op.drop_index(op.f("ix_claims_status"), table_name="claims")
    op.drop_index(op.f("ix_claims_created_by"), table_name="claims")
    op.drop_table("claims")
