import uuid
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Path,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)

from app.api.deps import (
    AuditServiceDep,
    CurrentUser,
    DbDep,
    DocumentServiceDep,
    PatientGroupingServiceDep,
    client_ip,
)
from app.core.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError
from app.core.rate_limit import limiter
from app.models.audit_log import AuditAction
from app.models.document import DocumentStatus, DocumentType
from app.models.extraction import Extraction
from app.repositories.document_repository import DocumentRepository
from app.repositories.extraction_repository import ExtractionRepository
from app.repositories.ocr_repository import OcrRepository
from app.schemas.document import (
    DocumentCreated,
    DocumentCursor,
    DocumentListItem,
    DocumentPage,
    DocumentRead,
    ExtractionRead,
    FlagRead,
    MismatchFlagRead,
    OcrResultRead,
    RelatedDocumentsRead,
)
from app.schemas.extraction import (
    FIELD_NAMES,
    INSURANCE_FIELD_NAMES,
    MAX_INSURANCE_SLOTS,
    FieldCorrection,
)
from app.services import flags as flags_service
from app.services.audit_service import AuditService
from app.services.file_validation import FileTooLargeError
from app.worker import process_document_task

router = APIRouter(prefix="/documents", tags=["documents"])

_CHUNK = 1024 * 1024


async def read_capped(upload: UploadFile, limit: int) -> bytes:
    """Read at most `limit` bytes, then fail.

    `await upload.read()` with no argument would materialise the whole body
    first and check the size afterwards -- which is the denial-of-service, not
    the defence against it. Content-Length is a claim and cannot be trusted
    either.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(_CHUNK):
        total += len(chunk)
        if total > limit:
            raise FileTooLargeError()
        chunks.append(chunk)
    if total == 0:
        raise FileTooLargeError("The uploaded file is empty.")
    return b"".join(chunks)


@router.post("", response_model=DocumentCreated, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(lambda: f"{get_settings().rate_limit_upload_per_minute}/minute")
async def upload_document(
    user: CurrentUser,
    service: DocumentServiceDep,
    db: DbDep,
    request: Request,
    ip: Annotated[str | None, Depends(client_ip)],
    file: Annotated[UploadFile, File()],
    doc_type: Annotated[DocumentType, Form()] = DocumentType.UNKNOWN,
) -> DocumentCreated:
    """202: the bytes are stored, the pipeline has not run.

    Poll `GET /documents/{id}` until `status` leaves `pending`/`ocr_running`.
    OCR of a scanned page on CPU takes seconds; a blocking request would time out
    at any reverse proxy and hold a worker thread for the duration.
    """
    settings = get_settings()
    data = await read_capped(file, settings.max_upload_bytes)

    created = service.upload(
        data=data,
        filename=file.filename or "unnamed",
        declared_content_type=file.content_type,
        doc_type=doc_type,
        user=user,
        ip_address=ip,
        user_agent=request.headers.get("user-agent", "")[:255] or None,
    )

    documents = DocumentRepository(db)
    if not created.deduplicated:
        process_document_task.delay(str(created.id))
    elif documents.rearm_if_failed(created.id):
        db.commit()
        process_document_task.delay(str(created.id))

    return created


@router.post(
    "/{document_id}/reprocess", response_model=DocumentCreated, status_code=status.HTTP_202_ACCEPTED
)
@limiter.limit(lambda: f"{get_settings().rate_limit_upload_per_minute}/minute")
def reprocess_document(
    request: Request,
    document_id: uuid.UUID,
    user: CurrentUser,
    service: DocumentServiceDep,
    db: DbDep,
    ip: Annotated[str | None, Depends(client_ip)],
    force: Annotated[bool, Query()] = False,
) -> DocumentCreated:
    """Re-run OCR + extraction for a FAILED or COMPLETED document, reviewer-
    requested. Re-arms the row to PENDING and dispatches the same Celery task
    the upload path schedules -- same reasoning, same rate limit (this burns
    the same CPU/LLM cost as a fresh upload).

    `ExtractionRepository.add` deletes-then-inserts the whole `Extraction` row
    on every run, which would silently discard any reviewer correction along
    with it. Refuse by default when the current extraction has one -- enforced
    here, not just warned about client-side, because a stale browser tab or an
    impatient click cannot be relied on to see a confirmation dialog. `force`
    is the deliberate override for a reviewer who really does want a fresh
    AI pass despite losing their corrections.
    """
    service.get(document_id)  # 404s if the document does not exist

    if not force:
        extraction = ExtractionRepository(db).get_by_document_id(document_id)
        if extraction is not None:
            corrected = ExtractionRepository(db).count_corrected_fields(extraction)
            if corrected:
                raise ConflictError(
                    f"This document has {corrected} manually corrected field"
                    f"{'s' if corrected != 1 else ''} that reprocessing would discard. "
                    "Retry with force=true to override."
                )

    if not DocumentRepository(db).claim_for_reprocess(document_id):
        raise ConflictError("Document is already processing.")

    # Same transaction as the claim: an unauditable reprocess is not a success.
    AuditService.record_atomic(
        db,
        action=AuditAction.DOCUMENT_REPROCESSED,
        user_id=user.id,
        document_id=document_id,
        ip_address=ip,
    )
    # Publish PENDING before dispatching -- the worker opens its own session
    # and would not see an uncommitted claim.
    db.commit()

    process_document_task.delay(str(document_id))
    return DocumentCreated(id=document_id, status=DocumentStatus.PENDING)


@router.post("/{document_id}/mark-reviewed", response_model=DocumentCreated)
def mark_reviewed(
    document_id: uuid.UUID,
    user: CurrentUser,
    service: DocumentServiceDep,
    db: DbDep,
    ip: Annotated[str | None, Depends(client_ip)],
) -> DocumentCreated:
    """A reviewer's explicit override: EXTRACTION_FAILED -> COMPLETED, after
    correcting whatever fields (via `correct_extraction_field`) they judged
    necessary. Does not re-run the completeness gate -- the reviewer's
    judgment overrides the heuristic, rather than the system silently
    re-deciding.
    """
    service.get(document_id)  # 404s if the document does not exist
    if not DocumentRepository(db).mark_reviewed_complete(document_id):
        raise ConflictError("Document is not awaiting review.")

    # Same transaction as the transition: an unauditable override is not a
    # success.
    AuditService.record_atomic(
        db,
        action=AuditAction.EXTRACTION_MANUALLY_ACCEPTED,
        user_id=user.id,
        document_id=document_id,
        ip_address=ip,
    )
    db.commit()
    return DocumentCreated(id=document_id, status=DocumentStatus.COMPLETED)


@router.get("", response_model=DocumentPage)
def search_documents(
    user: CurrentUser,
    db: DbDep,
    audit: AuditServiceDep,
    ip: Annotated[str | None, Depends(client_ip)],
    patient_name: Annotated[str | None, Query(max_length=100)] = None,
    run_number: Annotated[str | None, Query(max_length=32)] = None,
    dob_from: Annotated[date | None, Query()] = None,
    dob_to: Annotated[date | None, Query()] = None,
    status: Annotated[DocumentStatus, Query()] = DocumentStatus.COMPLETED,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor_created_at: Annotated[datetime | None, Query()] = None,
    cursor_id: Annotated[uuid.UUID | None, Query()] = None,
) -> DocumentPage:
    """Search documents by the denormalized patient identity, run number, or DOB.

    Defaults to `status=completed`: a document that is still pending, still
    running, crashed, or failed the extraction completeness gate
    (`app/services/extraction_validation.py`) is not something a reviewer
    should see here. Pass `status` explicitly to look at another state --
    the admin pipeline view (`GET /admin/pipeline`) is the cross-user
    equivalent for in-flight and failed documents.

    The query string carries a patient name, which is PHI -- it is why the access
    log is path-only (`RequestContextMiddleware`). A page that names patients is a
    disclosure, so it is audited; the audit `detail` records the count only, never
    the search terms.
    """
    cursor = (
        (cursor_created_at, cursor_id)
        if cursor_created_at is not None and cursor_id is not None
        else None
    )
    rows = DocumentRepository(db).search(
        patient_name=patient_name,
        run_number=run_number,
        dob_from=dob_from,
        dob_to=dob_to,
        status=status,
        limit=limit + 1,  # one extra row tells us whether a next page exists
        cursor=cursor,
    )

    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last_doc, _ = rows[-1]
        next_cursor = DocumentCursor(created_at=last_doc.created_at, id=last_doc.id)

    audit.record(
        action=AuditAction.DOCUMENT_VIEWED,
        user_id=user.id,
        ip_address=ip,
        detail={"resource": "search", "count": len(rows)},
    )
    items = []
    for doc, mean_confidence in rows:
        item = DocumentListItem.model_validate(doc)
        item.mean_confidence = mean_confidence
        items.append(item)
    return DocumentPage(items=items, next_cursor=next_cursor)


@router.get("/{document_id}", response_model=DocumentRead)
def get_document(
    document_id: uuid.UUID,
    user: CurrentUser,
    service: DocumentServiceDep,
    audit: AuditServiceDep,
    ip: Annotated[str | None, Depends(client_ip)],
) -> DocumentRead:
    document, filename = service.get(document_id)
    audit.record(
        action=AuditAction.DOCUMENT_VIEWED,
        user_id=user.id,
        document_id=document.id,
        ip_address=ip,
    )
    return DocumentRead.model_validate({**document.__dict__, "original_filename": filename})


@router.get("/{document_id}/ocr", response_model=OcrResultRead)
def get_ocr_result(
    document_id: uuid.UUID,
    user: CurrentUser,
    service: DocumentServiceDep,
    db: DbDep,
    audit: AuditServiceDep,
    ip: Annotated[str | None, Depends(client_ip)],
) -> OcrResultRead:
    """The recognised text and its per-line geometry.

    404 while the document is still pending or running: there is no result yet,
    and inventing an empty one would let a client mistake "not finished" for
    "nothing on the page".
    """
    document, _ = service.get(document_id)  # 404s if the document does not exist

    result = OcrRepository(db).get_by_document_id(document_id)
    if result is None:
        raise NotFoundError(f"No OCR result yet; document status is {document.status.value}.")

    # Reading OCR text is reading PHI. It is a disclosure, and it is audited.
    audit.record(
        action=AuditAction.DOCUMENT_VIEWED,
        user_id=user.id,
        document_id=document_id,
        ip_address=ip,
        detail={"resource": "ocr_result"},
    )
    return OcrResultRead.model_validate(result)


@router.get("/{document_id}/extraction", response_model=ExtractionRead)
def get_extraction(
    document_id: uuid.UUID,
    user: CurrentUser,
    service: DocumentServiceDep,
    db: DbDep,
    audit: AuditServiceDep,
    ip: Annotated[str | None, Depends(client_ip)],
) -> ExtractionRead:
    """The grounded field extraction. 404 until the AI stage has completed --
    same reasoning as `/ocr`: an empty result would read as "nothing found"
    rather than "not finished yet"."""
    document, _ = service.get(document_id)  # 404s if the document does not exist

    extraction = ExtractionRepository(db).get_by_document_id(document_id)
    if extraction is None:
        raise NotFoundError(f"No extraction yet; document status is {document.status.value}.")

    # Reading extracted fields is reading PHI. It is a disclosure, and it is audited.
    audit.record(
        action=AuditAction.DOCUMENT_VIEWED,
        user_id=user.id,
        document_id=document_id,
        ip_address=ip,
        detail={"resource": "extraction"},
    )
    return _extraction_read(extraction)


@router.get("/{document_id}/related", response_model=RelatedDocumentsRead)
def get_related_documents(
    document_id: uuid.UUID,
    user: CurrentUser,
    service: DocumentServiceDep,
    grouping: PatientGroupingServiceDep,
    audit: AuditServiceDep,
    ip: Annotated[str | None, Depends(client_ip)],
) -> RelatedDocumentsRead:
    """Other `COMPLETED` documents plausibly from the same encounter (same
    `run_number`, or the same name+DOB when neither has one), and any
    denormalized identity field that disagrees between them -- e.g. two
    documents sharing a run number but disagreeing on date of birth. Computed
    at read time; nothing about this grouping is persisted (see
    `app/services/patient_grouping_service.py` -- there is deliberately no
    `patients` table this could write a decision into).
    """
    anchor, _ = service.get(document_id)  # 404s if the document does not exist
    result = grouping.find_related(anchor)

    # Not a new disclosure: a caller who can view the anchor can already view
    # each related document individually. Still audited, count only, like
    # every other resource this router discloses.
    audit.record(
        action=AuditAction.DOCUMENT_VIEWED,
        user_id=user.id,
        document_id=document_id,
        ip_address=ip,
        detail={"resource": "related", "count": len(result.related)},
    )
    return RelatedDocumentsRead(
        related=[DocumentListItem.model_validate(doc) for doc in result.related],
        mismatches=[
            MismatchFlagRead(field=flag.field, values=list(flag.values))
            for flag in result.mismatches
        ],
    )


@router.patch("/{document_id}/extraction/fields/{field_name}", response_model=ExtractionRead)
def correct_extraction_field(
    document_id: uuid.UUID,
    field_name: str,
    body: FieldCorrection,
    user: CurrentUser,
    service: DocumentServiceDep,
    db: DbDep,
    ip: Annotated[str | None, Depends(client_ip)],
) -> ExtractionRead:
    """A reviewer overwriting one field's typed value. Marks the field
    `corrected` so a human edit is never mistaken for a page reading.

    `field_name` is either a top-level field (`last_name`) or an insurance-slot
    path (`insurance.0.policy_id`, index `0..MAX_INSURANCE_SLOTS-1`)."""
    service.get(document_id)  # 404s if document is missing; filename unneeded here
    if not _is_correctable_field(field_name):
        raise NotFoundError("Unknown field.")

    repo = ExtractionRepository(db)
    extraction = repo.get_by_document_id(document_id)
    if extraction is None:
        raise NotFoundError("No extraction yet.")
    if not _field_path_exists(extraction.fields, field_name):
        raise NotFoundError("Unknown field.")
    repo.set_typed_value(extraction, field_name, body.typed_value)

    # Same transaction as the write: an unauditable correction is not a
    # success. `detail` carries the field name only -- never the value, PHI.
    AuditService.record_atomic(
        db,
        action=AuditAction.EXTRACTION_FIELD_CORRECTED,
        user_id=user.id,
        document_id=document_id,
        ip_address=ip,
        detail={"field": field_name},
    )
    return _extraction_read(extraction)


@router.get("/{document_id}/page/{page}")
def get_document_page(
    document_id: uuid.UUID,
    user: CurrentUser,
    service: DocumentServiceDep,
    ip: Annotated[str | None, Depends(client_ip)],
    page: Annotated[int, Path(ge=1)],
) -> Response:
    """A single page rendered to PNG, for the review UI's bbox overlay.

    ponytail: rendered at `settings.ocr_dpi`, the same DPI the pipeline rasterized
    at, so the PNG's pixel space matches the stored OCR boxes exactly. Render at
    any other DPI and the overlay boxes drift off the text.
    """
    png = service.read_page_png(document_id, page=page, user=user, ip_address=ip)
    return Response(
        content=png,
        media_type="image/png",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
    )


@router.get("/{document_id}/file")
def download_document(
    document_id: uuid.UUID,
    user: CurrentUser,
    service: DocumentServiceDep,
    ip: Annotated[str | None, Depends(client_ip)],
) -> Response:
    data, filename, content_type = service.read_bytes(document_id, user=user, ip_address=ip)
    return Response(
        content=data,
        media_type=content_type,
        headers={
            # `attachment` so a PDF or image is never rendered inline in the
            # browser's origin. filename* is RFC 5987; the name may be non-ASCII.
            "Content-Disposition": f'attachment; filename="{_ascii_fallback(filename)}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


def _extraction_read(extraction: Extraction) -> ExtractionRead:
    """Attach the read-time advisory flags (`app/services/flags.py`) to the
    serialized extraction. Old stored rows lacking newer schema keys validate
    fine -- missing fields take their defaults."""
    read = ExtractionRead.model_validate(extraction)
    read.suggested_flags = [
        FlagRead(code=flag.code, detail=flag.detail) for flag in flags_service.suggest(read.fields)
    ]
    return read


def _is_correctable_field(field_name: str) -> bool:
    if field_name in FIELD_NAMES:
        return True
    parts = field_name.split(".")
    if len(parts) != 3 or parts[0] != "insurance":
        return False
    _, index_str, subfield = parts
    return (
        index_str.isdigit()
        and int(index_str) < MAX_INSURANCE_SLOTS
        and subfield in INSURANCE_FIELD_NAMES
    )


def _field_path_exists(fields: dict[str, Any], field_name: str) -> bool:
    """`_is_correctable_field` only checks the path is *shaped* right;
    an `insurance.N.*` path also needs slot `N` to actually be present on
    this extraction -- a document with one insurance slot has no `[1]`."""
    if "." not in field_name:
        return True
    _, index_str, _ = field_name.split(".")
    return int(index_str) < len(fields.get("insurance", []))


def _ascii_fallback(filename: str) -> str:
    # Control characters (CR/LF especially) survive an ascii encode and would
    # make h11 refuse the Content-Disposition header -- a 500 on download.
    safe = filename.encode("ascii", "ignore").decode().replace('"', "")
    safe = "".join(c for c in safe if " " <= c < "\x7f")
    return safe or "document"
