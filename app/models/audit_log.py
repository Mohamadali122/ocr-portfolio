import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Uuid, func, text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditAction(StrEnum):
    """Stored as VARCHAR, not a Postgres ENUM. Every phase adds members here, and
    growing a Postgres ENUM needs ALTER TYPE ... ADD VALUE, which cannot run in a
    transaction."""

    LOGIN_SUCCEEDED = "login_succeeded"
    LOGIN_FAILED = "login_failed"
    TOKEN_REFRESHED = "token_refreshed"  # noqa: S105
    TOKEN_REJECTED = "token_rejected"  # noqa: S105

    DOCUMENT_UPLOADED = "document_uploaded"
    DOCUMENT_VIEWED = "document_viewed"
    DOCUMENT_DOWNLOADED = "document_downloaded"
    DOCUMENT_UPLOAD_REJECTED = "document_upload_rejected"

    EXTRACTION_FIELD_CORRECTED = "extraction_field_corrected"
    DOCUMENT_REPROCESSED = "document_reprocessed"
    # A reviewer's explicit override of the completeness gate -- pairs with
    # EXTRACTION_VALIDATION_FAILED, the automated event it overrides.
    EXTRACTION_MANUALLY_ACCEPTED = "extraction_manually_accepted"

    AUDIT_LOG_VIEWED = "audit_log_viewed"

    OCR_COMPLETED = "ocr_completed"
    OCR_FAILED = "ocr_failed"

    AI_COMPLETED = "ai_completed"
    AI_FAILED = "ai_failed"
    EXTRACTION_VALIDATION_FAILED = "extraction_validation_failed"

    CLAIM_CREATED = "claim_created"
    CLAIM_VIEWED = "claim_viewed"
    CLAIM_REPROCESSED = "claim_reprocessed"
    CLAIM_TAGS_ASSIGNED = "claim_tags_assigned"
    CLAIM_TAG_ADDED = "claim_tag_added"
    CLAIM_TAG_DISMISSED = "claim_tag_dismissed"
    CLAIM_STATE_REPAIRED = "claim_state_repaired"


class AuditLog(Base):
    """Append-only. Nothing in this application updates or deletes a row here.

    The Phase 7 migration enforces that with `REVOKE UPDATE, DELETE` on the
    application's database role. Until then it is a convention, and a convention
    is not a safeguard -- the proposal's "no audit record can be modified" is not
    yet true.

    `detail` must never contain PHI. It holds identifiers, enums, and reasons.
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Nullable: a failed login for an unknown email has no user to point at.
    # RESTRICT: a user with audit history cannot be deleted out from under it.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    # Deliberately NOT a foreign key.
    #
    # An audit ledger must outlive what it observes. A retention purge or a
    # patient deletion request removes a document; the record that it was
    # uploaded, viewed, and downloaded must remain. ON DELETE RESTRICT would make
    # that purge impossible forever, and ON DELETE CASCADE would destroy the
    # evidence.
    #
    # It also has to be writable from a transaction that has not yet committed
    # the document row -- see AuditService.record vs record_atomic.
    document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)

    # Same reasoning and same deliberate absence of a foreign key as
    # `document_id` above, one level up: a claim audit trail (CLAIM_CREATED,
    # CLAIM_TAGS_ASSIGNED, ...) must outlive the claim it describes.
    claim_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)

    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        # The auditor's query is "what did this user do, most recent first".
        Index("ix_audit_logs_user_id_occurred_at", "user_id", text("occurred_at DESC")),
    )

    def __repr__(self) -> str:
        return f"<AuditLog id={self.id} action={self.action}>"
