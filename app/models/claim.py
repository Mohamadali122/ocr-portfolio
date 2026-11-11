import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import conv

from app.db.base import Base, TimestampMixin


class ClaimStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"  # every member document reached COMPLETED
    PARTIAL = "partial"  # a mix of terminal outcomes -- some completed, some not
    FAILED = "failed"  # no member document reached COMPLETED


def _enum(python_enum: type[StrEnum], name: str) -> Enum:
    # VARCHAR + CHECK, never a Postgres ENUM -- same reasoning as
    # app/models/document.py::_enum: growing a Postgres ENUM needs ALTER TYPE
    # ... ADD VALUE, which cannot run inside a transaction.
    return Enum(
        python_enum,
        native_enum=False,
        length=32,
        create_constraint=True,
        name=name,
        values_callable=lambda e: [m.value for m in e],
    )


class Claim(Base, TimestampMixin):
    """A patient transfer: one or more `Document`s uploaded together and
    processed as a unit. See `app/services/claim_merge.py` for how their
    per-document extractions are combined, and `app/services/tagging.py` for
    how `TAGS.docx`-defined tags are assigned from the merge.

    No merged-extraction column: the merge is a pure function of the member
    documents' `Extraction` rows, computed at read time (same reasoning as
    `app/services/flags.py` and `app/services/patient_grouping_service.py`) --
    a precedence-table change applies to every existing claim instantly, with
    no migration and no second copy of PHI on disk.
    """

    __tablename__ = "claims"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    status: Mapped[ClaimStatus] = mapped_column(
        _enum(ClaimStatus, "claimstatus"),
        default=ClaimStatus.PENDING,
        server_default=ClaimStatus.PENDING.value,
        nullable=False,
        index=True,
    )
    # Operator-facing: exception class name and stage only, same contract as
    # Document.failure_reason / DocumentRepository.mark_failed. Never a document value.
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Denormalized from the merged extraction, purely to serve search -- same
    # convention and same caveat as Document.patient_last_name etc: there is no
    # `patients` table in the pilot (see CLAUDE.md "Database conventions").
    patient_last_name: Mapped[str | None] = mapped_column(String(100))
    patient_first_name: Mapped[str | None] = mapped_column(String(100))
    patient_dob: Mapped[date | None] = mapped_column(Date)
    run_number: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (
        Index("ix_claims_patient_name", "patient_last_name", "patient_first_name"),
        Index("ix_claims_run_number", "run_number"),
        Index("ix_claims_created_at_id", "created_at", "id"),
    )

    def __repr__(self) -> str:
        # No patient identity. A repr reaches exception messages and logs.
        return f"<Claim id={self.id} status={self.status}>"


class ClaimDocument(Base):
    """Join table binding a `Document` to a `Claim`, in upload order.

    A join table, not a `documents.claim_id` column: `documents.sha256` is
    UNIQUE and `DocumentService.upload` dedups on it, so the same bytes
    uploaded in two different claims must resolve to the same `Document` row.
    A FK column on `documents` would make that document unassignable to the
    second claim. `position` carries the claim's document ordering (page
    ordering across the bundle) without touching `documents` at all.

    `document_id` is RESTRICT, not CASCADE: a document belongs to at least the
    claim(s) that reference it, and deleting a claim must not silently delete
    the underlying document out from under another claim that shares it.
    """

    __tablename__ = "claim_documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    claim_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("claims.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        # `conv(...)`, not a plain string: the naming convention's "uq" format
        # (`db/base.py`) is `uq_%(table_name)s_%(column_0_name)s` -- keyed on
        # the FIRST column only, with no `%(constraint_name)s` token, so it
        # ignores whatever explicit name is given and derives one from the
        # leading column alone. Both constraints here lead with `claim_id`,
        # so unwrapped they would silently collide on the same derived name
        # (`uq_claim_documents_claim_id`) instead of raising -- `conv()` marks
        # these names as already final, opting them out of that derivation.
        UniqueConstraint("claim_id", "document_id", name=conv("uq_claim_documents_claim_document")),
        UniqueConstraint("claim_id", "position", name=conv("uq_claim_documents_claim_position")),
        CheckConstraint("position >= 0", name="position_non_negative"),
    )

    def __repr__(self) -> str:
        return f"<ClaimDocument claim_id={self.claim_id} document_id={self.document_id}>"


class ClaimTagSource(StrEnum):
    AUTO = "auto"  # assigned by app/services/tagging.py
    REVIEWER = "reviewer"  # added by a reviewer via POST /claims/{id}/tags


class ClaimTag(Base, TimestampMixin):
    """One tag assigned to a claim -- `tag_name` is validated against the
    `TAGS.docx`-derived registry (`app/services/tag_registry.py`) at
    assignment time, never against a database-side enum: the registry is the
    single source of truth for which tags exist, and a DB CHECK constraint
    here would need a migration every time `TAGS.docx` grows one.

    `evidence` is PHI (matched text, field values, snippets) and must never
    reach a log record or an `AuditLog.detail` -- see `app/services/tagging.py`
    and CLAUDE.md's PHI rules. `rule_id` is the TOML `[[tag]]` entry that
    fired, recorded so a future ruleset change can be diffed against what
    actually produced a stored assignment.

    A reprocess replaces only `source == auto` rows (`ClaimService.try_finalize`)
    -- a reviewer's manual add or dismissal survives a re-run.
    """

    __tablename__ = "claim_tags"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    claim_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("claims.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tag_name: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[ClaimTagSource] = mapped_column(
        _enum(ClaimTagSource, "claimtagsource"),
        default=ClaimTagSource.AUTO,
        server_default=ClaimTagSource.AUTO.value,
        nullable=False,
    )
    rule_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[float] = mapped_column(
        Float, default=1.0, server_default="1.0", nullable=False
    )
    # list[{"kind", "field", "document_id", "page", "snippet"}] -- PHI, see docstring above.
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    # No separate `assigned_at`: `created_at` (TimestampMixin) already is that
    # timestamp -- this row is created at the moment the tag is assigned.

    dismissed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # conv(): see the identical note on ClaimDocument.__table_args__ --
        # without it the "uq" convention derives the name from `claim_id`
        # alone (`uq_claim_tags_claim_id`), not this more descriptive one.
        UniqueConstraint("claim_id", "tag_name", name=conv("uq_claim_tags_claim_tag_name")),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="claim_tag_confidence_in_range"
        ),
    )

    def __repr__(self) -> str:
        # No evidence. A repr reaches exception messages and logs.
        return f"<ClaimTag claim_id={self.claim_id} tag_name={self.tag_name!r}>"
