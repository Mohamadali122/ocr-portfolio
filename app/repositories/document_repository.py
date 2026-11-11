import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from sqlalchemy import ColumnElement, CursorResult, Update, func, or_, select, tuple_, update
from sqlalchemy.orm import Session

from app.models.document import Document, DocumentStatus, DocumentType
from app.models.ocr_result import OcrResult
from app.models.user import User

# A restart kills every in-process BackgroundTask. A *_RUNNING row at boot has a
# dead worker; a PENDING row's background task may never have fired at all (the
# response was sent but the task was dropped) -- and re-uploading identical bytes
# dedups onto it without re-kicking the pipeline, so it is stuck forever too.
# Reaping fails them (not retries -- that is the Phase 8 queue) so the poll ends.
_ORPHANABLE = (DocumentStatus.PENDING, DocumentStatus.OCR_RUNNING, DocumentStatus.AI_RUNNING)

# Shared with the admin pipeline view, which classifies a FAILED document as
# "was interrupted, not a real failure" vs. a genuine processing failure by
# matching this prefix -- keep the two in sync. Both reasons below share it:
# restart-time reaping and the periodic stale-document sweep are the same
# class of failure (nothing left running the pipeline for this row), just
# caught at different times.
INTERRUPTED_REASON_PREFIX = "processing interrupted"
RESTART_FAILURE_REASON = f"{INTERRUPTED_REASON_PREFIX} by application restart"
STALE_REAP_FAILURE_REASON = f"{INTERRUPTED_REASON_PREFIX}: no progress for over {{}}s"


class DocumentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _rowcount(self, stmt: Update) -> int:
        # An UPDATE always yields a CursorResult; Session.execute is typed as
        # the base Result, which the stubs say has no rowcount.
        return cast("CursorResult[Any]", self._session.execute(stmt)).rowcount

    def get_by_id(self, document_id: uuid.UUID) -> Document | None:
        return self._session.get(Document, document_id)

    def get_by_sha256(self, digest: str) -> Document | None:
        stmt = select(Document).where(Document.sha256 == digest)
        return self._session.execute(stmt).scalar_one_or_none()

    def add(self, document: Document) -> Document:
        self._session.add(document)
        self._session.flush()
        return document

    def search(
        self,
        *,
        patient_name: str | None = None,
        run_number: str | None = None,
        dob_from: date | None = None,
        dob_to: date | None = None,
        status: DocumentStatus | None = None,
        limit: int = 50,
        cursor: tuple[datetime, uuid.UUID] | None = None,
    ) -> list[tuple[Document, float | None]]:
        """Filtered, keyset-paginated document search over the denormalized
        identity columns. Ordered newest first; `cursor` is the (created_at, id)
        of the last row the caller already has -- a row-value comparison, not an
        OFFSET, so the page cost does not grow with how deep you are.

        LEFT JOINed to `OcrResult` for `mean_confidence` -- a review queue needs
        it to prioritize low-confidence documents, and `document_id` is unique on
        `OcrResult` (one row, replaced on re-run), so this is a 1:1 join, no
        fan-out. NULL for a document OCR hasn't finished yet.

        `status` is the review-eligibility gate: the router defaults it to
        `COMPLETED` so the review workflow never sees a pending, running,
        failed, or gate-failed document (`app/services/extraction_validation.py`)
        -- pass another status explicitly to look at those instead.
        """
        stmt = select(Document, OcrResult.mean_confidence).outerjoin(
            OcrResult, OcrResult.document_id == Document.id
        )
        if status is not None:
            stmt = stmt.where(Document.status == status)
        if patient_name:
            like = f"{patient_name}%"
            stmt = stmt.where(
                or_(
                    Document.patient_last_name.ilike(like),
                    Document.patient_first_name.ilike(like),
                )
            )
        if run_number:
            stmt = stmt.where(Document.run_number == run_number)
        if dob_from:
            stmt = stmt.where(Document.patient_dob >= dob_from)
        if dob_to:
            stmt = stmt.where(Document.patient_dob <= dob_to)
        if cursor is not None:
            # Row-value comparison: (created_at, id) strictly before the cursor.
            stmt = stmt.where(tuple_(Document.created_at, Document.id) < cursor)
        stmt = stmt.order_by(Document.created_at.desc(), Document.id.desc()).limit(limit)
        return [(row[0], row[1]) for row in self._session.execute(stmt).all()]

    def find_related(
        self,
        *,
        run_number: str | None,
        last_name: str | None,
        first_name: str | None,
        dob: date | None,
        exclude_id: uuid.UUID,
    ) -> list[Document]:
        """Other `COMPLETED` documents plausibly from the same encounter as
        `exclude_id`: same `run_number` if it has one, else an exact
        `(last_name, first_name, dob)` match. No fuzzy matching -- see
        `app/services/patient_grouping_service.py` for why.

        Group sizes here are a handful of documents per encounter, never a
        paged list, so this returns everything rather than keyset-paginating
        like `search`.
        """
        if run_number:
            match = Document.run_number == run_number
        elif last_name and first_name and dob:
            match = (
                (Document.patient_last_name == last_name)
                & (Document.patient_first_name == first_name)
                & (Document.patient_dob == dob)
            )
        else:
            return []
        stmt = (
            select(Document)
            .where(
                Document.status == DocumentStatus.COMPLETED,
                Document.id != exclude_id,
                match,
            )
            .order_by(Document.created_at.desc())
        )
        return list(self._session.execute(stmt).scalars())

    def claim(
        self, document_id: uuid.UUID, *, expected: DocumentStatus, to: DocumentStatus
    ) -> bool:
        """Atomically move a document from one status to another.

        Returns False if it was not in `expected` -- meaning another worker got
        there first, or the document is not in a state this transition is legal
        from. A read-then-write would let two BackgroundTasks both decide a
        PENDING document was theirs.
        """
        stmt = (
            update(Document)
            .where(Document.id == document_id, Document.status == expected)
            .values(status=to)
        )
        return self._rowcount(stmt) == 1

    def reclaim_for_resume(self, document_id: uuid.UUID) -> bool:
        """Re-arm a document for a Celery redelivery -- `task_acks_late` +
        `task_reject_on_worker_lost` (app/worker.py) redeliver the same task
        when the worker executing it dies mid-run (OOM kill, container crash),
        instead of losing it silently. Unlike `claim()`, accepts any
        non-terminal status, not just PENDING: the dead worker may have
        already committed PENDING->OCR_RUNNING or OCR_RUNNING->AI_RUNNING
        before it died. Always resets to OCR_RUNNING so the resumed run
        restarts the pipeline from OCR regardless of which stage was
        in-flight -- redoing OCR on an AI-stage crash is wasted compute, not a
        correctness problem (OcrRepository.add/ExtractionRepository.add both
        delete-then-insert on document_id, so a second write off a redelivered
        run replaces rather than collides with the dead run's partial rows).

        Returns False only if the document has already reached a terminal
        status by the time the redelivery lands (rare: fast-follow reap or
        reprocess got there first) -- the resumed task should treat that as a
        no-op, same as `claim()`'s own False case.
        """
        stmt = (
            update(Document)
            .where(Document.id == document_id, Document.status.in_(_ORPHANABLE))
            .values(
                status=DocumentStatus.OCR_RUNNING,
                processing_started_at=datetime.now(UTC),
            )
        )
        return self._rowcount(stmt) == 1

    def claim_for_reprocess(self, document_id: uuid.UUID) -> bool:
        """Re-arm a FAILED, EXTRACTION_FAILED, or COMPLETED document back to
        PENDING for a reviewer-requested re-run. Clears `failure_reason` and the
        completion timestamps so a poller doesn't see stale data from the prior
        run while status is PENDING.

        Returns False if the document is not currently in a terminal state (i.e.
        it's already mid-pipeline, or does not exist) -- same "atomic claim, no
        read-then-write race" reasoning as `claim`.
        """
        stmt = (
            update(Document)
            .where(
                Document.id == document_id,
                Document.status.in_(
                    (
                        DocumentStatus.FAILED,
                        DocumentStatus.EXTRACTION_FAILED,
                        DocumentStatus.COMPLETED,
                    )
                ),
            )
            .values(
                status=DocumentStatus.PENDING,
                failure_reason=None,
                ocr_completed_at=None,
                ai_completed_at=None,
                processing_started_at=datetime.now(UTC),
            )
        )
        return self._rowcount(stmt) == 1

    def rearm_if_failed(self, document_id: uuid.UUID) -> bool:
        """A claim adopted an existing document (sha256 dedup) that had already
        ended FAILED or EXTRACTION_FAILED -- typically reaped by an earlier
        interrupted run. Put it back to PENDING so the new claim's dispatch
        actually runs it. COMPLETED is deliberately excluded: re-running a good
        document discards reviewer corrections, and an explicit reprocess (which
        guards on those) is the only path that may do it."""
        stmt = (
            update(Document)
            .where(
                Document.id == document_id,
                Document.status.in_(
                    (DocumentStatus.FAILED, DocumentStatus.EXTRACTION_FAILED)
                ),
            )
            .values(
                status=DocumentStatus.PENDING,
                failure_reason=None,
                ocr_completed_at=None,
                ai_completed_at=None,
                processing_started_at=datetime.now(UTC),
            )
        )
        return self._rowcount(stmt) == 1

    def mark_reviewed_complete(self, document_id: uuid.UUID) -> bool:
        """A reviewer's explicit override: EXTRACTION_FAILED -> COMPLETED
        regardless of the completeness gate, after they've corrected whatever
        fields they judged necessary via `set_typed_value`. Guarded like
        `claim` -- returns False if the document is not currently
        EXTRACTION_FAILED (already reprocessed, already completed, or does
        not exist)."""
        stmt = (
            update(Document)
            .where(Document.id == document_id, Document.status == DocumentStatus.EXTRACTION_FAILED)
            .values(status=DocumentStatus.COMPLETED, failure_reason=None)
        )
        return self._rowcount(stmt) == 1

    def record_ocr_completed(self, document_id: uuid.UUID) -> None:
        """Stamp `ocr_completed_at` without changing status.

        OCR finishing is not terminal -- extraction runs next. The status
        transition to `AI_RUNNING` happens separately via `claim`, so a
        poller never observes a document that is simultaneously "OCR done"
        and "nothing left to do."
        """
        self._session.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(ocr_completed_at=datetime.now(UTC))
        )

    def mark_completed(self, document_id: uuid.UUID, *, ai_completed: bool = False) -> None:
        values: dict[str, object] = {"status": DocumentStatus.COMPLETED, "failure_reason": None}
        if ai_completed:
            values["ai_completed_at"] = datetime.now(UTC)
        self._session.execute(update(Document).where(Document.id == document_id).values(**values))

    def apply_patient_identity(
        self,
        document_id: uuid.UUID,
        *,
        last_name: str | None,
        first_name: str | None,
        dob: date | None,
        run_number: str | None,
    ) -> None:
        """Denormalize identity fields onto the document, purely to serve search.

        A data write, not a status transition -- kept separate from
        `mark_completed` for the same reason `claim` is separate from it.
        """
        self._session.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(
                patient_last_name=last_name,
                patient_first_name=first_name,
                patient_dob=dob,
                run_number=run_number,
            )
        )

    def mark_failed(self, document_id: uuid.UUID, *, reason: str) -> None:
        """`reason` is an operator-facing string. It must never contain PHI, so
        it carries an exception class name and a stage, never document content."""
        self._session.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(status=DocumentStatus.FAILED, failure_reason=reason[:500])
        )

    def mark_extraction_failed(self, document_id: uuid.UUID, *, reason: str) -> None:
        """The AI stage ran and produced fields, but they failed the
        completeness gate (`app/services/extraction_validation.py`) -- distinct
        from `mark_failed`, which is an OCR/LLM crash. `ai_completed_at` is
        stamped regardless: the stage did finish, it just had nothing a
        reviewer should see. `reason` is operator-facing, field *names* and a
        confidence number -- never a field value, same discipline as
        `mark_failed`."""
        self._session.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(
                status=DocumentStatus.EXTRACTION_FAILED,
                failure_reason=reason[:500],
                ai_completed_at=datetime.now(UTC),
            )
        )

    def set_doc_type(self, document_id: uuid.UUID, doc_type: DocumentType) -> None:
        """Persist `classification.classify()`'s guess when the upload didn't
        declare one. Only ever called when the stored type was `UNKNOWN` --
        never overwrites a caller-declared type."""
        self._session.execute(
            update(Document).where(Document.id == document_id).values(doc_type=doc_type)
        )

    def reap_orphaned(self) -> int:
        """Fail every document left mid-pipeline with nothing left to finish it.

        Called once at startup (`app/main.py::lifespan`). This is
        defense-in-depth, not the primary recovery path: `task_acks_late` +
        `task_reject_on_worker_lost` (app/worker.py) make Celery redeliver a
        task whose worker died mid-run, and `reclaim_for_resume` widens the
        status guard so that redelivery actually resumes the pipeline instead
        of no-op'ing against a non-PENDING status. What redelivery doesn't
        cover is a document stuck PENDING because `process_document_task.delay(...)`
        never reached the broker at all, or a task that was purged -- those
        leave a row with no worker ever coming for it, caught here or by
        `reap_stale`'s periodic sweep, whichever runs first.
        """
        stmt = (
            update(Document)
            .where(Document.status.in_(_ORPHANABLE))
            .values(status=DocumentStatus.FAILED, failure_reason=RESTART_FAILURE_REASON)
        )
        return self._rowcount(stmt)

    def reap_stale(self, *, older_than_seconds: int) -> int:
        """Periodic version of `reap_orphaned` -- same target statuses, but
        gated on processing epoch age instead of running unconditionally, so it's
        safe to call on a timer (`app.worker.reap_stale_documents_task`)
        without also failing documents that are still legitimately mid-pipeline.
        `processing_started_at` (falling back to `created_at` for rows that
        predate the column) is set every time a document (re-)enters processing.
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
        epoch = func.coalesce(Document.processing_started_at, Document.created_at)
        stmt = (
            update(Document)
            .where(Document.status.in_(_ORPHANABLE), epoch < cutoff)
            .values(
                status=DocumentStatus.FAILED,
                failure_reason=STALE_REAP_FAILURE_REASON.format(older_than_seconds),
            )
        )
        return self._rowcount(stmt)

    def pipeline_overview(
        self, *, failed_limit: int
    ) -> tuple[list[tuple[Document, str]], list[tuple[Document, str]], list[tuple[Document, str]]]:
        """Admin-only cross-user view: documents in flight, the most recent
        FAILED documents, and the most recent EXTRACTION_FAILED documents, each
        paired with the uploader's email.

        EXTRACTION_FAILED is returned separately from FAILED, not folded into
        it: one is a crash, the other is a gate miss on an extraction that ran
        fine -- conflating them in an ops view is exactly the ambiguity Phase 7
        exists to remove.

        In-flight is unbounded -- a healthy pilot never has enough of these to
        need paging. `failed_limit` caps each of the other two lists; the
        caller (the admin router) also uses it to cap bulk reprocess requests,
        so an admin can never select more than they can see.
        """

        def q(
            condition: ColumnElement[bool], *, newest_first: bool, limit: int | None = None
        ) -> list[tuple[Document, str]]:
            stmt = (
                select(Document, User.email)
                .join(User, User.id == Document.uploaded_by)
                .where(condition)
                .order_by(Document.created_at.desc() if newest_first else Document.created_at)
            )
            if limit is not None:
                stmt = stmt.limit(limit)
            return [(row[0], row[1]) for row in self._session.execute(stmt).all()]

        in_flight = q(Document.status.in_(_ORPHANABLE), newest_first=False)
        failed = q(Document.status == DocumentStatus.FAILED, newest_first=True, limit=failed_limit)
        extraction_failed = q(
            Document.status == DocumentStatus.EXTRACTION_FAILED,
            newest_first=True,
            limit=failed_limit,
        )
        return in_flight, failed, extraction_failed
