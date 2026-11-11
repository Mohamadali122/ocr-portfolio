import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class DocumentType(StrEnum):
    HFS = "hfs"  # Hospital Face Sheet
    EPCR = "epcr"  # Electronic Patient Care Report
    PCS = "pcs"  # Physician Certification Statement (medical necessity)
    BOOKING_SUMMARY = "booking_summary"  # EMS dispatch/scheduling record
    UNKNOWN = "unknown"


class DocumentStatus(StrEnum):
    PENDING = "pending"
    OCR_RUNNING = "ocr_running"
    AI_RUNNING = "ai_running"
    COMPLETED = "completed"  # extraction passed the completeness gate -- review-eligible
    FAILED = "failed"  # OCR or the LLM call itself crashed
    # The AI stage ran and produced fields, but they failed the completeness
    # gate (app/services/extraction_validation.py) -- e.g. an empty LLM
    # response, or a near-blank page (sample/ANALYSIS.md: near_blank_booking_1.pdf).
    # Distinct from FAILED so a crash and "nothing to review" are never
    # conflated in the admin view or a reviewer's queue.
    EXTRACTION_FAILED = "extraction_failed"


def _enum(python_enum: type[StrEnum], name: str) -> Enum:
    # VARCHAR + CHECK, never a Postgres ENUM: growing one needs ALTER TYPE ...
    # ADD VALUE, which cannot run inside a transaction.
    return Enum(
        python_enum,
        native_enum=False,
        length=32,
        create_constraint=True,
        name=name,
        values_callable=lambda e: [m.value for m in e],
    )


class Document(Base, TimestampMixin):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    doc_type: Mapped[DocumentType] = mapped_column(
        _enum(DocumentType, "documenttype"), default=DocumentType.UNKNOWN, nullable=False
    )

    # Patient names appear in filenames: "SMITH_JOHN_facesheet.pdf". A plaintext
    # column here would disclose PHI to anyone holding SELECT on this table.
    original_filename_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    # 32 hex chars. Names a file under DATA_DIR, and is the GCM associated data
    # that binds the ciphertext to this row.
    storage_key: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)

    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Dedup, and tamper-evidence: proves the stored ciphertext still decrypts to
    # the bytes that were uploaded.
    sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)

    status: Mapped[DocumentStatus] = mapped_column(
        _enum(DocumentStatus, "documentstatus"),
        default=DocumentStatus.PENDING,
        server_default=DocumentStatus.PENDING.value,
        nullable=False,
        index=True,
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    ocr_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ai_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Denormalized from the extraction (Phase 5) purely to serve search. There is
    # no `patients` table in the pilot: deciding when two scans are the same
    # person is the SOP's "merge patient" problem, and every premature answer to
    # it becomes a data migration. See the Phase 0 plan.
    patient_last_name: Mapped[str | None] = mapped_column(String(100))
    patient_first_name: Mapped[str | None] = mapped_column(String(100))
    patient_dob: Mapped[date | None] = mapped_column(Date)
    run_number: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint("page_count IS NULL OR page_count > 0", name="page_count_positive"),
        # Search: "documents for this patient" and "documents for this run".
        Index("ix_documents_patient_name", "patient_last_name", "patient_first_name"),
        Index("ix_documents_run_number", "run_number"),
        Index("ix_documents_patient_dob", "patient_dob"),
        Index("ix_documents_created_at_id", "created_at", "id"),
    )

    def __repr__(self) -> str:
        # No filename, no patient name. A repr ends up in exception messages.
        return f"<Document id={self.id} type={self.doc_type} status={self.status}>"
