"""Document processing pipeline: OCR, then LLM extraction.

`process_document` takes a document id and nothing else -- no session, no
request, no ORM object. It opens its own session, does its work, and commits.
That is a plain Celery task signature (`app/worker.py::process_document_task`
wraps it, no redesign needed) precisely because it was written to not depend
on a request at all: `api/v1/documents.py` and `admin.py` used to fire it as a
FastAPI `BackgroundTask`; they now call `.delay()` on the Celery task instead.

Why it must own its session: whatever dispatches this (a request's
`BackgroundTask`, previously; a Celery worker process, now) runs independently
of the request's own session lifecycle. Touching a request-scoped session from
here raises `DetachedInstanceError` at best, and at worst commits into a
connection another request has already reclaimed.
"""

import time
import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.llm.base import LlmError, LlmTransientError
from app.models.audit_log import AuditAction
from app.models.document import DocumentStatus, DocumentType
from app.models.extraction import Extraction
from app.models.ocr_result import OcrResult
from app.ocr import pdf_text, rasterize
from app.ocr.base import OcrEngine, OcrError
from app.ocr.base import OcrResult as EngineOcrResult
from app.repositories.document_repository import DocumentRepository
from app.repositories.extraction_repository import ExtractionRepository
from app.repositories.ocr_repository import OcrRepository
from app.services import classification, extraction_validation
from app.services.audit_service import AuditService
from app.services.extraction_service import ExtractionService
from app.services.normalization import searchable_dob, searchable_name
from app.storage.base import StorageBackend

logger = get_logger(__name__)


class PipelineService:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        storage: StorageBackend,
        engine: OcrEngine,
        extraction: ExtractionService,
        audit: AuditService,
        dpi: int = rasterize.DEFAULT_DPI,
    ) -> None:
        self._session_factory = session_factory
        self._storage = storage
        self._engine = engine
        self._extraction = extraction
        self._audit = audit
        self._dpi = dpi

    def process_document(self, document_id: uuid.UUID, *, resume: bool = False) -> None:
        """Run the pipeline. Never raises, with one deliberate exception:
        `LlmTransientError` (LM Studio unreachable, connection dropped) is let
        through so `app.worker.process_document_task`'s bounded retry can
        retry the same document instead of it going straight to FAILED -- a
        transport blip, unlike truncation/validation failure, is worth
        retrying without redoing OCR.

        Every other exit path leaves the document in a terminal status. A
        document that stays PENDING or *_RUNNING forever (and never retries)
        is worse than one marked FAILED, because the client polls it until it
        gives up.

        `resume=True` means `app.worker.process_document_task` detected this
        call as a Celery redelivery -- the worker that had this document
        previously died mid-run (`task_acks_late` + `task_reject_on_worker_lost`).
        Widens the guard to `DocumentRepository.reclaim_for_resume`, which
        accepts any non-terminal status rather than just PENDING, since the
        dead run may have already committed past it.
        """
        with self._session_factory() as session:
            documents = DocumentRepository(session)

            # Guarded transition. If another worker already claimed it, stop.
            claimed = (
                documents.reclaim_for_resume(document_id)
                if resume
                else documents.claim(
                    document_id, expected=DocumentStatus.PENDING, to=DocumentStatus.OCR_RUNNING
                )
            )
            if not claimed:
                logger.info("pipeline_skipped_not_pending", document_id=str(document_id))
                return
            session.commit()  # publish OCR_RUNNING so a poller sees progress

        try:
            self._run_ocr(document_id)
        except Exception as exc:
            # The message may embed a filename or a row value. Record the class
            # name and the stage; the traceback goes to the (redacted) log.
            reason = f"ocr: {type(exc).__name__}"
            logger.error(
                "pipeline_failed", document_id=str(document_id), stage="ocr", exc_info=True
            )
            self._fail(document_id, reason, action=AuditAction.OCR_FAILED)
            return

        try:
            self._run_extraction(document_id)
        except LlmTransientError:
            # Let app.worker's bounded retry handle it -- the document stays
            # AI_RUNNING (still being worked on) until retries are exhausted,
            # at which point app.worker marks it FAILED itself.
            logger.warning(
                "pipeline_llm_transient_failure", document_id=str(document_id), stage="ai"
            )
            raise
        except Exception as exc:
            reason = f"ai: {type(exc).__name__}"
            logger.error("pipeline_failed", document_id=str(document_id), stage="ai", exc_info=True)
            self._fail(document_id, reason, action=AuditAction.AI_FAILED)

    # -- stages ------------------------------------------------------------

    def _run_ocr(self, document_id: uuid.UUID) -> None:
        started = time.perf_counter()

        with self._session_factory() as session:
            documents = DocumentRepository(session)
            document = documents.get_by_id(document_id)
            if document is None:
                raise OcrError("document vanished mid-pipeline")
            storage_key = document.storage_key
            content_type = document.content_type

        # Decrypt, then look for a real embedded text layer before touching the
        # engine at all: sample/ANALYSIS.md found most PDFs this pipeline sees
        # (every auto-generated ePCR, booking summaries, insurance eligibility)
        # are digital, not scanned, and OCR-ing exact text only adds noise and
        # ~23s/page. `None` means no layer, or too thin to trust -- fall through
        # to rasterize + recognize exactly as before. Outside any open
        # transaction either way: OCR of a 50-page scan takes minutes on CPU;
        # holding a database connection for its duration would exhaust the pool
        # under any concurrency at all.
        data = self._storage.get(storage_key)
        result: EngineOcrResult | None = None
        if content_type == "application/pdf":
            result = pdf_text.extract_text_layer(data, dpi=self._dpi)
        if result is None:
            page_images = rasterize.rasterize(data, content_type, dpi=self._dpi)
            result = self._engine.recognize(page_images)
        duration_ms = int((time.perf_counter() - started) * 1000)

        with self._session_factory() as session:
            OcrRepository(session).add(
                OcrResult(
                    document_id=document_id,
                    raw_text=result.raw_text,
                    page_texts=[
                        {"page": page.page_number, "lines": [line.as_dict() for line in page.lines]}
                        for page in result.pages
                    ],
                    mean_confidence=result.mean_confidence,
                    engine_version=result.engine_version,
                    page_count=len(result.pages),
                    duration_ms=duration_ms,
                )
            )
            # OCR finishing is not terminal -- extraction runs next. Stamp the
            # timestamp and advance the status separately, so a poller never
            # sees "OCR done" and "nothing left to do" at once.
            documents = DocumentRepository(session)
            documents.record_ocr_completed(document_id)
            documents.claim(
                document_id, expected=DocumentStatus.OCR_RUNNING, to=DocumentStatus.AI_RUNNING
            )
            AuditService.record_atomic(
                session,
                action=AuditAction.OCR_COMPLETED,
                document_id=document_id,
                detail={
                    "pages": len(result.pages),
                    "duration_ms": duration_ms,
                    # A number, not text. Confidence is not PHI; the text is.
                    "mean_confidence": round(result.mean_confidence, 4),
                    "engine": result.engine_version,
                },
            )
            session.commit()  # publish AI_RUNNING so a poller sees progress

        logger.info(
            "ocr_completed",
            document_id=str(document_id),
            pages=len(result.pages),
            duration_ms=duration_ms,
            mean_confidence=round(result.mean_confidence, 4),
            engine=result.engine_version,
        )

    def _run_extraction(self, document_id: uuid.UUID) -> None:
        started = time.perf_counter()

        with self._session_factory() as session:
            document = DocumentRepository(session).get_by_id(document_id)
            ocr_result = OcrRepository(session).get_by_document_id(document_id)
            if document is None or ocr_result is None:
                raise LlmError("document or OCR result vanished mid-pipeline")
            doc_type = document.doc_type
            raw_text = ocr_result.raw_text
            page_texts = ocr_result.page_texts

        # `doc_type` is a caller-supplied upload field that defaults to UNKNOWN
        # and is rarely set correctly in practice (sample/ANALYSIS.md) -- guess
        # from the recognized text before picking a prompt hint, so an UNKNOWN
        # document doesn't get the least specific one for no reason.
        classified_type = doc_type
        if doc_type is DocumentType.UNKNOWN:
            classified_type = classification.classify(raw_text)

        # Call the LLM outside any open transaction, same reasoning as OCR: a
        # slow request must not hold a pooled database connection.
        outcome = self._extraction.extract(classified_type, raw_text, page_texts)
        # EPCR only: that is where the crew narrative lives. Best-effort -- a
        # failed summary never fails the document.
        narrative_summary = (
            self._extraction.summarize_narrative(raw_text)
            if classified_type is DocumentType.EPCR
            else None
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        fields = outcome.fields

        # The completeness gate: an LLM response that validates cleanly and
        # grounds to nothing is not a successful extraction (see
        # app/services/extraction_validation.py). Only a document that passes
        # it is marked COMPLETED -- the status the review queue filters on.
        verdict = extraction_validation.evaluate(classified_type, fields)

        with self._session_factory() as session:
            ExtractionRepository(session).add(
                Extraction(
                    document_id=document_id,
                    fields=fields.model_dump(mode="json"),
                    llm_model=self._extraction.provider_version,
                    duration_ms=duration_ms,
                    repaired=outcome.repaired,
                    narrative_summary=narrative_summary,
                )
            )
            documents = DocumentRepository(session)
            if classified_type is not doc_type:
                documents.set_doc_type(document_id, classified_type)
            # Search columns get the typed (billing-system-shaped) identity: name split
            # with the middle dropped, run number as entered. `patient_dob` stores
            # the actually-parsed date, never the 01/01/1900 SOP default -- that
            # default is for data entry, not for polluting the DOB search index.
            # `searchable_dob` reads `typed_value` (already century-resolved by
            # `normalize_dob`), not the raw `value` -- see its docstring.
            documents.apply_patient_identity(
                document_id,
                last_name=searchable_name(fields.last_name.typed_value, fields.last_name.source),
                first_name=searchable_name(
                    fields.first_name.typed_value, fields.first_name.source
                ),
                dob=searchable_dob(fields.dob.typed_value, fields.dob.source),
                run_number=fields.run_number.typed_value,
            )
            if verdict.passed:
                documents.mark_completed(document_id, ai_completed=True)
                audit_action = AuditAction.AI_COMPLETED
            else:
                documents.mark_extraction_failed(
                    document_id,
                    reason=(
                        f"gate: missing={','.join(verdict.missing_fields) or 'none'} "
                        f"confidence={verdict.min_confidence:.2f}"
                    ),
                )
                audit_action = AuditAction.EXTRACTION_VALIDATION_FAILED
            AuditService.record_atomic(
                session,
                action=audit_action,
                document_id=document_id,
                detail={
                    "duration_ms": duration_ms,
                    "repaired": outcome.repaired,
                    "model": self._extraction.provider_version,
                    "doc_type": classified_type.value,
                    "gate_passed": verdict.passed,
                    "missing_fields": list(verdict.missing_fields),
                    "min_confidence": round(verdict.min_confidence, 4),
                },
            )
            session.commit()

        logger.info(
            "ai_completed",
            document_id=str(document_id),
            duration_ms=duration_ms,
            repaired=outcome.repaired,
            doc_type=classified_type.value,
            gate_passed=verdict.passed,
            missing_fields=list(verdict.missing_fields),
            min_confidence=round(verdict.min_confidence, 4),
        )

    def fail_after_retries_exhausted(self, document_id: uuid.UUID, exc: Exception) -> None:
        """Called by `app.worker.process_document_task` once its bounded retry
        of an `LlmTransientError` (see `process_document`'s docstring) has used
        up every attempt. Marks the document FAILED the same way any other
        AI-stage crash does -- a transient failure that never recovered is,
        from the reviewer's point of view, indistinguishable from one that
        never could have."""
        reason = f"ai: {type(exc).__name__} (retries exhausted)"
        logger.error("pipeline_llm_transient_retries_exhausted", document_id=str(document_id))
        self._fail(document_id, reason, action=AuditAction.AI_FAILED)

    def _fail(self, document_id: uuid.UUID, reason: str, *, action: AuditAction) -> None:
        try:
            with self._session_factory() as session:
                DocumentRepository(session).mark_failed(document_id, reason=reason)
                AuditService.record_atomic(
                    session,
                    action=action,
                    document_id=document_id,
                    detail={"reason": reason},
                )
                session.commit()
        except Exception:
            # Nothing left to do. If we cannot even record the failure, the
            # startup reaper will catch the row on the next boot.
            logger.error(
                "pipeline_failure_not_recorded", document_id=str(document_id), exc_info=True
            )


def reap_orphaned_documents(session_factory: Callable[[], Session]) -> int:
    """Startup hook. See `DocumentRepository.reap_orphaned`."""
    with session_factory() as session:
        count = DocumentRepository(session).reap_orphaned()
        session.commit()
    if count:
        logger.warning("orphaned_documents_failed", count=count)
    return count
