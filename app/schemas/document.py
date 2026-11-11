import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.document import DocumentStatus, DocumentType
from app.schemas.extraction import PatientExtraction


class DocumentRead(BaseModel):
    """Everything the API discloses about a document, and nothing more.

    `storage_key` is absent: it is the GCM associated data, and there is no
    reason for a client to learn it. `original_filename` is decrypted on the way
    out, per request, by the service.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    doc_type: DocumentType
    status: DocumentStatus
    original_filename: str
    content_type: str
    size_bytes: int
    page_count: int | None
    sha256: str
    failure_reason: str | None
    created_at: datetime
    ocr_completed_at: datetime | None
    ai_completed_at: datetime | None


class DocumentListItem(BaseModel):
    """A search hit. Carries the denormalized identity columns (Phase 5) that
    serve search -- not the encrypted filename, which stays out of a list."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    doc_type: DocumentType
    status: DocumentStatus
    patient_last_name: str | None
    patient_first_name: str | None
    patient_dob: date | None
    run_number: str | None
    created_at: datetime
    ocr_completed_at: datetime | None
    ai_completed_at: datetime | None
    # OCR's mean confidence, not extraction's -- lets a reviewer queue prioritize
    # low-confidence scans. None until OCR has completed.
    mean_confidence: float | None = None


class MismatchFlagRead(BaseModel):
    """A denormalized identity field that disagrees across an encounter's
    documents -- e.g. two documents sharing a `run_number` but disagreeing on
    `patient_dob`. See `app/services/patient_grouping_service.py`."""

    field: str
    values: list[str]


class RelatedDocumentsRead(BaseModel):
    """Other `COMPLETED` documents from the same encounter as the one
    requested, and any denormalized identity fields that disagree between
    them. Not a new disclosure: a caller who can view the anchor document can
    already view each related one individually via `GET /documents/{id}` --
    this just aggregates that same information."""

    related: list[DocumentListItem]
    mismatches: list[MismatchFlagRead]


class DocumentCursor(BaseModel):
    """Keyset position: the (created_at, id) of the last row returned. The client
    passes it back to fetch the next page -- no OFFSET scan."""

    created_at: datetime
    id: uuid.UUID


class DocumentPage(BaseModel):
    items: list[DocumentListItem]
    next_cursor: DocumentCursor | None = None


class FlagRead(BaseModel):
    """One advisory review flag (`app/services/flags.py`). `detail` carries
    field names and slot indices only -- never a document value."""

    code: str
    detail: str


class ExtractionRead(BaseModel):
    """The grounded field extraction. Every value here is PHI -- reading it is a
    disclosure, and the endpoint audits it."""

    model_config = ConfigDict(from_attributes=True)

    document_id: uuid.UUID
    llm_model: str
    repaired: bool
    created_at: datetime
    fields: PatientExtraction
    # LLM-generated digest of the crew narrative (EPCR only, best-effort).
    # NOT grounded -- render as unverified prose, never as a field value.
    narrative_summary: str | None = None
    # Computed at read time by the router, not stored -- rule updates apply to
    # already-processed documents without reprocessing.
    suggested_flags: list[FlagRead] = Field(default_factory=list)


class DocumentCreated(BaseModel):
    id: uuid.UUID
    status: DocumentStatus
    # True when the bytes were already on file. The caller gets the existing
    # document's id rather than a duplicate row.
    deduplicated: bool = False


class OcrLineRead(BaseModel):
    text: str
    confidence: float
    box: list[list[float]]


class OcrPageRead(BaseModel):
    page: int
    lines: list[OcrLineRead]


class OcrResultRead(BaseModel):
    """The OCR text and its geometry. Every field here is PHI."""

    model_config = ConfigDict(from_attributes=True)

    document_id: uuid.UUID
    raw_text: str
    page_texts: list[OcrPageRead]
    mean_confidence: float
    engine_version: str
    page_count: int
    duration_ms: int
    created_at: datetime

    @property
    def is_low_confidence(self) -> bool:  # pragma: no cover - convenience for callers
        return self.mean_confidence < 0.80
