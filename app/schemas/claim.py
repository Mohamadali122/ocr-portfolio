"""Claim read/write schemas.

Every value under `ClaimExtractionRead`/`ClaimTagRead.evidence` is PHI, same
discipline as `app.schemas.extraction` -- reading either endpoint is a
disclosure and the router audits it (see `app/api/v1/claims.py`).
"""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.claim import ClaimStatus, ClaimTagSource
from app.models.document import DocumentStatus


class ClaimMemberRead(BaseModel):
    document_id: uuid.UUID
    position: int
    status: DocumentStatus


class ClaimRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: ClaimStatus
    failure_reason: str | None
    created_at: datetime
    finalized_at: datetime | None
    patient_last_name: str | None
    patient_first_name: str | None
    patient_dob: date | None
    run_number: str | None
    members: list[ClaimMemberRead] = Field(default_factory=list)


class ClaimCursor(BaseModel):
    created_at: datetime
    id: uuid.UUID


class ClaimListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: ClaimStatus
    patient_last_name: str | None
    patient_first_name: str | None
    patient_dob: date | None
    run_number: str | None
    created_at: datetime
    finalized_at: datetime | None


class ClaimPage(BaseModel):
    items: list[ClaimListItem]
    next_cursor: ClaimCursor | None = None


class RejectedFileRead(BaseModel):
    filename: str
    reason: str


class AcceptedDocumentRead(BaseModel):
    filename: str
    document_id: uuid.UUID
    status: DocumentStatus
    deduplicated: bool


class ClaimCreated(BaseModel):
    claim_id: uuid.UUID
    accepted: list[AcceptedDocumentRead]
    rejected: list[RejectedFileRead]


class MergedFieldRead(BaseModel):
    value: str | None
    typed_value: str | None
    confidence: float
    source: str
    document_id: uuid.UUID | None
    source_page: int | None
    source_bbox: list[list[float]] | None


class MergedInsuranceSlotRead(BaseModel):
    insurance_name: MergedFieldRead
    policy_id: MergedFieldRead
    group_number: MergedFieldRead
    subscriber_name: MergedFieldRead
    relationship_to_subscriber: MergedFieldRead
    effective_date: MergedFieldRead
    verified: MergedFieldRead
    payer_address: MergedFieldRead
    payer_phone: MergedFieldRead


class FieldConflictRead(BaseModel):
    field: str
    values_by_document: list[tuple[uuid.UUID, str]]


class ClaimExtractionRead(BaseModel):
    """The claim-level merge (`app/services/claim_merge.py`), computed at
    read time -- not stored, so a precedence-table change applies to every
    existing claim instantly."""

    fields: dict[str, MergedFieldRead]
    insurance: list[MergedInsuranceSlotRead]
    conflicts: list[FieldConflictRead]


class EvidenceRead(BaseModel):
    kind: str
    field: str | None
    document_id: uuid.UUID | None
    page: int | None
    snippet: str


class ClaimTagRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tag_name: str
    source: ClaimTagSource
    rule_id: str | None
    confidence: float
    evidence: list[EvidenceRead]
    # docx REASON prose (app/services/tag_registry.py), joined in by the
    # router -- not stored on the row itself, so a docx wording fix applies
    # to every already-assigned tag without a migration.
    reason: str = ""


class ManualReviewTagRead(BaseModel):
    """A `TAGS.docx` tag with no rule in `tag_rules.toml` -- never
    auto-assigned. Surfaced so a reviewer can judge it by hand and add it via
    `POST /claims/{id}/tags` if it applies."""

    tag_name: str
    reason: str


class ClaimTagsRead(BaseModel):
    assigned: list[ClaimTagRead]
    manual_review: list[ManualReviewTagRead]


class TagRegistryEntryRead(BaseModel):
    """One row of `GET /tags` -- the registry itself. No PHI."""

    name: str
    reason: str
    has_rule: bool


class AddTagRequest(BaseModel):
    tag_name: str = Field(max_length=64)
