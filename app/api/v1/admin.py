import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.deps import DbDep, client_ip, require_role
from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.models.audit_log import AuditAction
from app.models.document import Document
from app.models.user import User, UserRole
from app.repositories.document_repository import INTERRUPTED_REASON_PREFIX, DocumentRepository
from app.schemas.admin import (
    REPROCESS_BATCH_LIMIT,
    PipelineDocItem,
    PipelineOverview,
    ReprocessFailedRequest,
    ReprocessFailedResult,
)
from app.services.audit_service import AuditService
from app.worker import process_document_task

router = APIRouter(prefix="/admin", tags=["admin"])


def _item(doc: Document, email: str) -> PipelineDocItem:
    return PipelineDocItem(
        id=doc.id,
        status=doc.status,
        uploaded_by=doc.uploaded_by,
        uploader_email=email,
        created_at=doc.created_at,
        failure_reason=doc.failure_reason,
    )


@router.get("/pipeline", response_model=PipelineOverview)
def get_pipeline_overview(
    user: Annotated[User, Depends(require_role(UserRole.ADMIN))],
    db: DbDep,
) -> PipelineOverview:
    """Cross-user view of what is in flight and what has failed.

    Not audited: it is polled every few seconds by the admin masthead bar and
    discloses no patient PHI (ids, status, and staff email only) -- auditing
    every poll would drown the trail in no-PHI noise.
    """
    in_flight_rows, failed_rows, extraction_failed_rows = DocumentRepository(db).pipeline_overview(
        failed_limit=REPROCESS_BATCH_LIMIT
    )

    in_flight: list[PipelineDocItem] = [_item(doc, email) for doc, email in in_flight_rows]

    interrupted: list[PipelineDocItem] = []
    failed: list[PipelineDocItem] = []
    for doc, email in failed_rows:
        item = _item(doc, email)
        if doc.failure_reason and doc.failure_reason.startswith(INTERRUPTED_REASON_PREFIX):
            interrupted.append(item)
        else:
            failed.append(item)

    extraction_failed: list[PipelineDocItem] = [
        _item(doc, email) for doc, email in extraction_failed_rows
    ]

    return PipelineOverview(
        in_flight=in_flight,
        interrupted=interrupted,
        failed=failed,
        extraction_failed=extraction_failed,
    )


@router.post("/reprocess-failed", response_model=ReprocessFailedResult)
@limiter.limit(lambda: f"{get_settings().rate_limit_upload_per_minute}/minute")
def reprocess_failed(
    request: Request,
    body: ReprocessFailedRequest,
    user: Annotated[User, Depends(require_role(UserRole.ADMIN))],
    db: DbDep,
    ip: Annotated[str | None, Depends(client_ip)],
) -> ReprocessFailedResult:
    """Re-arm an admin-selected batch of FAILED documents and re-run each.

    Same claim (`claim_for_reprocess`) and audit (`record_atomic`) primitives as
    the single-document endpoint, looped -- one audit row per document, not one
    for the batch, so the trail stays document-scoped like every other action.
    A document already claimed by something else (or not FAILED/COMPLETED) is
    silently skipped, not an error: a bulk action over a set the admin picked
    from a live, polled list will sometimes race a state change.
    """
    repo = DocumentRepository(db)
    reprocessed: list[uuid.UUID] = []
    skipped: list[uuid.UUID] = []
    for document_id in body.document_ids:
        if repo.claim_for_reprocess(document_id):
            AuditService.record_atomic(
                db,
                action=AuditAction.DOCUMENT_REPROCESSED,
                user_id=user.id,
                document_id=document_id,
                ip_address=ip,
            )
            reprocessed.append(document_id)
        else:
            skipped.append(document_id)

    # One commit for the whole batch: every claim and its audit row land
    # together, or none do.
    db.commit()

    for document_id in reprocessed:
        process_document_task.delay(str(document_id))

    return ReprocessFailedResult(reprocessed=reprocessed, skipped=skipped)
