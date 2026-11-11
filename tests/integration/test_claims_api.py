import uuid
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import worker
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.audit_log import AuditAction, AuditLog
from app.models.claim import Claim, ClaimDocument, ClaimStatus, ClaimTag
from app.models.document import Document, DocumentStatus
from app.models.user import User
from app.services.claim_service import ClaimFinalizer
from app.services.tag_registry import load_registry
from tests.factories import make_pdf
from tests.fakes import FakeLlmProvider
from tests.integration.conftest import TEST_PASSWORD, OwnerSessionLocal

# FakeOcrEngine's default recognized text (tests/fakes.py) -- grounding a
# last_name/first_name value against it is what lets a document actually
# clear the extraction_validation gate and reach COMPLETED, instead of the
# default `FakeLlmProvider()` (`"{}"`) which every EXTRACTION_FAILED test
# elsewhere in this suite relies on.
_GROUNDED_NAME_RESPONSE = '{"last_name": {"value": "DOE"}, "first_name": {"value": "JANE"}}'


def _ground_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        worker, "get_llm_provider", lambda: FakeLlmProvider(response=_GROUNDED_NAME_RESPONSE)
    )


@pytest.fixture
def auth(client: TestClient, admin: User) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login", json={"email": admin.email, "password": TEST_PASSWORD}
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def cleanup():
    claim_ids: list[uuid.UUID] = []
    document_ids: list[uuid.UUID] = []
    yield claim_ids, document_ids
    with OwnerSessionLocal() as session:
        for claim_id in claim_ids:
            session.execute(delete(AuditLog).where(AuditLog.claim_id == claim_id))
        for document_id in document_ids:
            session.execute(delete(AuditLog).where(AuditLog.document_id == document_id))
        session.commit()
    with SessionLocal() as session:
        for claim_id in claim_ids:
            session.execute(delete(ClaimTag).where(ClaimTag.claim_id == claim_id))
            session.execute(delete(ClaimDocument).where(ClaimDocument.claim_id == claim_id))
        session.commit()
        for claim_id in claim_ids:
            session.execute(delete(Claim).where(Claim.id == claim_id))
        for document_id in document_ids:
            session.execute(delete(Document).where(Document.id == document_id))
        session.commit()


def _pdf_file(name: str = "face_sheet.pdf", *, pages: int = 1) -> tuple[str, bytes, str]:
    # `pages` varies the actual bytes, not just the filename -- two files
    # meant to be genuinely distinct documents (not a dedup case) must differ
    # in content, since sha256 dedup only looks at bytes.
    return (name, make_pdf(pages), "application/pdf")


def _upload_claim(client: TestClient, auth: dict[str, str], files: list[tuple[str, bytes, str]]):
    return client.post(
        "/api/v1/claims",
        headers=auth,
        files=[("files", f) for f in files],
    )


def _track(cleanup, body: dict) -> uuid.UUID:
    claim_ids, document_ids = cleanup
    claim_id = uuid.UUID(body["claim_id"])
    claim_ids.append(claim_id)
    document_ids.extend(uuid.UUID(a["document_id"]) for a in body["accepted"])
    return claim_id


class TestCreateClaimRequiresAuth:
    def test_anonymous_upload_is_refused(self, client: TestClient) -> None:
        response = client.post("/api/v1/claims", files=[("files", _pdf_file())])
        assert response.status_code == 401


class TestCreateClaim:
    def test_multi_file_upload_is_accepted(self, client, auth, cleanup) -> None:
        response = _upload_claim(
            client, auth, [_pdf_file("hfs.pdf", pages=1), _pdf_file("epcr.pdf", pages=2)]
        )
        assert response.status_code == 202
        body = response.json()
        _track(cleanup, body)
        assert len(body["accepted"]) == 2
        assert body["rejected"] == []

    def test_task_always_eager_runs_the_pipeline_inline_and_finalizes(
        self, client, auth, cleanup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `tests/conftest.py` sets `task_always_eager = True`, so `.delay()` and
        # `.apply_async()` both run synchronously during the POST -- by the time
        # this returns, every member document and the claim itself are terminal.
        _ground_extraction(monkeypatch)
        response = _upload_claim(client, auth, [_pdf_file()])
        body = response.json()
        claim_id = _track(cleanup, body)

        detail = client.get(f"/api/v1/claims/{claim_id}", headers=auth).json()
        assert detail["status"] == "completed"
        assert len(detail["members"]) == 1
        assert detail["members"][0]["status"] == DocumentStatus.COMPLETED.value

    def test_one_invalid_file_is_rejected_not_the_whole_claim(self, client, auth, cleanup) -> None:
        good = _pdf_file("good.pdf")
        bad = ("not_a_real_file.pdf", b"this is not a pdf or an image", "application/pdf")
        response = _upload_claim(client, auth, [good, bad])
        assert response.status_code == 202
        body = response.json()
        _track(cleanup, body)
        assert len(body["accepted"]) == 1
        assert len(body["rejected"]) == 1
        assert body["rejected"][0]["filename"] == "not_a_real_file.pdf"

    def test_every_file_rejected_refuses_the_claim(self, client, auth) -> None:
        bad = ("nope.pdf", b"garbage", "application/pdf")
        response = _upload_claim(client, auth, [bad])
        assert response.status_code == 422


class TestClaimFinalizePartial:
    def test_one_failed_member_document_finalizes_the_claim_as_partial(
        self, client, auth, cleanup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ground_extraction(monkeypatch)
        response = _upload_claim(
            client, auth, [_pdf_file("a.pdf", pages=1), _pdf_file("b.pdf", pages=2)]
        )
        body = response.json()
        claim_id = _track(cleanup, body)
        failed_document_id = uuid.UUID(body["accepted"][0]["document_id"])

        # Both documents already completed (grounded extraction + the pipeline
        # ran inline during upload) -- force one back to FAILED to exercise the
        # partial path, then re-run the same finalize logic the worker uses.
        with SessionLocal() as session:
            document = session.get(Document, failed_document_id)
            document.status = DocumentStatus.FAILED  # type: ignore[union-attr]
            session.commit()
            claim = session.get(Claim, claim_id)
            assert claim is not None
            claim.status = ClaimStatus.PENDING
            claim.finalized_at = None
            session.commit()

        finalizer = ClaimFinalizer(
            session_factory=SessionLocal,
            registry=load_registry(),
            finalize_timeout_seconds=get_settings().claim_finalize_timeout_seconds,
        )
        assert finalizer.try_finalize(claim_id) is True

        detail = client.get(f"/api/v1/claims/{claim_id}", headers=auth).json()
        assert detail["status"] == "partial"


class TestClaimReprocess:
    def test_reprocessing_a_claim_with_a_corrected_field_is_a_conflict(
        self, client, auth, cleanup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same guard as documents.reprocess_document, applied claim-wide: a
        # member document's ExtractionRepository.add re-run would silently
        # discard its correction, so reprocess-the-claim refuses too.
        _ground_extraction(monkeypatch)
        body = _upload_claim(client, auth, [_pdf_file()]).json()
        claim_id = _track(cleanup, body)
        document_id = body["accepted"][0]["document_id"]

        client.patch(
            f"/api/v1/documents/{document_id}/extraction/fields/last_name",
            headers=auth,
            json={"typed_value": "SMITH"},
        )

        response = client.post(f"/api/v1/claims/{claim_id}/reprocess", headers=auth)
        assert response.status_code == 409
        assert "1 manually corrected field" in response.json()["message"]

    def test_reprocessing_a_claim_with_force_overrides_the_correction_guard(
        self, client, auth, cleanup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ground_extraction(monkeypatch)
        body = _upload_claim(client, auth, [_pdf_file()]).json()
        claim_id = _track(cleanup, body)
        document_id = body["accepted"][0]["document_id"]

        client.patch(
            f"/api/v1/documents/{document_id}/extraction/fields/last_name",
            headers=auth,
            json={"typed_value": "SMITH"},
        )

        response = client.post(f"/api/v1/claims/{claim_id}/reprocess?force=true", headers=auth)
        assert response.status_code == 202

    def test_reprocessing_a_claim_without_corrections_needs_no_force(
        self, client, auth, cleanup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ground_extraction(monkeypatch)
        body = _upload_claim(client, auth, [_pdf_file()]).json()
        claim_id = _track(cleanup, body)

        response = client.post(f"/api/v1/claims/{claim_id}/reprocess", headers=auth)
        assert response.status_code == 202


class TestClaimExtractionAndTags:
    def test_extraction_endpoint_is_audited(self, client, auth, cleanup) -> None:
        body = _upload_claim(client, auth, [_pdf_file()]).json()
        claim_id = _track(cleanup, body)

        response = client.get(f"/api/v1/claims/{claim_id}/extraction", headers=auth)
        assert response.status_code == 200
        assert "fields" in response.json()

        with SessionLocal() as session:
            stmt = select(AuditLog).where(
                AuditLog.claim_id == claim_id, AuditLog.action == AuditAction.CLAIM_VIEWED.value
            )
            rows = list(session.execute(stmt).scalars())
        assert any((row.detail or {}).get("resource") == "extraction" for row in rows)

    def test_no_epcr_and_no_face_sheet_fire_when_doc_type_is_unknown(
        self, client, auth, cleanup
    ) -> None:
        # FakeOcrEngine's canned text matches none of classification.classify()'s
        # regexes, so doc_type stays UNKNOWN -- both doc-type-presence tags fire
        # deterministically regardless of extracted field content.
        body = _upload_claim(client, auth, [_pdf_file()]).json()
        claim_id = _track(cleanup, body)

        tags = client.get(f"/api/v1/claims/{claim_id}/tags", headers=auth).json()
        names = {t["tag_name"] for t in tags["assigned"]}
        assert "NO EPCR" in names
        assert "No Face Sheet" in names
        assert tags["manual_review"]  # MCR Age / MCD Minor / ... always present

    def test_no_epcr_does_not_fire_for_a_member_still_stuck_when_the_deadline_forces_finalize(
        self, client, auth, cleanup
    ) -> None:
        # Reproduces the live incident this regression pins down: a member
        # document that never leaves AI_RUNNING (e.g. every LLM-transient
        # retry silently orphaned it, see app/worker.py) never gets a real
        # doc_type classification (PipelineService._run_extraction persists
        # it only once the LLM actually responds). If ClaimFinalizer.try_finalize
        # is forced through anyway by its deadline, `doc_types_present` would
        # still just be {UNKNOWN} -- indistinguishable from a claim that
        # legitimately finished with an unclassifiable document (the sibling
        # test above), except this one never got the chance to be classified
        # at all. `missing_doc_type` rules (NO EPCR, No Face Sheet) must not
        # fire on that unresolved data.
        body = _upload_claim(client, auth, [_pdf_file()]).json()
        claim_id = _track(cleanup, body)
        stuck_document_id = uuid.UUID(body["accepted"][0]["document_id"])

        with SessionLocal() as session:
            document = session.get(Document, stuck_document_id)
            document.status = DocumentStatus.AI_RUNNING  # type: ignore[union-attr]
            session.commit()
            claim = session.get(Claim, claim_id)
            assert claim is not None
            claim.status = ClaimStatus.PENDING
            claim.finalized_at = None
            session.commit()

        finalizer = ClaimFinalizer(
            session_factory=SessionLocal,
            registry=load_registry(),
            finalize_timeout_seconds=0,  # deadline already passed -- force through now
        )
        assert finalizer.try_finalize(claim_id) is True

        detail = client.get(f"/api/v1/claims/{claim_id}", headers=auth).json()
        assert detail["status"] == "failed"

        tags = client.get(f"/api/v1/claims/{claim_id}/tags", headers=auth).json()
        assert "NO EPCR" not in {t["tag_name"] for t in tags["assigned"]}

    def test_no_epcr_does_not_fire_for_a_member_that_failed_before_classification(
        self, client, auth, cleanup
    ) -> None:
        # Sibling of the still-stuck regression above, for a second way a
        # member's doc_type can end up unresolved -- confirmed live: a claim
        # deduplicated onto an old document the stale-document reaper
        # (DocumentRepository.reap_stale) had already marked FAILED before it
        # ever reached extraction. Unlike EXTRACTION_FAILED (the sibling
        # `test_no_epcr_and_no_face_sheet_fire_when_doc_type_is_unknown`
        # test's scenario), a plain FAILED status -- from OCR failure, a
        # non-transient LLM error, or the reaper -- can be reached *before*
        # `PipelineService._run_extraction` ever calls `set_doc_type`, so
        # `doc_type` is just as unresolved as a member still mid-pipeline.
        # No deadline-forcing needed here: FAILED is already terminal, so
        # try_finalize proceeds on the first call.
        body = _upload_claim(client, auth, [_pdf_file()]).json()
        claim_id = _track(cleanup, body)
        failed_document_id = uuid.UUID(body["accepted"][0]["document_id"])

        with SessionLocal() as session:
            document = session.get(Document, failed_document_id)
            document.status = DocumentStatus.FAILED  # type: ignore[union-attr]
            document.failure_reason = "ocr: OcrError"  # type: ignore[union-attr]
            session.commit()
            claim = session.get(Claim, claim_id)
            assert claim is not None
            claim.status = ClaimStatus.PENDING
            claim.finalized_at = None
            session.commit()

        finalizer = ClaimFinalizer(
            session_factory=SessionLocal,
            registry=load_registry(),
            finalize_timeout_seconds=get_settings().claim_finalize_timeout_seconds,
        )
        assert finalizer.try_finalize(claim_id) is True

        detail = client.get(f"/api/v1/claims/{claim_id}", headers=auth).json()
        assert detail["status"] == "failed"

        tags = client.get(f"/api/v1/claims/{claim_id}/tags", headers=auth).json()
        assert "NO EPCR" not in {t["tag_name"] for t in tags["assigned"]}

    def test_tags_endpoint_is_audited(self, client, auth, cleanup) -> None:
        body = _upload_claim(client, auth, [_pdf_file()]).json()
        claim_id = _track(cleanup, body)
        client.get(f"/api/v1/claims/{claim_id}/tags", headers=auth)

        with SessionLocal() as session:
            stmt = select(AuditLog).where(
                AuditLog.claim_id == claim_id, AuditLog.action == AuditAction.CLAIM_VIEWED.value
            )
            rows = list(session.execute(stmt).scalars())
        assert len(rows) >= 1

    def test_reviewer_can_add_and_dismiss_a_tag(self, client, auth, cleanup) -> None:
        body = _upload_claim(client, auth, [_pdf_file()]).json()
        claim_id = _track(cleanup, body)

        add = client.post(
            f"/api/v1/claims/{claim_id}/tags", headers=auth, json={"tag_name": "Hospice"}
        )
        assert add.status_code == 201
        assert add.json()["source"] == "reviewer"

        tags = client.get(f"/api/v1/claims/{claim_id}/tags", headers=auth).json()
        assert "Hospice" in {t["tag_name"] for t in tags["assigned"]}

        dismiss = client.delete(f"/api/v1/claims/{claim_id}/tags/{quote('Hospice')}", headers=auth)
        assert dismiss.status_code == 204

        tags = client.get(f"/api/v1/claims/{claim_id}/tags", headers=auth).json()
        assert "Hospice" not in {t["tag_name"] for t in tags["assigned"]}

    def test_adding_an_unregistered_tag_name_404s(self, client, auth, cleanup) -> None:
        body = _upload_claim(client, auth, [_pdf_file()]).json()
        claim_id = _track(cleanup, body)

        response = client.post(
            f"/api/v1/claims/{claim_id}/tags", headers=auth, json={"tag_name": "Not A Real Tag"}
        )
        assert response.status_code == 404

    def test_dismissing_an_auto_tag_survives_reprocess(self, client, auth, cleanup) -> None:
        body = _upload_claim(client, auth, [_pdf_file()]).json()
        claim_id = _track(cleanup, body)

        tags = client.get(f"/api/v1/claims/{claim_id}/tags", headers=auth).json()
        assert "NO EPCR" in {t["tag_name"] for t in tags["assigned"]}

        dismiss = client.delete(f"/api/v1/claims/{claim_id}/tags/{quote('NO EPCR')}", headers=auth)
        assert dismiss.status_code == 204

        # Re-run finalize directly (task_always_eager already ran the full
        # /reprocess flow's dispatch synchronously; re-finalizing again proves
        # the dismissal isn't silently re-created).
        finalizer = ClaimFinalizer(
            session_factory=SessionLocal,
            registry=load_registry(),
            finalize_timeout_seconds=get_settings().claim_finalize_timeout_seconds,
        )
        finalizer.try_finalize(claim_id)

        tags = client.get(f"/api/v1/claims/{claim_id}/tags", headers=auth).json()
        assert "NO EPCR" not in {t["tag_name"] for t in tags["assigned"]}


class TestRearmFailedDedup:
    def test_dedup_onto_extraction_failed_document_rearms_and_dispatches(
        self, client, auth, cleanup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ground_extraction(monkeypatch)
        pdf = _pdf_file("facesheet.pdf", pages=7)
        first = _upload_claim(client, auth, [pdf])
        first_body = first.json()
        document_id = uuid.UUID(first_body["accepted"][0]["document_id"])
        _track(cleanup, first_body)

        with SessionLocal() as session:
            document = session.get(Document, document_id)
            document.status = DocumentStatus.EXTRACTION_FAILED  # type: ignore[union-attr]
            document.failure_reason = "gate: missing fields"  # type: ignore[union-attr]
            session.commit()

        second = _upload_claim(client, auth, [pdf])
        assert second.status_code == 202
        second_body = second.json()
        _track(cleanup, second_body)
        assert second_body["accepted"][0]["deduplicated"] is True

        with SessionLocal() as session:
            document = session.get(Document, document_id)
            assert document is not None
            assert document.status is DocumentStatus.COMPLETED

    def test_dedup_onto_completed_document_does_not_rearm(
        self, client, auth, cleanup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ground_extraction(monkeypatch)
        pdf = _pdf_file("completed.pdf", pages=8)
        first_body = _upload_claim(client, auth, [pdf]).json()
        assert first_body["accepted"][0]["deduplicated"] is False
        document_id = uuid.UUID(first_body["accepted"][0]["document_id"])
        _track(cleanup, first_body)

        with SessionLocal() as session:
            assert session.get(Document, document_id).status is DocumentStatus.COMPLETED

        second_body = _upload_claim(client, auth, [pdf]).json()
        _track(cleanup, second_body)
        assert second_body["accepted"][0]["deduplicated"] is True

        with SessionLocal() as session:
            assert session.get(Document, document_id).status is DocumentStatus.COMPLETED
