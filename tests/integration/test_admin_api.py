import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.crypto import get_cipher
from app.db.session import SessionLocal
from app.models.audit_log import AuditAction, AuditLog
from app.models.document import Document, DocumentStatus, DocumentType
from app.models.user import User
from app.repositories.document_repository import RESTART_FAILURE_REASON
from app.schemas.admin import REPROCESS_BATCH_LIMIT
from tests.integration.conftest import TEST_PASSWORD, OwnerSessionLocal


@pytest.fixture
def auth(client: TestClient, admin: User) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login", json={"email": admin.email, "password": TEST_PASSWORD}
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def prebiller_auth(client: TestClient, prebiller: User) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login", json={"email": prebiller.email, "password": TEST_PASSWORD}
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def cleanup_documents():
    created: list[uuid.UUID] = []
    yield created
    with OwnerSessionLocal() as session:
        for document_id in created:
            session.execute(delete(AuditLog).where(AuditLog.document_id == document_id))
        session.commit()
    with SessionLocal() as session:
        for document_id in created:
            session.execute(delete(Document).where(Document.id == document_id))
        session.commit()


def _insert_document(
    uploader: User,
    *,
    status: DocumentStatus,
    failure_reason: str | None = None,
) -> uuid.UUID:
    with SessionLocal() as session:
        document = Document(
            uploaded_by=uploader.id,
            doc_type=DocumentType.EPCR,
            original_filename_enc=get_cipher().encrypt(b"f.pdf", aad=b"document.original_filename"),
            storage_key=uuid.uuid4().hex,
            content_type="application/pdf",
            size_bytes=1234,
            page_count=1,
            sha256=uuid.uuid4().hex * 2,
            status=status,
            failure_reason=failure_reason,
            patient_last_name="SHOULD_NOT_LEAK",
            patient_first_name="PATIENT",
        )
        session.add(document)
        session.flush()
        document_id = document.id
        session.commit()
    return document_id


class TestPipelineOverview:
    def test_requires_auth(self, client: TestClient) -> None:
        assert client.get("/api/v1/admin/pipeline").status_code == 401

    def test_forbidden_for_non_admin(self, client: TestClient, prebiller_auth) -> None:
        response = client.get("/api/v1/admin/pipeline", headers=prebiller_auth)
        assert response.status_code == 403

    def test_splits_in_flight_and_failed_by_reason(
        self, client, auth, admin, prebiller, cleanup_documents
    ) -> None:
        pending = _insert_document(admin, status=DocumentStatus.PENDING)
        running = _insert_document(prebiller, status=DocumentStatus.OCR_RUNNING)
        interrupted = _insert_document(
            prebiller, status=DocumentStatus.FAILED, failure_reason=RESTART_FAILURE_REASON
        )
        real_failure = _insert_document(
            admin, status=DocumentStatus.FAILED, failure_reason="ExtractionTimeoutError"
        )
        cleanup_documents.extend([pending, running, interrupted, real_failure])

        body = client.get("/api/v1/admin/pipeline", headers=auth).json()

        in_flight_ids = {item["id"] for item in body["in_flight"]}
        interrupted_ids = {item["id"] for item in body["interrupted"]}
        failed_ids = {item["id"] for item in body["failed"]}

        assert in_flight_ids >= {str(pending), str(running)}
        assert interrupted_ids >= {str(interrupted)}
        assert failed_ids >= {str(real_failure)}
        assert str(interrupted) not in failed_ids
        assert str(real_failure) not in interrupted_ids

        assert len(body["in_flight"]) >= 2
        assert len(body["interrupted"]) + len(body["failed"]) >= 2

        # Uploader is surfaced by email, and no patient field leaks into the view.
        all_items = body["in_flight"] + body["interrupted"] + body["failed"]
        by_id = {item["id"]: item for item in all_items}
        assert by_id[str(running)]["uploader_email"] == prebiller.email
        assert by_id[str(pending)]["uploader_email"] == admin.email
        body_text = str(body)
        assert "SHOULD_NOT_LEAK" not in body_text


class TestBulkReprocess:
    def test_requires_auth(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/admin/reprocess-failed", json={"document_ids": [str(uuid.uuid4())]}
        )
        assert response.status_code == 401

    def test_forbidden_for_non_admin(self, client: TestClient, prebiller_auth) -> None:
        response = client.post(
            "/api/v1/admin/reprocess-failed",
            headers=prebiller_auth,
            json={"document_ids": [str(uuid.uuid4())]},
        )
        assert response.status_code == 403

    def test_reprocesses_each_selected_document_and_audits_it_individually(
        self, client, auth, admin, cleanup_documents
    ) -> None:
        first = _insert_document(admin, status=DocumentStatus.FAILED, failure_reason="OcrError")
        second = _insert_document(admin, status=DocumentStatus.FAILED, failure_reason="OcrError")
        cleanup_documents.extend([first, second])

        response = client.post(
            "/api/v1/admin/reprocess-failed",
            headers=auth,
            json={"document_ids": [str(first), str(second)]},
        )
        assert response.status_code == 200
        body = response.json()
        assert set(body["reprocessed"]) == {str(first), str(second)}
        assert body["skipped"] == []

        with SessionLocal() as session:
            rows = (
                session.execute(
                    select(AuditLog)
                    .where(AuditLog.document_id.in_([first, second]))
                    .where(AuditLog.action == AuditAction.DOCUMENT_REPROCESSED.value)
                )
                .scalars()
                .all()
            )
        # One audit row per document, not one for the whole batch.
        assert {row.document_id for row in rows} == {first, second}
        assert all(row.user_id == admin.id for row in rows)

    def test_skips_a_document_that_is_not_in_a_terminal_state(
        self, client, auth, admin, cleanup_documents
    ) -> None:
        failed = _insert_document(admin, status=DocumentStatus.FAILED, failure_reason="X")
        mid_flight = _insert_document(admin, status=DocumentStatus.OCR_RUNNING)
        cleanup_documents.extend([failed, mid_flight])

        response = client.post(
            "/api/v1/admin/reprocess-failed",
            headers=auth,
            json={"document_ids": [str(failed), str(mid_flight)]},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["reprocessed"] == [str(failed)]
        assert body["skipped"] == [str(mid_flight)]

    def test_over_cap_request_is_rejected(self, client, auth) -> None:
        limit = REPROCESS_BATCH_LIMIT
        ids = [str(uuid.uuid4()) for _ in range(limit + 1)]
        response = client.post(
            "/api/v1/admin/reprocess-failed", headers=auth, json={"document_ids": ids}
        )
        assert response.status_code == 422

    def test_empty_id_list_is_rejected(self, client, auth) -> None:
        response = client.post(
            "/api/v1/admin/reprocess-failed", headers=auth, json={"document_ids": []}
        )
        assert response.status_code == 422
