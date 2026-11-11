import uuid

import pytest
from celery.exceptions import Retry

from app import worker
from app.llm.base import LlmTransientError


class _StubPipelineService:
    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, bool]] = []

    def process_document(self, document_id: uuid.UUID, *, resume: bool = False) -> None:
        self.calls.append((document_id, resume))


class _TransientlyFailingPipelineService:
    """`process_document` always raises `LlmTransientError`, the way a
    dropped LM Studio connection would; `fail_after_retries_exhausted`
    records its call instead of touching a real document."""

    def __init__(self) -> None:
        self.processed: list[uuid.UUID] = []
        self.exhausted: list[tuple[uuid.UUID, Exception]] = []

    def process_document(self, document_id: uuid.UUID, *, resume: bool = False) -> None:
        self.processed.append(document_id)
        raise LlmTransientError("simulated transport failure")

    def fail_after_retries_exhausted(self, document_id: uuid.UUID, exc: Exception) -> None:
        self.exhausted.append((document_id, exc))


def test_task_calls_pipeline_service_with_the_parsed_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubPipelineService()
    monkeypatch.setattr(worker, "_pipeline_service", lambda: stub)

    document_id = uuid.uuid4()
    worker.process_document_task.run(str(document_id))

    assert stub.calls == [(document_id, False)]


def test_task_passes_resume_true_when_the_broker_redelivers_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `redelivered` is set by the broker, not by `self.retry()` -- it's what
    # `task_reject_on_worker_lost` produces when a worker dies mid-task and
    # the same message goes back on the queue for another worker to pick up.
    # `push_request`/`pop_request` is Celery's documented way to fake that
    # context for a task called directly via `.run()`.
    stub = _StubPipelineService()
    monkeypatch.setattr(worker, "_pipeline_service", lambda: stub)
    document_id = uuid.uuid4()

    worker.process_document_task.push_request(delivery_info={"redelivered": True})
    try:
        worker.process_document_task.run(str(document_id))
    finally:
        worker.process_document_task.pop_request()

    assert stub.calls == [(document_id, True)]


def test_task_passes_resume_true_on_a_celery_level_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A `self.retry()` after `LlmTransientError` (below) is a fresh task
    # dispatch, not a broker redelivery -- `delivery_info.get("redelivered")`
    # is never set for it, only `self.request.retries > 0` distinguishes it
    # from the first attempt. Before this was checked, the retried call
    # passed `resume=False`, so `PipelineService.process_document` only
    # accepted a PENDING document -- but the first attempt had already
    # advanced it to OCR_RUNNING/AI_RUNNING before the LLM call failed, so
    # every retry silently no-op'd (`pipeline_skipped_not_pending`) and the
    # document was orphaned forever. `push_request(retries=1)` simulates
    # Celery having already counted one retry, the same way
    # `push_request(delivery_info=...)` simulates a redelivery above.
    stub = _StubPipelineService()
    monkeypatch.setattr(worker, "_pipeline_service", lambda: stub)
    document_id = uuid.uuid4()

    worker.process_document_task.push_request(retries=1)
    try:
        worker.process_document_task.run(str(document_id))
    finally:
        worker.process_document_task.pop_request()

    assert stub.calls == [(document_id, True)]


def test_task_ignores_result_backend() -> None:
    # Document.status is the single source of truth for a task's outcome --
    # see app/worker.py. No result backend means nothing else can drift out of
    # sync with it.
    assert worker.celery_app.conf.task_ignore_result is True


def test_task_acks_late_so_a_dead_worker_s_task_is_redelivered() -> None:
    # Default (early ack) considers a task done the instant a worker picks it
    # up -- a worker killed mid-task (OOM, crash) then loses it silently.
    assert worker.celery_app.conf.task_acks_late is True
    assert worker.celery_app.conf.task_reject_on_worker_lost is True


def test_broker_visibility_timeout_matches_settings() -> None:
    from app.core.config import get_settings

    options = worker.celery_app.conf.broker_transport_options
    assert options["visibility_timeout"] == get_settings().celery_broker_visibility_timeout_seconds


def test_reap_stale_documents_task_is_on_the_beat_schedule() -> None:
    from app.core.config import get_settings

    entry = worker.celery_app.conf.beat_schedule["reap-stale-documents"]
    assert entry["task"] == "reap_stale_documents"
    assert entry["schedule"] == get_settings().document_reap_interval_seconds


def test_llm_transient_error_is_retried_when_under_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `.run()` calls the task directly, outside a worker/broker context, so
    # Celery's `self.retry()` takes its "called directly" shortcut and
    # re-raises the same exception rather than actually scheduling a retry --
    # this is enough to prove the task attempts a retry instead of marking
    # the document FAILED immediately.
    stub = _TransientlyFailingPipelineService()
    monkeypatch.setattr(worker, "_pipeline_service", lambda: stub)
    document_id = uuid.uuid4()

    with pytest.raises(LlmTransientError):
        worker.process_document_task.run(str(document_id))

    assert stub.processed == [document_id]
    assert stub.exhausted == []


def test_llm_transient_error_marks_failed_once_retries_are_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _TransientlyFailingPipelineService()
    monkeypatch.setattr(worker, "_pipeline_service", lambda: stub)
    monkeypatch.setattr(worker.process_document_task, "max_retries", 0)
    document_id = uuid.uuid4()

    worker.process_document_task.run(str(document_id))  # must not raise

    assert len(stub.exhausted) == 1
    exhausted_id, exc = stub.exhausted[0]
    assert exhausted_id == document_id
    assert isinstance(exc, LlmTransientError)


class _StubClaimFinalizer:
    def __init__(self, *, ready: bool) -> None:
        self._ready = ready
        self.calls: list[uuid.UUID] = []

    def try_finalize(self, claim_id: uuid.UUID) -> bool:
        self.calls.append(claim_id)
        return self._ready


def test_finalize_claim_task_calls_claim_finalizer_with_the_parsed_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubClaimFinalizer(ready=True)
    monkeypatch.setattr(worker, "_claim_finalizer", lambda: stub)

    claim_id = uuid.uuid4()
    worker.finalize_claim_task.run(str(claim_id))

    assert stub.calls == [claim_id]


def test_finalize_claim_task_retries_while_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    # `.run()` outside a worker context makes `self.retry()` re-raise
    # directly -- same shortcut `process_document_task`'s own retry test
    # above relies on.
    stub = _StubClaimFinalizer(ready=False)
    monkeypatch.setattr(worker, "_claim_finalizer", lambda: stub)
    claim_id = uuid.uuid4()

    with pytest.raises(Retry):
        worker.finalize_claim_task.run(str(claim_id))

    assert stub.calls == [claim_id]


def test_finalize_claim_task_does_not_retry_once_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubClaimFinalizer(ready=True)
    monkeypatch.setattr(worker, "_claim_finalizer", lambda: stub)

    worker.finalize_claim_task.run(str(uuid.uuid4()))  # must not raise
