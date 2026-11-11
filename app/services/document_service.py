"""Document ingestion. Depends on the `StorageBackend` protocol, never an adapter."""

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.crypto import CipherRing, DecryptionError
from app.core.exceptions import AppError, DocumentUnreadableError, NotFoundError
from app.core.logging import get_logger
from app.models.audit_log import AuditAction
from app.models.document import Document, DocumentStatus, DocumentType
from app.models.user import User
from app.ocr.base import OcrError
from app.ocr.rasterize import render_page
from app.repositories.document_repository import DocumentRepository
from app.schemas.document import DocumentCreated
from app.services import file_validation as validate
from app.services.audit_service import AuditService
from app.storage.base import StorageBackend, StorageError

logger = get_logger(__name__)

# The filename is encrypted under its own associated data, so a ciphertext lifted
# from this column cannot be replayed as a document body.
_FILENAME_AAD = b"document.original_filename"


class DocumentService:
    def __init__(
        self,
        *,
        session: Session,
        documents: DocumentRepository,
        storage: StorageBackend,
        cipher: CipherRing,
        audit: AuditService,
        settings: Settings,
    ) -> None:
        # The session is held only to hand to `AuditService.record_atomic`, so
        # that a success audit commits with the row it describes. All data access
        # still goes through the repository.
        self._session = session
        self._documents = documents
        self._storage = storage
        self._cipher = cipher
        self._audit = audit
        self._settings = settings

    def upload(
        self,
        *,
        data: bytes,
        filename: str,
        declared_content_type: str | None,
        doc_type: DocumentType,
        user: User,
        ip_address: str | None,
        user_agent: str | None,
    ) -> DocumentCreated:
        try:
            detected = validate.sniff_type(data[: validate.SNIFF_BYTES])
            validate.assert_declared_type_matches(declared_content_type, detected)
            page_count = (
                validate.count_pdf_pages(data) if detected is validate.DetectedType.PDF else 1
            )
            if page_count > self._settings.max_pdf_pages:
                raise validate.TooManyPagesError()
        except AppError as exc:
            # AppError only. A bare `except Exception` here would audit an
            # AttributeError as though the user had uploaded a bad file, and
            # return a rejection for what is actually our bug.
            #
            # Durable: this request is about to raise, and `get_db` will roll
            # back. Repeated malformed uploads from one account is a signal, and
            # it must not be discarded with the transaction. The filename is not
            # recorded -- it may carry a patient name.
            self._audit.record(
                action=AuditAction.DOCUMENT_UPLOAD_REJECTED,
                user_id=user.id,
                ip_address=ip_address,
                user_agent=user_agent,
                detail={"reason": type(exc).__name__},
            )
            raise

        digest = hashlib.sha256(data).hexdigest()
        if existing := self._documents.get_by_sha256(digest):
            if existing.status == DocumentStatus.FAILED:
                # Dedup means identical bytes never create a new row, so a FAILED
                # document has no other path back to PENDING -- it would otherwise
                # be stuck forever (same class of bug as the orphan reaper in
                # document_repository.py, but reaping never touches a terminal
                # status). Re-arm it and let the caller re-kick the pipeline.
                self._documents.claim(
                    existing.id, expected=DocumentStatus.FAILED, to=DocumentStatus.PENDING
                )
                # Same reprocess as the explicit endpoint -- audit it identically,
                # atomic with the commit below, so the trail shows every re-run,
                # not just endpoint-driven ones.
                AuditService.record_atomic(
                    self._session,
                    action=AuditAction.DOCUMENT_REPROCESSED,
                    user_id=user.id,
                    document_id=existing.id,
                    ip_address=ip_address,
                )
                self._session.commit()
                logger.info("document_reprocessing", document_id=str(existing.id))
                return DocumentCreated(
                    id=existing.id, status=DocumentStatus.PENDING, deduplicated=False
                )
            # Identical bytes already stored. Hand back the original rather than
            # encrypting a second copy under a second key.
            logger.info("document_deduplicated", document_id=str(existing.id))
            return DocumentCreated(id=existing.id, status=existing.status, deduplicated=True)

        storage_key = uuid.uuid4().hex
        document = Document(
            uploaded_by=user.id,
            doc_type=doc_type,
            original_filename_enc=self._cipher.encrypt(filename.encode("utf-8"), aad=_FILENAME_AAD),
            storage_key=storage_key,
            content_type=detected.value,
            size_bytes=len(data),
            page_count=page_count,
            sha256=digest,
            processing_started_at=datetime.now(UTC),
        )
        self._documents.add(document)

        # Row first, bytes second. If `put` raises, the request transaction rolls
        # back and no row survives. The reverse order would leave a row pointing
        # at a file that does not exist. An orphaned ciphertext is inert; a row
        # promising a document it cannot produce is a bug the auditor finds.
        self._storage.put(storage_key, data)

        # Atomic: this row references `document.id`, which is not committed yet.
        # An independent transaction cannot see it, and an audit entry that
        # survives a rolled-back upload claims something that never happened.
        AuditService.record_atomic(
            self._session,
            action=AuditAction.DOCUMENT_UPLOADED,
            user_id=user.id,
            document_id=document.id,
            ip_address=ip_address,
            user_agent=user_agent,
            detail={"doc_type": doc_type.value, "size_bytes": len(data), "pages": page_count},
        )
        logger.info(
            "document_uploaded",
            document_id=str(document.id),
            doc_type=doc_type.value,
            size_bytes=len(data),
        )

        # Commit here, not just via `get_db`'s post-yield commit. FastAPI's
        # request-scoped AsyncExitStack -- which holds `get_db`'s yield
        # dependency -- closes *after* the response is sent, and BackgroundTasks
        # run as part of sending that response. Left to `get_db`, the pipeline's
        # own session would open before this row is visible and its `claim()`
        # would find 0 matching rows, not a status mismatch -- exactly what
        # `pipeline_skipped_not_pending` logged for the row this is a fix for.
        self._session.commit()
        return DocumentCreated(id=document.id, status=document.status)

    def get(self, document_id: uuid.UUID) -> tuple[Document, str]:
        """Returns the row and its decrypted original filename."""
        document = self._documents.get_by_id(document_id)
        if document is None:
            raise NotFoundError("No such document.")
        try:
            filename = self._cipher.decrypt(document.original_filename_enc, aad=_FILENAME_AAD)
        except DecryptionError as exc:
            logger.error("filename_decryption_failed", document_id=str(document_id))
            raise DocumentUnreadableError() from exc
        return document, filename.decode("utf-8")

    def read_bytes(
        self,
        document_id: uuid.UUID,
        *,
        user: User,
        ip_address: str | None,
    ) -> tuple[bytes, str, str]:
        """Decrypt and return `(data, filename, content_type)`. Audited as a download."""
        document, filename = self.get(document_id)
        # Decrypt before auditing: a failed decrypt is not a download, and the
        # trail should not claim PHI was disclosed when it was not.
        try:
            data = self._storage.get(document.storage_key)
        except (DecryptionError, StorageError) as exc:
            raise DocumentUnreadableError() from exc
        AuditService.record_atomic(
            self._session,
            action=AuditAction.DOCUMENT_DOWNLOADED,
            user_id=user.id,
            document_id=document.id,
            ip_address=ip_address,
        )
        return data, filename, document.content_type

    def read_page_png(
        self,
        document_id: uuid.UUID,
        *,
        page: int,
        user: User,
        ip_address: str | None,
    ) -> bytes:
        """Render one 1-based page to PNG at the pipeline's OCR DPI, so the image's
        pixel space matches the stored OCR boxes. Rendering the scan is a PHI
        disclosure -- audited, like a view of the OCR text."""
        document, _ = self.get(document_id)
        if document.page_count is not None and not (1 <= page <= document.page_count):
            raise NotFoundError(
                f"Page {page} out of range; document has {document.page_count} page(s)."
            )
        try:
            data = self._storage.get(document.storage_key)
            png = render_page(
                data, document.content_type, page=page - 1, dpi=self._settings.ocr_dpi
            )
        except (DecryptionError, StorageError, OcrError) as exc:
            raise DocumentUnreadableError() from exc
        AuditService.record_atomic(
            self._session,
            action=AuditAction.DOCUMENT_VIEWED,
            user_id=user.id,
            document_id=document.id,
            ip_address=ip_address,
            detail={"resource": "page", "page": page},
        )
        return png
