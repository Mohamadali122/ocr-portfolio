import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.document import DocumentStatus


class PipelineDocItem(BaseModel):
    """One row in the admin pipeline view. Deliberately narrower than
    `DocumentRead`/`DocumentListItem`: no patient name, DOB, or run number --
    an admin sees who triggered a document and its processing state, not the
    patient it concerns, beyond what normal document access already discloses.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: DocumentStatus
    uploaded_by: uuid.UUID
    uploader_email: str
    created_at: datetime
    failure_reason: str | None


class PipelineOverview(BaseModel):
    in_flight: list[PipelineDocItem]
    interrupted: list[PipelineDocItem]
    failed: list[PipelineDocItem]
    # Extraction ran but failed the completeness gate (extraction_validation.py)
    # -- distinct from `failed`, which is an OCR/LLM crash.
    extraction_failed: list[PipelineDocItem]


# Bulk reprocess re-runs OCR + LLM for every document selected -- the same
# per-document cost as a fresh upload. This caps one admin action from
# becoming an unbounded CPU/LLM spike, and doubles as the admin pipeline
# view's failed-list page size.
REPROCESS_BATCH_LIMIT = 25


class ReprocessFailedRequest(BaseModel):
    document_ids: list[uuid.UUID] = Field(min_length=1, max_length=REPROCESS_BATCH_LIMIT)


class ReprocessFailedResult(BaseModel):
    reprocessed: list[uuid.UUID]
    skipped: list[uuid.UUID]
