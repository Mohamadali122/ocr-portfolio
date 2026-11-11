import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update

from app.core.config import Settings
from app.core.crypto import get_cipher
from app.core.logging import configure_logging
from app.db.session import SessionLocal
from app.llm.base import LlmTransientError
from app.models.audit_log import AuditAction, AuditLog
from app.models.document import Document, DocumentStatus, DocumentType
from app.models.extraction import Extraction
from app.models.ocr_result import OcrResult
from app.models.user import User
from app.repositories.document_repository import INTERRUPTED_REASON_PREFIX, DocumentRepository
from app.services.audit_service import AuditService
from app.services.extraction_service import ExtractionService
from app.services.pipeline_service import PipelineService, reap_orphaned_documents
from app.storage.encrypted_local import EncryptedLocalStorage
from tests.factories import make_pdf
from tests.fakes import (
    ExplodingLlmProvider,
    ExplodingOcrEngine,
    FakeLlmProvider,
    FakeOcrEngine,
    TransientlyFailingLlmProvider,
)
from tests.integration.conftest import OwnerSessionLocal


@pytest.fixture
def storage(tmp_path):
    return EncryptedLocalStorage(root=tmp_path, cipher=get_cipher())


@pytest.fixture
def stored_document(admin: User, storage):
    """A real PENDING document with its bytes on disk."""
    data = make_pdf(2)
    storage_key = uuid.uuid4().hex
    storage.put(storage_key, data)

    with SessionLocal() as session:
        document = Document(
            uploaded_by=admin.id,
            doc_type=DocumentType.HFS,
            original_filename_enc=get_cipher().encrypt(b"f.pdf", aad=b"document.original_filename"),
            storage_key=storage_key,
            content_type="application/pdf",
            size_bytes=len(data),
            page_count=2,
            sha256=uuid.uuid4().hex * 2,
            status=DocumentStatus.PENDING,
        )
        session.add(document)
        session.commit()
        document_id = document.id

    yield document_id

    with OwnerSessionLocal() as session:
        session.execute(delete(AuditLog).where(AuditLog.document_id == document_id))
        session.commit()
    with SessionLocal() as session:
        session.execute(delete(Extraction).where(Extraction.document_id == document_id))
        session.execute(delete(OcrResult).where(OcrResult.document_id == document_id))
        session.execute(delete(Document).where(Document.id == document_id))
        session.commit()


def _pipeline(storage, engine, extraction: ExtractionService | None = None) -> PipelineService:
    return PipelineService(
        session_factory=SessionLocal,
        storage=storage,
        engine=engine,
        extraction=extraction or ExtractionService(FakeLlmProvider()),
        audit=AuditService(SessionLocal),
        dpi=72,  # fast; rendering fidelity is not what these tests assert
    )


def _status(document_id: uuid.UUID) -> DocumentStatus:
    with SessionLocal() as session:
        return session.get(Document, document_id).status


class TestHappyPath:
    def test_document_reaches_completed(self, storage, stored_document) -> None:
        # `stored_document` is HFS, which requires last_name/first_name/dob
        # (extraction_validation._REQUIRED_FIELDS) -- the default FakeOcrEngine
        # text and "{}" LLM response ground nothing, so a genuine happy path
        # needs a response that actually grounds against the OCR text.
        engine = FakeOcrEngine(text="DOE, JANE  DOB 04/17/1958")
        provider = FakeLlmProvider(
            response='{"last_name": {"value": "DOE"}, "first_name": {"value": "JANE"}, '
            '"dob": {"value": "04/17/1958"}}'
        )
        _pipeline(storage, engine, ExtractionService(provider)).process_document(stored_document)
        assert _status(stored_document) is DocumentStatus.COMPLETED

    def test_ocr_result_is_persisted_with_geometry(self, storage, stored_document) -> None:
        _pipeline(storage, FakeOcrEngine(text="DOE JANE")).process_document(stored_document)

        with SessionLocal() as session:
            result = session.execute(
                select(OcrResult).where(OcrResult.document_id == stored_document)
            ).scalar_one()

        assert result.page_count == 2
        assert "DOE JANE" in result.raw_text
        assert result.mean_confidence == pytest.approx(0.95)
        assert result.engine_version == "fake-ocr/1.0"
        # Boxes, not just text. Phase 5's provenance guard depends on these.
        assert result.page_texts[0]["lines"][0]["box"] == [[0, 0], [100, 0], [100, 20], [0, 20]]

    def test_ocr_completed_at_is_stamped(self, storage, stored_document) -> None:
        _pipeline(storage, FakeOcrEngine()).process_document(stored_document)
        with SessionLocal() as session:
            assert session.get(Document, stored_document).ocr_completed_at is not None

    def test_success_is_audited_without_the_recognised_text(self, storage, stored_document) -> None:
        _pipeline(storage, FakeOcrEngine(text="TESTERSON JANE")).process_document(
            stored_document
        )
        with SessionLocal() as session:
            row = session.execute(
                select(AuditLog)
                .where(AuditLog.document_id == stored_document)
                .where(AuditLog.action == AuditAction.OCR_COMPLETED.value)
            ).scalar_one()

        assert row.detail["pages"] == 2
        assert row.detail["mean_confidence"] == 0.95
        # The audit detail carries measurements. The text is PHI and stays out.
        assert "TESTERSON" not in str(row.detail)


class TestReprocessing:
    def test_re_running_the_pipeline_replaces_ocr_and_extraction_rows(
        self, storage, stored_document
    ) -> None:
        # A document that failed at the AI stage already has an OcrResult row
        # (document_id unique) by the time it's retried. The retry re-runs OCR
        # too, so the insert must replace that row, not collide with it.
        engine = FakeOcrEngine(text="DOE, JANE  DOB 04/17/1958  RUN 12345")
        provider = FakeLlmProvider(
            response=(
                '{"last_name": {"value": "DOE"}, "first_name": {"value": "JANE"}, '
                '"dob": {"value": "04/17/1958"}, "run_number": {"value": "12345"}}'
            )
        )
        pipeline = _pipeline(storage, engine, ExtractionService(provider))
        pipeline.process_document(stored_document)
        assert _status(stored_document) is DocumentStatus.COMPLETED

        with SessionLocal() as session:
            session.get(Document, stored_document).status = DocumentStatus.PENDING  # type: ignore[union-attr]
            session.commit()

        pipeline.process_document(stored_document)
        assert _status(stored_document) is DocumentStatus.COMPLETED

        with SessionLocal() as session:
            ocr_rows = (
                session.execute(select(OcrResult).where(OcrResult.document_id == stored_document))
                .scalars()
                .all()
            )
            extraction_rows = (
                session.execute(select(Extraction).where(Extraction.document_id == stored_document))
                .scalars()
                .all()
            )
        assert len(ocr_rows) == 1
        assert len(extraction_rows) == 1


class TestFailure:
    def test_engine_failure_marks_the_document_failed(self, storage, stored_document) -> None:
        # A background task has nobody to raise to. Every exit path must be terminal:
        # a document stuck in OCR_RUNNING is polled forever.
        _pipeline(storage, ExplodingOcrEngine()).process_document(stored_document)
        assert _status(stored_document) is DocumentStatus.FAILED

    def test_failure_reason_names_the_stage_not_the_content(self, storage, stored_document) -> None:
        _pipeline(storage, ExplodingOcrEngine()).process_document(stored_document)
        with SessionLocal() as session:
            reason = session.get(Document, stored_document).failure_reason
        assert reason == "ocr: OcrError"

    def test_failure_is_audited(self, storage, stored_document) -> None:
        _pipeline(storage, ExplodingOcrEngine()).process_document(stored_document)
        with SessionLocal() as session:
            actions = {
                row.action
                for row in session.execute(
                    select(AuditLog).where(AuditLog.document_id == stored_document)
                ).scalars()
            }
        assert AuditAction.OCR_FAILED.value in actions

    def test_a_missing_file_fails_rather_than_hangs(self, storage, stored_document) -> None:
        with SessionLocal() as session:
            key = session.get(Document, stored_document).storage_key
        storage.delete(key)

        _pipeline(storage, FakeOcrEngine()).process_document(stored_document)
        assert _status(stored_document) is DocumentStatus.FAILED


class TestTransientLlmFailure:
    """`LlmTransientError` (a dropped connection, LM Studio restarting) is the
    one exception `process_document` lets through rather than routing to
    FAILED -- see the docstring on `PipelineService.process_document`. Celery
    (`app.worker.process_document_task`) is what retries it; here we only
    verify the pipeline itself does not swallow it into a terminal status."""

    def test_transient_llm_failure_propagates_instead_of_marking_failed(
        self, storage, stored_document
    ) -> None:
        pipeline = _pipeline(
            storage, FakeOcrEngine(), ExtractionService(TransientlyFailingLlmProvider())
        )

        with pytest.raises(LlmTransientError):
            pipeline.process_document(stored_document)

        # OCR did complete; the document is still AI_RUNNING, not FAILED --
        # Celery's retry, not this call, decides the terminal outcome.
        assert _status(stored_document) is DocumentStatus.AI_RUNNING

    def test_fail_after_retries_exhausted_marks_the_document_failed(
        self, storage, stored_document
    ) -> None:
        pipeline = _pipeline(storage, FakeOcrEngine())
        pipeline.fail_after_retries_exhausted(stored_document, LlmTransientError("boom"))

        with SessionLocal() as session:
            document = session.get(Document, stored_document)
        assert document.status is DocumentStatus.FAILED
        assert document.failure_reason == "ai: LlmTransientError (retries exhausted)"

    def test_fail_after_retries_exhausted_is_audited_as_ai_failed(
        self, storage, stored_document
    ) -> None:
        pipeline = _pipeline(storage, FakeOcrEngine())
        pipeline.fail_after_retries_exhausted(stored_document, LlmTransientError("boom"))

        with SessionLocal() as session:
            actions = {
                row.action
                for row in session.execute(
                    select(AuditLog).where(AuditLog.document_id == stored_document)
                ).scalars()
            }
        assert AuditAction.AI_FAILED.value in actions


class TestAiExtraction:
    def test_extraction_result_is_persisted_and_grounded(self, storage, stored_document) -> None:
        engine = FakeOcrEngine(text="DOE, JANE  DOB 04/17/1958  RUN 12345")
        provider = FakeLlmProvider(
            response=(
                '{"last_name": {"value": "DOE"}, "first_name": {"value": "JANE"}, '
                '"dob": {"value": "04/17/1958"}, "run_number": {"value": "12345"}}'
            )
        )
        _pipeline(storage, engine, ExtractionService(provider)).process_document(stored_document)

        with SessionLocal() as session:
            extraction = session.execute(
                select(Extraction).where(Extraction.document_id == stored_document)
            ).scalar_one()
            document = session.get(Document, stored_document)

        assert extraction.fields["last_name"]["value"] == "DOE"
        assert extraction.fields["last_name"]["confidence"] == 1.0
        assert extraction.llm_model == "fake-llm/1.0"
        assert extraction.repaired is False
        assert document.patient_last_name == "DOE"
        assert document.patient_first_name == "JANE"
        assert document.patient_dob == date(1958, 4, 17)
        assert document.run_number == "12345"
        assert document.ai_completed_at is not None
        assert document.status is DocumentStatus.COMPLETED

    def test_epcr_format_dob_is_stored_as_a_real_date(self, storage, stored_document) -> None:
        # EPCR dates are YYYY/MM/DD. The denormalized patient_dob column must hold
        # the parsed date, and the typed extraction value must be MM/DD/YYYY.
        engine = FakeOcrEngine(text="DOE JANE  1958/04/17")
        provider = FakeLlmProvider(
            response='{"last_name": {"value": "DOE"}, "dob": {"value": "1958/04/17"}}'
        )
        _pipeline(storage, engine, ExtractionService(provider)).process_document(stored_document)

        with SessionLocal() as session:
            document = session.get(Document, stored_document)
            extraction = session.execute(
                select(Extraction).where(Extraction.document_id == stored_document)
            ).scalar_one()

        assert document.patient_dob == date(1958, 4, 17)
        assert extraction.fields["dob"]["typed_value"] == "04/17/1958"

    def test_epcr_document_gets_a_narrative_summary(self, storage, stored_document) -> None:
        # "Patient Care Report" classifies the document EPCR; the summary call
        # goes to the same fake provider, so the stored summary is its
        # response verbatim. classify() only runs when doc_type is UNKNOWN
        # (pipeline_service.py) -- stored_document defaults to HFS, so it
        # must be reset here or classification never fires.
        with SessionLocal() as session:
            session.get(Document, stored_document).doc_type = DocumentType.UNKNOWN
            session.commit()
        engine = FakeOcrEngine(text="Patient Care Report\nDOE JANE")
        provider = FakeLlmProvider(response='{"last_name": {"value": "DOE"}}')
        _pipeline(storage, engine, ExtractionService(provider)).process_document(stored_document)

        with SessionLocal() as session:
            document = session.get(Document, stored_document)
            extraction = session.execute(
                select(Extraction).where(Extraction.document_id == stored_document)
            ).scalar_one()

        assert document.doc_type is DocumentType.EPCR
        assert extraction.narrative_summary == '{"last_name": {"value": "DOE"}}'

    def test_non_epcr_document_has_no_narrative_summary(self, storage, stored_document) -> None:
        # stored_document defaults to HFS, not EPCR -- no narrative is ever
        # requested (pipeline_service.py), regardless of OCR content.
        engine = FakeOcrEngine(text="DOE JANE")
        provider = FakeLlmProvider(response='{"last_name": {"value": "DOE"}}')
        _pipeline(storage, engine, ExtractionService(provider)).process_document(stored_document)

        with SessionLocal() as session:
            extraction = session.execute(
                select(Extraction).where(Extraction.document_id == stored_document)
            ).scalar_one()

        assert extraction.narrative_summary is None
        # The LLM response never supplies a dob and the OCR text has no date,
        # so normalize_dob() falls back to the SOP default (normalization.py).
        assert extraction.fields["dob"]["source"] == "sop_default"

    def test_ai_completed_audit_carries_no_phi(self, storage, stored_document) -> None:
        # HFS requires last_name/first_name/dob to pass the gate and reach
        # AI_COMPLETED at all -- see test_document_reaches_completed.
        engine = FakeOcrEngine(text="TESTERSON JANE DOB 04/17/1958")
        provider = FakeLlmProvider(
            response='{"last_name": {"value": "TESTERSON"}, "first_name": {"value": "JANE"}, '
            '"dob": {"value": "04/17/1958"}}'
        )
        _pipeline(storage, engine, ExtractionService(provider)).process_document(stored_document)

        with SessionLocal() as session:
            row = session.execute(
                select(AuditLog)
                .where(AuditLog.document_id == stored_document)
                .where(AuditLog.action == AuditAction.AI_COMPLETED.value)
            ).scalar_one()

        assert "TESTERSON" not in str(row.detail)
        assert row.detail["repaired"] is False

    def test_extraction_failure_marks_the_document_failed(self, storage, stored_document) -> None:
        pipeline = _pipeline(storage, FakeOcrEngine(), ExtractionService(ExplodingLlmProvider()))
        pipeline.process_document(stored_document)
        assert _status(stored_document) is DocumentStatus.FAILED

    def test_extraction_failure_reason_names_the_stage_not_the_content(
        self, storage, stored_document
    ) -> None:
        pipeline = _pipeline(storage, FakeOcrEngine(), ExtractionService(ExplodingLlmProvider()))
        pipeline.process_document(stored_document)
        with SessionLocal() as session:
            reason = session.get(Document, stored_document).failure_reason
        assert reason == "ai: LlmError"

    def test_extraction_failure_is_audited_as_ai_failed(self, storage, stored_document) -> None:
        pipeline = _pipeline(storage, FakeOcrEngine(), ExtractionService(ExplodingLlmProvider()))
        pipeline.process_document(stored_document)
        with SessionLocal() as session:
            actions = {
                row.action
                for row in session.execute(
                    select(AuditLog).where(AuditLog.document_id == stored_document)
                ).scalars()
            }
        assert AuditAction.AI_FAILED.value in actions
        # OCR still succeeded and is still recorded -- only the AI stage failed.
        assert AuditAction.OCR_COMPLETED.value in actions


class TestExtractionGate:
    """An LLM response that validates cleanly and grounds to nothing must not
    reach COMPLETED -- the defect this effort exists to fix (see
    app/services/extraction_validation.py and sample/ANALYSIS.md)."""

    def test_empty_extraction_is_not_completed(self, storage, stored_document) -> None:
        # Default FakeOcrEngine + default FakeLlmProvider("{}") -- nothing
        # grounds, HFS's required last_name/first_name/dob stay unmet.
        _pipeline(storage, FakeOcrEngine()).process_document(stored_document)
        assert _status(stored_document) is DocumentStatus.EXTRACTION_FAILED

    def test_gate_failure_reason_names_fields_not_values(self, storage, stored_document) -> None:
        _pipeline(storage, FakeOcrEngine()).process_document(stored_document)
        with SessionLocal() as session:
            reason = session.get(Document, stored_document).failure_reason
        assert reason is not None
        # first_name has no typed_value at all -> in missing_fields. dob gets
        # the SOP default ("01/01/1900", normalization.py) so it's *not*
        # missing -- but that default carries confidence 0.0 forward, which
        # is exactly what sinks min_required_confidence to 0.00 here.
        assert "first_name" in reason
        assert "confidence=0.00" in reason
        # Field names, not page content -- the OCR text is never in the reason.
        assert "PATIENT NAME DOE JANE" not in reason

    def test_gate_failure_is_audited(self, storage, stored_document) -> None:
        _pipeline(storage, FakeOcrEngine()).process_document(stored_document)
        with SessionLocal() as session:
            row = session.execute(
                select(AuditLog)
                .where(AuditLog.document_id == stored_document)
                .where(AuditLog.action == AuditAction.EXTRACTION_VALIDATION_FAILED.value)
            ).scalar_one()
        assert row.detail["gate_passed"] is False
        assert "first_name" in row.detail["missing_fields"]

    def test_extraction_row_is_still_persisted_for_diagnostics(
        self, storage, stored_document
    ) -> None:
        # The gate hides a document from review; it does not throw the
        # extraction away -- a reprocess or an operator needs it.
        _pipeline(storage, FakeOcrEngine()).process_document(stored_document)
        with SessionLocal() as session:
            extraction = session.execute(
                select(Extraction).where(Extraction.document_id == stored_document)
            ).scalar_one()
        assert extraction is not None

    def test_extraction_failed_document_is_reprocessable(self, storage, stored_document) -> None:
        _pipeline(storage, FakeOcrEngine()).process_document(stored_document)
        with SessionLocal() as session:
            assert DocumentRepository(session).claim_for_reprocess(stored_document)

    def test_a_near_blank_document_fails_the_gate_even_with_one_grounded_field(
        self, storage, stored_document
    ) -> None:
        # sample/near_blank_booking_1.pdf: a real near-blank booking-summary print
        # with a page number and a URL but no patient data. A last name alone
        # (no first name, no dob) must still fail HFS's required set.
        engine = FakeOcrEngine(text="BookingSummary  DOE  page 1/1")
        provider = FakeLlmProvider(response='{"last_name": {"value": "DOE"}}')
        _pipeline(storage, engine, ExtractionService(provider)).process_document(stored_document)
        assert _status(stored_document) is DocumentStatus.EXTRACTION_FAILED


class TestGuardedTransition:
    def test_a_document_is_processed_only_once(self, storage, stored_document) -> None:
        engine = FakeOcrEngine()
        pipeline = _pipeline(storage, engine)

        pipeline.process_document(stored_document)
        # Second call: the document is COMPLETED, not PENDING. The guarded UPDATE
        # matches no rows, so the engine is never entered. A read-then-write would
        # have let two workers both claim it and race on the unique ocr_results row.
        pipeline.process_document(stored_document)

        assert engine.calls == 1

    def test_claim_returns_false_when_status_does_not_match(self, stored_document) -> None:
        with SessionLocal() as session:
            documents = DocumentRepository(session)
            assert documents.claim(
                stored_document, expected=DocumentStatus.PENDING, to=DocumentStatus.OCR_RUNNING
            )
            assert not documents.claim(
                stored_document, expected=DocumentStatus.PENDING, to=DocumentStatus.OCR_RUNNING
            )
            session.rollback()


class TestResumeAfterRedelivery:
    """`resume=True` (app.worker's redelivery detection, `reclaim_for_resume`
    in app/repositories/document_repository.py) is what a Celery redelivery
    uses after a worker died mid-task -- see app/worker.py's `task_acks_late`
    comment."""

    # `stored_document` is HFS, which requires last_name/first_name/dob
    # (extraction_validation._REQUIRED_FIELDS) -- same grounded engine/provider
    # pair TestHappyPath.test_document_reaches_completed uses, so these tests
    # actually reach COMPLETED instead of failing the completeness gate.
    def _grounded(self, storage) -> PipelineService:
        engine = FakeOcrEngine(text="DOE, JANE  DOB 04/17/1958")
        provider = FakeLlmProvider(
            response='{"last_name": {"value": "DOE"}, "first_name": {"value": "JANE"}, '
            '"dob": {"value": "04/17/1958"}}'
        )
        return _pipeline(storage, engine, ExtractionService(provider)), engine

    def test_resume_reclaims_a_document_stuck_mid_ocr(self, storage, stored_document) -> None:
        # Simulate a worker dying right after committing OCR_RUNNING.
        with SessionLocal() as session:
            DocumentRepository(session).claim(
                stored_document, expected=DocumentStatus.PENDING, to=DocumentStatus.OCR_RUNNING
            )
            session.commit()

        pipeline, engine = self._grounded(storage)
        pipeline.process_document(stored_document, resume=True)

        assert engine.calls == 1
        assert _status(stored_document) is DocumentStatus.COMPLETED

    def test_resume_reclaims_a_document_stuck_mid_ai(self, storage, stored_document) -> None:
        # Simulate a worker dying after OCR finished and AI_RUNNING committed.
        with SessionLocal() as session:
            documents = DocumentRepository(session)
            documents.claim(
                stored_document, expected=DocumentStatus.PENDING, to=DocumentStatus.OCR_RUNNING
            )
            documents.claim(
                stored_document, expected=DocumentStatus.OCR_RUNNING, to=DocumentStatus.AI_RUNNING
            )
            session.commit()

        pipeline, engine = self._grounded(storage)
        pipeline.process_document(stored_document, resume=True)

        # Redelivery restarts from OCR rather than resuming exactly where the
        # dead run left off -- wasted compute, not a correctness problem.
        assert engine.calls == 1
        assert _status(stored_document) is DocumentStatus.COMPLETED

    def test_resume_is_a_no_op_once_the_document_is_already_terminal(
        self, storage, stored_document
    ) -> None:
        pipeline, engine = self._grounded(storage)
        pipeline.process_document(stored_document)
        assert _status(stored_document) is DocumentStatus.COMPLETED

        # A redelivery landing after the document already finished (e.g. it
        # was slow to ack, not actually dead) must not reprocess it.
        pipeline.process_document(stored_document, resume=True)

        assert engine.calls == 1


class TestOrphanReaper:
    def test_running_documents_are_failed_at_startup(self, stored_document) -> None:
        # Simulate a process death mid-OCR.
        with SessionLocal() as session:
            DocumentRepository(session).claim(
                stored_document, expected=DocumentStatus.PENDING, to=DocumentStatus.OCR_RUNNING
            )
            session.commit()

        assert reap_orphaned_documents(SessionLocal) >= 1
        assert _status(stored_document) is DocumentStatus.FAILED

        with SessionLocal() as session:
            reason = session.get(Document, stored_document).failure_reason
        assert "restart" in reason

    def test_pending_documents_are_also_reaped(self, stored_document) -> None:
        # A PENDING row at startup has no in-flight request (the reaper runs once
        # at boot, before serving). Its BackgroundTask was dropped and will never
        # fire, and re-uploading identical bytes dedups onto it without re-kicking
        # the pipeline -- so it is stuck forever unless failed here.
        assert reap_orphaned_documents(SessionLocal) >= 1
        assert _status(stored_document) is DocumentStatus.FAILED

    def test_ai_running_documents_are_also_reaped(self, stored_document) -> None:
        # Simulate a process death mid-extraction, after OCR already finished.
        with SessionLocal() as session:
            documents = DocumentRepository(session)
            documents.claim(
                stored_document, expected=DocumentStatus.PENDING, to=DocumentStatus.OCR_RUNNING
            )
            documents.claim(
                stored_document, expected=DocumentStatus.OCR_RUNNING, to=DocumentStatus.AI_RUNNING
            )
            session.commit()

        assert reap_orphaned_documents(SessionLocal) >= 1
        assert _status(stored_document) is DocumentStatus.FAILED


class TestStaleReaper:
    """`DocumentRepository.reap_stale` -- the periodic backstop (app.worker's
    beat-scheduled `reap_stale_documents_task`) for what redelivery doesn't
    cover: a task that never reached the broker, or was purged."""

    def _age(self, document_id: uuid.UUID, *, seconds: int) -> None:
        with SessionLocal() as session:
            session.execute(
                update(Document)
                .where(Document.id == document_id)
                .values(created_at=datetime.now(UTC) - timedelta(seconds=seconds))
            )
            session.commit()

    def test_an_old_running_document_is_failed(self, stored_document) -> None:
        with SessionLocal() as session:
            DocumentRepository(session).claim(
                stored_document, expected=DocumentStatus.PENDING, to=DocumentStatus.OCR_RUNNING
            )
            session.commit()
        self._age(stored_document, seconds=120)

        with SessionLocal() as session:
            assert DocumentRepository(session).reap_stale(older_than_seconds=60) >= 1
            session.commit()

        assert _status(stored_document) is DocumentStatus.FAILED
        with SessionLocal() as session:
            reason = session.get(Document, stored_document).failure_reason
        assert reason.startswith(INTERRUPTED_REASON_PREFIX)

    def test_a_document_younger_than_the_threshold_is_left_alone(self, stored_document) -> None:
        with SessionLocal() as session:
            DocumentRepository(session).claim(
                stored_document, expected=DocumentStatus.PENDING, to=DocumentStatus.OCR_RUNNING
            )
            session.commit()

        with SessionLocal() as session:
            DocumentRepository(session).reap_stale(older_than_seconds=3600)
            session.commit()

        assert _status(stored_document) is DocumentStatus.OCR_RUNNING

    def test_a_completed_document_is_never_touched_regardless_of_age(
        self, storage, stored_document
    ) -> None:
        # Grounded engine/provider, same as TestHappyPath -- the default fake
        # response fails HFS's completeness gate and lands on EXTRACTION_FAILED,
        # not COMPLETED, which isn't what this test is about.
        engine = FakeOcrEngine(text="DOE, JANE  DOB 04/17/1958")
        provider = FakeLlmProvider(
            response='{"last_name": {"value": "DOE"}, "first_name": {"value": "JANE"}, '
            '"dob": {"value": "04/17/1958"}}'
        )
        _pipeline(storage, engine, ExtractionService(provider)).process_document(stored_document)
        self._age(stored_document, seconds=120)

        with SessionLocal() as session:
            DocumentRepository(session).reap_stale(older_than_seconds=60)
            session.commit()

        assert _status(stored_document) is DocumentStatus.COMPLETED


# A synthetic patient, distinct from the ones in TestHappyPath above so a
# passing assertion here cannot be explained by another test having already
# scrubbed the same string out of a shared log buffer.
_PHI_NAME = "HALVERSEN"
_PHI_DOB = "09/23/1971"
_PHI_RUN = "554433"
_PHI_SSN = "987-65-4321"
_PHI_TEXT = f"{_PHI_NAME}, PAT  DOB {_PHI_DOB}  RUN {_PHI_RUN}  SSN {_PHI_SSN}"


def _configure_logging_for_capsys(settings: Settings) -> None:
    # Must run inside the test body -- see tests/unit/test_phi_logging.py's
    # identical note. dictConfig binds `ext://sys.stdout` to a concrete stream
    # once; pytest swaps that stream between setup and call phases.
    configure_logging(settings.model_copy(update={"log_format": "json"}))


class _TwoStageLlmProvider:
    """Implements `LlmProvider`. First call fails validation, forcing the
    repair-retry path; second call succeeds. `FakeLlmProvider` always returns
    the same response, which cannot drive that path."""

    def __init__(self, *, bad_response: str, good_response: str) -> None:
        self._responses = [bad_response, good_response]
        self.calls = 0

    @property
    def version(self) -> str:
        return "fake-llm/1.0"

    def complete(self, *, system: str, user: str, temperature: float = 0.0) -> str:
        response = self._responses[self.calls]
        self.calls += 1
        return response


class TestPhiNeverReachesLogs:
    """The redaction chokepoint (app/core/logging.py) is unit-tested against
    synthetic log calls in tests/unit/test_phi_logging.py. This exercises the
    same guarantee end-to-end: real OCR text and a real LLM response, carried
    through the actual OCR -> extraction pipeline, must never surface in
    anything written to stdout -- success, failure, or a repair retry."""

    def test_success_path_leaves_no_phi_in_logs(
        self, storage, stored_document, settings: Settings, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _configure_logging_for_capsys(settings)
        engine = FakeOcrEngine(text=_PHI_TEXT)
        provider = FakeLlmProvider(
            response=f'{{"last_name": {{"value": "{_PHI_NAME}"}}, '
            f'"run_number": {{"value": "{_PHI_RUN}"}}}}'
        )
        _pipeline(storage, engine, ExtractionService(provider)).process_document(stored_document)

        out = capsys.readouterr().out
        for secret in (_PHI_NAME, _PHI_DOB, _PHI_RUN, _PHI_SSN):
            assert secret not in out

    def test_ocr_failure_leaves_no_phi_in_logs(
        self, storage, stored_document, settings: Settings, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _configure_logging_for_capsys(settings)
        # The pipeline logs only the exception's class name on failure
        # (pipeline_service.py: `reason = f"ocr: {type(exc).__name__}"`), never
        # its message -- this proves that holds even when the message itself
        # is PHI-shaped, not just that the field carrying it is absent.
        _pipeline(storage, ExplodingOcrEngine()).process_document(stored_document)

        out = capsys.readouterr().out
        assert _PHI_NAME not in out

    def test_extraction_repair_retry_leaves_no_phi_in_logs(
        self, storage, stored_document, settings: Settings, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _configure_logging_for_capsys(settings)
        engine = FakeOcrEngine(text=_PHI_TEXT)
        # First response fails schema validation and echoes into the repair
        # prompt built in ExtractionService._complete_and_validate; that
        # prompt is never logged, only `error_type=type(exc).__name__`.
        provider = _TwoStageLlmProvider(
            bad_response=f"not valid json, patient {_PHI_NAME}",
            good_response=f'{{"last_name": {{"value": "{_PHI_NAME}"}}}}',
        )
        _pipeline(storage, engine, ExtractionService(provider)).process_document(stored_document)

        out = capsys.readouterr().out
        assert _PHI_NAME not in out
