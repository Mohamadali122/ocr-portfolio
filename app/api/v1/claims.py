import uuid
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile, status

from app.api.deps import AuditServiceDep, ClaimServiceDep, CurrentUser, DbDep, client_ip
from app.api.v1.documents import read_capped
from app.core.config import get_settings
from app.core.exceptions import ConflictError
from app.core.rate_limit import limiter
from app.models.audit_log import AuditAction
from app.models.claim import Claim, ClaimStatus
from app.models.document import DocumentStatus
from app.repositories.claim_repository import ClaimRepository
from app.repositories.document_repository import DocumentRepository
from app.repositories.extraction_repository import ExtractionRepository
from app.schemas.claim import (
    AddTagRequest,
    ClaimCreated,
    ClaimCursor,
    ClaimExtractionRead,
    ClaimMemberRead,
    ClaimPage,
    ClaimRead,
    ClaimTagRead,
    ClaimTagsRead,
    EvidenceRead,
    FieldConflictRead,
    ManualReviewTagRead,
    MergedFieldRead,
    MergedInsuranceSlotRead,
    TagRegistryEntryRead,
)
from app.services.audit_service import AuditService
from app.services.claim_service import UploadedFile
from app.services.file_validation import FileTooLargeError
from app.services.tag_registry import load_registry
from app.worker import finalize_claim_task, process_document_task

router = APIRouter(tags=["claims"])

_FINALIZE_COUNTDOWN_SECONDS = 15


@router.post("/claims", response_model=ClaimCreated, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(lambda: f"{get_settings().rate_limit_upload_per_minute}/minute")
async def create_claim(
    user: CurrentUser,
    service: ClaimServiceDep,
    db: DbDep,
    request: Request,
    ip: Annotated[str | None, Depends(client_ip)],
    files: Annotated[list[UploadFile], File()],
) -> ClaimCreated:
    """202: every accepted file's bytes are stored, no processing has run yet.

    Poll `GET /claims/{id}` until `status` leaves `pending`/`processing`. One
    bad file does not sink the bundle -- it comes back in `rejected` while
    every other file's document is created and dispatched normally. The
    claim itself is refused (422) only if every file was rejected.
    """
    settings = get_settings()
    if len(files) > settings.max_claim_files:
        raise FileTooLargeError(f"A claim may contain at most {settings.max_claim_files} files.")

    uploaded = []
    for f in files:
        data = await read_capped(f, settings.max_upload_bytes)
        uploaded.append(
            UploadedFile(filename=f.filename or "unnamed", content_type=f.content_type, data=data)
        )

    result = service.create(
        files=uploaded,
        user=user,
        ip_address=ip,
        user_agent=request.headers.get("user-agent", "")[:255] or None,
    )

    documents = DocumentRepository(db)
    to_dispatch: list[uuid.UUID] = []
    for doc in result.accepted:
        if not doc.deduplicated:
            to_dispatch.append(doc.document_id)
        elif documents.rearm_if_failed(doc.document_id):
            to_dispatch.append(doc.document_id)
    db.commit()
    for document_id in to_dispatch:
        process_document_task.delay(str(document_id))
    finalize_claim_task.apply_async(
        args=[str(result.claim_id)], countdown=_FINALIZE_COUNTDOWN_SECONDS
    )

    return ClaimCreated(
        claim_id=result.claim_id,
        accepted=[
            {
                "filename": a.filename,
                "document_id": a.document_id,
                "status": a.status,
                "deduplicated": a.deduplicated,
            }
            for a in result.accepted
        ],
        rejected=[{"filename": r.filename, "reason": r.reason} for r in result.rejected],
    )


@router.post(
    "/claims/{claim_id}/reprocess", response_model=ClaimRead, status_code=status.HTTP_202_ACCEPTED
)
@limiter.limit(lambda: f"{get_settings().rate_limit_upload_per_minute}/minute")
def reprocess_claim(
    request: Request,
    claim_id: uuid.UUID,
    user: CurrentUser,
    service: ClaimServiceDep,
    db: DbDep,
    ip: Annotated[str | None, Depends(client_ip)],
    force: Annotated[bool, Query()] = False,
) -> ClaimRead:
    """Re-run every member document and re-finalize the claim -- same guard
    and rate limit as `documents.reprocess_document`, applied claim-wide:
    refuses by default if *any* member has a reviewer-corrected field (that
    document's `ExtractionRepository.add` re-run would discard it), one 409
    naming the total count across every member rather than one member at a
    time. `force=true` overrides for all of them at once. Member documents
    are then re-armed individually (each is its own
    `DocumentRepository.claim_for_reprocess` guard); a document already
    mid-pipeline is simply skipped rather than failing the whole request."""
    claim = service.get(claim_id)  # 404s if the claim does not exist
    members = service.members(claim_id)

    if not force:
        extractions = ExtractionRepository(db)
        corrected = sum(
            extractions.count_corrected_fields(extraction)
            for document_id, _position, _status in members
            if (extraction := extractions.get_by_document_id(document_id)) is not None
        )
        if corrected:
            raise ConflictError(
                f"This claim has {corrected} manually corrected field"
                f"{'s' if corrected != 1 else ''} across its documents that reprocessing "
                "would discard. Retry with force=true to override."
            )

    if not ClaimRepository(db).claim_for_reprocess(claim_id):
        raise ConflictError("Claim is already processing.")

    documents = DocumentRepository(db)
    for document_id, _position, _status in members:
        if documents.claim_for_reprocess(document_id):
            process_document_task.delay(str(document_id))

    AuditService.record_atomic(
        db, action=AuditAction.CLAIM_REPROCESSED, user_id=user.id, claim_id=claim_id, ip_address=ip
    )
    db.commit()

    finalize_claim_task.apply_async(args=[str(claim_id)], countdown=_FINALIZE_COUNTDOWN_SECONDS)
    return _claim_read(claim, service.members(claim_id))


@router.get("/claims", response_model=ClaimPage)
def search_claims(
    user: CurrentUser,
    db: DbDep,
    audit: AuditServiceDep,
    ip: Annotated[str | None, Depends(client_ip)],
    patient_name: Annotated[str | None, Query(max_length=100)] = None,
    run_number: Annotated[str | None, Query(max_length=32)] = None,
    dob_from: Annotated[date | None, Query()] = None,
    dob_to: Annotated[date | None, Query()] = None,
    status_filter: Annotated[ClaimStatus, Query(alias="status")] = ClaimStatus.COMPLETED,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor_created_at: Annotated[datetime | None, Query()] = None,
    cursor_id: Annotated[uuid.UUID | None, Query()] = None,
) -> ClaimPage:
    """Mirrors `documents.search_documents`: keyset-paged, defaults to
    `completed`, the search terms are PHI so only the result count is audited."""
    cursor = (
        (cursor_created_at, cursor_id)
        if cursor_created_at is not None and cursor_id is not None
        else None
    )
    rows = ClaimRepository(db).search(
        patient_name=patient_name,
        run_number=run_number,
        dob_from=dob_from,
        dob_to=dob_to,
        status=status_filter,
        limit=limit + 1,
        cursor=cursor,
    )
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = ClaimCursor(created_at=last.created_at, id=last.id)

    audit.record(
        action=AuditAction.CLAIM_VIEWED,
        user_id=user.id,
        ip_address=ip,
        detail={"resource": "search", "count": len(rows)},
    )
    return ClaimPage(items=list(rows), next_cursor=next_cursor)


@router.get("/claims/{claim_id}", response_model=ClaimRead)
def get_claim(
    claim_id: uuid.UUID,
    user: CurrentUser,
    service: ClaimServiceDep,
    audit: AuditServiceDep,
    ip: Annotated[str | None, Depends(client_ip)],
) -> ClaimRead:
    claim = service.get(claim_id)
    audit.record(action=AuditAction.CLAIM_VIEWED, user_id=user.id, claim_id=claim_id, ip_address=ip)
    return _claim_read(claim, service.members(claim_id))


@router.get("/claims/{claim_id}/extraction", response_model=ClaimExtractionRead)
def get_claim_extraction(
    claim_id: uuid.UUID,
    user: CurrentUser,
    service: ClaimServiceDep,
    audit: AuditServiceDep,
    ip: Annotated[str | None, Depends(client_ip)],
) -> ClaimExtractionRead:
    """The read-time claim-level merge (`app/services/claim_merge.py`). PHI --
    reading it is a disclosure, audited like every other extraction read."""
    merged = service.merged_extraction(claim_id)
    audit.record(
        action=AuditAction.CLAIM_VIEWED,
        user_id=user.id,
        claim_id=claim_id,
        ip_address=ip,
        detail={"resource": "extraction"},
    )
    return ClaimExtractionRead(
        fields={
            name: MergedFieldRead.model_validate(field, from_attributes=True)
            for name, field in merged.fields.items()
        },
        insurance=[
            MergedInsuranceSlotRead.model_validate(slot, from_attributes=True)
            for slot in merged.insurance
        ],
        conflicts=[
            FieldConflictRead(field=c.field, values_by_document=list(c.values_by_document))
            for c in merged.conflicts
        ],
    )


@router.get("/claims/{claim_id}/tags", response_model=ClaimTagsRead)
def get_claim_tags(
    claim_id: uuid.UUID,
    user: CurrentUser,
    service: ClaimServiceDep,
    audit: AuditServiceDep,
    ip: Annotated[str | None, Depends(client_ip)],
) -> ClaimTagsRead:
    """Assigned tags (with evidence -- PHI, audited) plus the manual-review
    prompts: `TAGS.docx` tags with no rule in `tag_rules.toml`, never
    auto-assigned, shown with their docx REASON so a reviewer can judge them
    by hand."""
    assigned, manual = service.tags(claim_id)
    registry = load_registry()
    audit.record(
        action=AuditAction.CLAIM_VIEWED,
        user_id=user.id,
        claim_id=claim_id,
        ip_address=ip,
        detail={"resource": "tags"},
    )
    return ClaimTagsRead(
        assigned=[
            ClaimTagRead(
                tag_name=tag.tag_name,
                source=tag.source,
                rule_id=tag.rule_id,
                confidence=tag.confidence,
                evidence=[EvidenceRead.model_validate(e) for e in tag.evidence],
                reason=(definition.reason if (definition := registry.by_key(tag.tag_name)) else ""),
            )
            for tag in assigned
            if tag.dismissed_at is None
        ],
        manual_review=[ManualReviewTagRead(tag_name=t.name, reason=t.reason) for t in manual],
    )


@router.post(
    "/claims/{claim_id}/tags", response_model=ClaimTagRead, status_code=status.HTTP_201_CREATED
)
def add_claim_tag(
    claim_id: uuid.UUID,
    body: AddTagRequest,
    user: CurrentUser,
    service: ClaimServiceDep,
    ip: Annotated[str | None, Depends(client_ip)],
) -> ClaimTagRead:
    """A reviewer's manual tag. 404 if `tag_name` isn't in the `TAGS.docx`
    registry, 409 if the claim already carries it."""
    tag = service.add_tag(claim_id, body.tag_name, user=user, ip_address=ip)
    definition = load_registry().by_key(tag.tag_name)
    return ClaimTagRead(
        tag_name=tag.tag_name,
        source=tag.source,
        rule_id=tag.rule_id,
        confidence=tag.confidence,
        evidence=[],
        reason=definition.reason if definition else "",
    )


@router.delete("/claims/{claim_id}/tags/{tag_name}", status_code=status.HTTP_204_NO_CONTENT)
def dismiss_claim_tag(
    claim_id: uuid.UUID,
    tag_name: str,
    user: CurrentUser,
    service: ClaimServiceDep,
    ip: Annotated[str | None, Depends(client_ip)],
) -> None:
    """A reviewer dismissing a tag. A `reviewer`-sourced tag is deleted
    outright; an `auto` tag is soft-dismissed so a later reprocess does not
    silently resurrect it -- see `ClaimRepository.dismiss_tag`."""
    service.dismiss_tag(claim_id, tag_name, user=user, ip_address=ip)


@router.get("/tags", response_model=list[TagRegistryEntryRead])
def list_tag_registry(user: CurrentUser) -> list[TagRegistryEntryRead]:
    """The `TAGS.docx`-derived registry itself: every tag name, its docx
    REASON, and whether `tag_rules.toml` can auto-assign it. No PHI, not
    audited (unlike the claim endpoints above) -- this is static reference
    data, the same tag list every claim is evaluated against. Still requires
    login, like every other route in this router."""
    registry = load_registry()
    return [
        TagRegistryEntryRead(name=tag.name, reason=tag.reason, has_rule=tag.rule is not None)
        for tag in registry.tags
    ]


def _claim_read(claim: Claim, members: list[tuple[uuid.UUID, int, DocumentStatus]]) -> ClaimRead:
    return ClaimRead.model_validate(
        {
            **claim.__dict__,
            "members": [
                ClaimMemberRead(document_id=doc_id, position=position, status=doc_status)
                for doc_id, position, doc_status in members
            ],
        }
    )
