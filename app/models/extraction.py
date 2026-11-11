import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Extraction(Base, TimestampMixin):
    """The LLM's grounded field extraction for one document.

    `fields` holds a validated `PatientExtraction.model_dump()` -- every value
    in it has already been proven to appear verbatim in that document's
    `OcrResult.raw_text`, or nulled out by the hallucination guard.
    """

    __tablename__ = "extractions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    # One extraction per document. A re-run replaces the row, same convention
    # as OcrResult.
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="CASCADE"), unique=True, nullable=False
    )

    fields: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    llm_model: Mapped[str] = mapped_column(String(64), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)

    # True if the first LLM response failed validation and the one repair
    # retry was used. Cheap signal for later triage of which documents are
    # borderline for the model.
    repaired: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    # LLM-written digest of the crew narrative (EPCR only), for the reviewer.
    # Generated prose, NOT grounded -- the hallucination guard cannot prove a
    # summary, so the UI must present it as unverified. PHI, like `fields`.
    narrative_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        # No fields. A repr reaches exception messages and logs.
        return f"<Extraction document_id={self.document_id}>"
