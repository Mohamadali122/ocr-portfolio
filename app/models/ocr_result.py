import uuid
from typing import Any

from sqlalchemy import CheckConstraint, Float, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class OcrResult(Base, TimestampMixin):
    """The text a document yielded, and where on the page each line sat.

    `raw_text` and `page_texts` are both PHI. They are the densest PHI in the
    system: a face sheet's OCR text contains the name, DOB, SSN, address, and
    policy numbers all at once.
    """

    __tablename__ = "ocr_results"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    # One result per document. A re-run replaces the row rather than appending,
    # so `document_id` is unique. Versioned re-OCR is a post-pilot concern.
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="CASCADE"), unique=True, nullable=False
    )

    raw_text: Mapped[str] = mapped_column(Text, nullable=False)

    # Per-page, per-line: {"page": 1, "lines": [{"text":..., "confidence":..., "box":...}]}
    #
    # The boxes are not decoration. Phase 5's hallucination guard has to prove
    # each extracted field appears in the OCR text, and the review UI has to show
    # the prebiller *where* on the scan a value came from. Extracting geometry
    # later would mean re-running OCR over every stored document.
    page_texts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)

    # Mean over lines, not over pages. Surfaced to the reviewer: a low-confidence
    # scan must never be silently trusted. The prebilling SOP has a whole section
    # on unclear face sheets, so bad scans are the norm, not the exception.
    mean_confidence: Mapped[float] = mapped_column(Float, nullable=False)

    engine_version: Mapped[str] = mapped_column(String(64), nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "mean_confidence >= 0 AND mean_confidence <= 1", name="confidence_in_range"
        ),
        CheckConstraint("page_count > 0", name="page_count_positive"),
    )

    def __repr__(self) -> str:
        # No text. A repr reaches exception messages and logs.
        return f"<OcrResult document_id={self.document_id} pages={self.page_count}>"
