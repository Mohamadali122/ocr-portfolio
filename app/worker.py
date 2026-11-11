"""Celery entrypoint. The pipeline's task-signature seam
(`app/services/pipeline_service.py`) actually gets a broker here.

This is the second (and only other) place a concrete adapter gets bound to a
service -- but it does so by importing the *binder functions* from
`app.api.deps` (`get_ocr_engine`, `get_llm_provider`, `get_extraction_service`)
rather than the concrete adapter classes directly, so `app/api/deps.py` stays
the only module that names them. `app.worker` sits outside every
import-linter `source_modules` list in `pyproject.toml`
(`app.services`/`models`/`repositories`/`storage`/`ocr`/`llm`), so importing
`app.api.deps` from here is contract-clean.

Run with: celery -A app.worker worker --loglevel=info --concurrency=1
"""

import uuid

from celery import Celery, Task

from app.api.deps import get_extraction_service, get_llm_provider, get_ocr_engine, get_storage
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.llm.base import LlmTransientError
from app.repositories.document_repository import DocumentRepository
from app.services.audit_service import AuditService
from app.services.claim_service import ClaimFinalizer
from app.services.pipeline_service import PipelineService
from app.services.tag_registry import load_registry

logger = get_logger(__name__)

celery_app = Celery("local-doc-ocr", broker=get_settings().redis_url)
# Document.status is already the single source of truth for a task's outcome
# (every poller already reads it) -- a Celery result backend would be a
# second, redundant status channel.
# ponytail: no result backend; add one only if something needs Celery's own
# retry/result introspection, not just "did the document finish".
celery_app.conf.task_ignore_result = True

# Late ack + redeliver-on-worker-loss: without these, the broker considers a
# task done the instant a worker picks it up, not when it finishes. A worker
# process killed mid-task (OOM, container crash) then loses the task
# silently -- no retry, nothing marks the document FAILED, it sits
# OCR_RUNNING/AI_RUNNING forever. With these set, a dead worker's task goes
# back on the queue for another worker to pick up; `process_document_task`
# below detects that redelivery and resumes via `reclaim_for_resume`
# (app/repositories/document_repository.py) instead of no-op'ing.
celery_app.conf.task_acks_late = True
celery_app.conf.task_reject_on_worker_lost = True
# How long an unacked task sits before the Redis transport assumes the
# worker died and redelivers it. Must clear worst-case single-document
# processing time with real margin -- if a task that's still genuinely
# running (alive, just slow) ever exceeds this, it gets redelivered to a
# second worker and double-processed while the first is still working.
celery_app.conf.broker_transport_options = {
    "visibility_timeout": get_settings().celery_broker_visibility_timeout_seconds
}


def _pipeline_service() -> PipelineService:
    settings = get_settings()
    return PipelineService(
        session_factory=SessionLocal,
        storage=get_storage(settings),
        engine=get_ocr_engine(),
        extraction=get_extraction_service(settings, get_llm_provider()),
        audit=AuditService(SessionLocal),
        dpi=settings.ocr_dpi,
    )


def _claim_finalizer() -> ClaimFinalizer:
    settings = get_settings()
    return ClaimFinalizer(
        session_factory=SessionLocal,
        registry=load_registry(),
        finalize_timeout_seconds=settings.claim_finalize_timeout_seconds,
    )


# Bounded retry for LlmTransientError only -- a dropped connection or LM
# Studio restarting is worth retrying without redoing OCR. Truncation/
# validation failures raise plain LlmError, never reach here, and go
# straight to FAILED as before: retrying the same deterministic
# (temperature=0.0) prompt would only reproduce them.
_LLM_TRANSIENT_MAX_RETRIES = 3


@celery_app.task(  # type: ignore[untyped-decorator]  # celery ships no stubs
    name="process_document", bind=True, max_retries=_LLM_TRANSIENT_MAX_RETRIES
)
def process_document_task(self: Task, document_id: str) -> None:
    # `delivery_info.get("redelivered")` is set by the broker (not by
    # `self.retry()`) when this exact message is being handed to a worker for
    # a second time -- the signature `task_reject_on_worker_lost` above
    # produces. A genuinely fresh dispatch never has this set.
    # `delivery_info` is `None` (not `{}`) for a task run outside a real
    # worker/broker context (`.run()` in tests) -- only set to a dict once
    # Celery actually dispatches the task through a worker.
    #
    # `self.request.retries > 0` covers the other resumed case: a
    # `self.retry()` below (LlmTransientError) is a fresh task dispatch, not a
    # broker redelivery, so `redelivered` is never set for it -- without this
    # check, `resume` would be False on that retry, `process_document` would
    # then only accept a PENDING document (`claim(expected=PENDING, ...)`),
    # and since the first attempt already advanced the document to
    # OCR_RUNNING/AI_RUNNING before the LLM call failed, that guard would
    # reject every retry as "already claimed" (`pipeline_skipped_not_pending`)
    # and return without ever calling the LLM again -- silently orphaning the
    # document at AI_RUNNING forever, with none of `_LLM_TRANSIENT_MAX_RETRIES`
    # actually retrying anything. `resume=True` here re-arms via
    # `reclaim_for_resume`, which accepts any non-terminal status.
    delivery_info = self.request.delivery_info or {}
    resumed = bool(delivery_info.get("redelivered")) or self.request.retries > 0
    logger.info("celery_task_started", document_id=document_id, resumed=resumed)
    try:
        _pipeline_service().process_document(uuid.UUID(document_id), resume=resumed)
    except LlmTransientError as exc:
        if self.request.retries >= self.max_retries:
            _pipeline_service().fail_after_retries_exhausted(uuid.UUID(document_id), exc)
            return
        # Exponential backoff capped at 60s -- LM Studio restarts take seconds,
        # not minutes. `self.retry()` always raises (Retry, to the broker; or
        # `exc` itself if `max_retries` were exceeded here, which the check
        # above already prevents).
        raise self.retry(exc=exc, countdown=min(2**self.request.retries * 5, 60)) from exc


# Poll-based join, not a Celery chord: a chord needs a result backend, and
# `task_ignore_result = True` above is deliberate (Document.status is already
# the single source of truth for a task's outcome -- see that setting's own
# comment). `max_retries=None` -- ClaimFinalizer.try_finalize's own deadline
# (settings.claim_finalize_timeout_seconds, measured from Claim.created_at)
# is what actually bounds this, not Celery's retry count.
# ponytail: poll-based join, not a chord. Switch to a chord if claim finalize
# latency ever matters more than not running a second status channel.
_FINALIZE_RETRY_COUNTDOWN_SECONDS = 15


@celery_app.task(name="finalize_claim", bind=True, max_retries=None)  # type: ignore[untyped-decorator]
def finalize_claim_task(self: Task, claim_id: str) -> None:
    if not _claim_finalizer().try_finalize(uuid.UUID(claim_id)):
        # throw=False: `self.retry()` still re-publishes the task to the
        # broker with the countdown -- that's the actual scheduling, and it
        # happens regardless of `throw`. What `throw` controls is whether a
        # `Retry` exception also propagates out of *this* call. In a real
        # (non-eager) worker that never matters -- `apply_async` from the
        # dispatching router never runs this body in the caller's process at
        # all. It only matters under `task_always_eager` (tests): there,
        # `finalize_claim_task.apply_async(...)` executes synchronously
        # inside `POST /claims`, and `throw=True` (the default) would raise
        # the `Retry` straight out of that request. `Document.status`/
        # `Claim.status` are the only source of truth this system reads
        # (`task_ignore_result = True`, above) -- Celery's own per-attempt
        # RETRY/SUCCESS bookkeeping is not, so suppressing it costs nothing.
        self.retry(countdown=_FINALIZE_RETRY_COUNTDOWN_SECONDS, throw=False)


# `DocumentRepository.reap_stale`'s periodic backstop: catches what
# redelivery doesn't (a task that never reached the broker, or was purged).
# A real `beat_schedule` entry, not a self-rescheduling task like
# `finalize_claim_task` above -- this one has no natural end, and this
# deployment's `docker compose restart worker` habit would pile up a new
# infinite self-rescheduling chain in Redis on every restart. `celery beat`
# (run as its own container, see docker-compose.yml) owns the interval
# instead; restarting it just resumes the same schedule, nothing piles up.
@celery_app.task(name="reap_stale_documents")  # type: ignore[untyped-decorator]
def reap_stale_documents_task() -> None:
    settings = get_settings()
    with SessionLocal() as session:
        count = DocumentRepository(session).reap_stale(
            older_than_seconds=settings.document_stale_after_seconds
        )
        session.commit()
    if count:
        logger.warning("stale_documents_reaped", count=count)


celery_app.conf.beat_schedule = {
    "reap-stale-documents": {
        "task": "reap_stale_documents",
        "schedule": get_settings().document_reap_interval_seconds,
    }
}
