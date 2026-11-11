import uuid
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.crypto import get_cipher
from app.db.session import SessionLocal
from app.models.audit_log import AuditAction, AuditLog
from app.models.document import Document, DocumentStatus, DocumentType
from app.models.extraction import Extraction
from app.models.ocr_result import OcrResult
from app.models.user import User
from app.schemas.extraction import InsuranceSlot, PatientExtraction
from tests.factories import make_jpeg, make_pdf, make_png
from tests.integration.conftest import TEST_PASSWORD, OwnerSessionLocal

PATIENT_FILENAME = "TESTERSON_JANE_facesheet.pdf"


@pytest.fixture
def auth(client: TestClient, admin: User) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login", json={"email": admin.email, "password": TEST_PASSWORD}
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


def _upload(
    client: TestClient,
    auth: dict[str, str],
    data: bytes,
    filename: str = PATIENT_FILENAME,
    content_type: str = "application/pdf",
    doc_type: str = "hfs",
):
    return client.post(
        "/api/v1/documents",
        headers=auth,
        files={"file": (filename, data, content_type)},
        data={"doc_type": doc_type},
    )


class TestUploadRequiresAuth:
    def test_anonymous_upload_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/documents", files={"file": ("x.pdf", make_pdf(), "application/pdf")}
        )
        assert response.status_code == 401


class TestUpload:
    def test_valid_pdf_is_accepted(self, client, auth, cleanup_documents) -> None:
        response = _upload(client, auth, make_pdf(11))
        assert response.status_code == 202

        body = response.json()
        cleanup_documents.append(uuid.UUID(body["id"]))
        assert body["status"] == DocumentStatus.PENDING.value
        assert body["deduplicated"] is False

    @pytest.mark.parametrize(
        ("data", "content_type"),
        [(make_png(), "image/png"), (make_jpeg(), "image/jpeg")],
    )
    def test_images_are_accepted(self, client, auth, cleanup_documents, data, content_type) -> None:
        response = _upload(client, auth, data, filename="scan.img", content_type=content_type)
        assert response.status_code == 202
        cleanup_documents.append(uuid.UUID(response.json()["id"]))

    def test_page_count_is_recorded(self, client, auth, cleanup_documents) -> None:
        document_id = uuid.UUID(_upload(client, auth, make_pdf(7)).json()["id"])
        cleanup_documents.append(document_id)
        body = client.get(f"/api/v1/documents/{document_id}", headers=auth).json()
        assert body["page_count"] == 7

    def test_identical_bytes_are_deduplicated(self, client, auth, cleanup_documents) -> None:
        data = make_pdf(2)
        first = _upload(client, auth, data).json()
        cleanup_documents.append(uuid.UUID(first["id"]))

        second = _upload(client, auth, data, filename="a-different-name.pdf").json()
        assert second["id"] == first["id"]
        assert second["deduplicated"] is True

    def test_reuploading_a_failed_document_re_arms_it_for_reprocessing(
        self, client, auth, cleanup_documents
    ) -> None:
        # Content-hash dedup means identical bytes never create a new row, so a
        # FAILED document had no path back to PENDING -- it was stuck forever.
        data = make_pdf(1)
        first = _upload(client, auth, data).json()
        document_id = uuid.UUID(first["id"])
        cleanup_documents.append(document_id)
        with SessionLocal() as session:
            session.get(Document, document_id).status = DocumentStatus.FAILED  # type: ignore[union-attr]
            session.commit()

        retry = _upload(client, auth, data, filename="retry.pdf").json()

        assert retry["id"] == first["id"]
        assert retry["deduplicated"] is False
        assert retry["status"] == DocumentStatus.PENDING.value

    def test_reuploading_a_failed_document_is_audited_as_reprocessed(
        self, client, auth, admin, cleanup_documents
    ) -> None:
        # The dedup re-arm does the same FAILED->PENDING transition as the
        # explicit /reprocess endpoint; the trail must show it too, not just
        # endpoint-driven reprocesses.
        data = make_pdf(1)
        first = _upload(client, auth, data).json()
        document_id = uuid.UUID(first["id"])
        cleanup_documents.append(document_id)
        with SessionLocal() as session:
            session.get(Document, document_id).status = DocumentStatus.FAILED  # type: ignore[union-attr]
            session.commit()

        _upload(client, auth, data, filename="retry.pdf")

        with SessionLocal() as session:
            row = (
                session.execute(
                    select(AuditLog)
                    .where(AuditLog.document_id == document_id)
                    .where(AuditLog.action == AuditAction.DOCUMENT_REPROCESSED.value)
                )
                .scalars()
                .first()
            )
        assert row is not None
        assert row.user_id == admin.id


class TestUploadRejection:
    def test_a_zip_renamed_to_pdf_is_refused(self, client, auth) -> None:
        # Declared type and extension both say PDF. The bytes say otherwise, and
        # only the bytes are consulted.
        response = _upload(client, auth, b"PK\x03\x04rest-of-a-zip")
        assert response.status_code == 415
        assert response.json()["error"] == "unsupported_file_type"

    def test_declared_type_that_contradicts_the_bytes_is_refused(self, client, auth) -> None:
        response = _upload(client, auth, make_pdf(), content_type="image/png")
        assert response.status_code == 415

    def test_empty_file_is_refused(self, client, auth) -> None:
        assert _upload(client, auth, b"").status_code == 413

    def test_oversized_file_is_refused(self, client, auth) -> None:
        settings = get_settings()
        oversized = b"%PDF-" + b"\x00" * (settings.max_upload_bytes + 1)
        assert _upload(client, auth, oversized).status_code == 413

    def test_too_many_pages_is_refused(self, client, auth) -> None:
        settings = get_settings()
        response = _upload(client, auth, make_pdf(settings.max_pdf_pages + 1))
        assert response.status_code == 422
        assert response.json()["error"] == "too_many_pages"

    def test_corrupt_pdf_is_refused(self, client, auth) -> None:
        response = _upload(client, auth, b"%PDF-1.4\nnot actually a pdf at all")
        assert response.status_code == 422
        assert response.json()["error"] == "corrupt_file"

    def test_rejections_are_audited_without_the_filename(self, client, auth, admin) -> None:
        _upload(client, auth, b"PK\x03\x04zip", filename=PATIENT_FILENAME)
        with SessionLocal() as session:
            stmt = (
                select(AuditLog)
                .where(AuditLog.user_id == admin.id)
                .where(AuditLog.action == AuditAction.DOCUMENT_UPLOAD_REJECTED.value)
            )
            row = session.execute(stmt).scalars().first()
            assert row is not None
            assert row.detail == {"reason": "UnsupportedFileTypeError"}
            # The filename carries a patient surname. It must not be audited.
            assert "TESTERSON" not in str(row.detail)


class TestEncryptionAtRest:
    def test_the_file_on_disk_is_ciphertext(self, client, auth, cleanup_documents) -> None:
        marker = b"%PDF-1.4 Testerson 123-45-6789"
        data = make_pdf(1) + b"\n% " + marker
        document_id = uuid.UUID(_upload(client, auth, data).json()["id"])
        cleanup_documents.append(document_id)

        with SessionLocal() as session:
            storage_key = session.get(Document, document_id).storage_key

        root = get_settings().data_dir / "documents"
        on_disk = (root / storage_key[:2] / storage_key[2:4] / storage_key).read_bytes()
        assert b"Testerson" not in on_disk
        assert b"123-45-6789" not in on_disk
        assert not on_disk.startswith(b"%PDF")

    def test_the_filename_column_is_ciphertext(self, client, auth, cleanup_documents) -> None:
        document_id = uuid.UUID(_upload(client, auth, make_pdf()).json()["id"])
        cleanup_documents.append(document_id)

        with SessionLocal() as session:
            stored = session.get(Document, document_id).original_filename_enc
        # Patient names live in filenames. A plaintext column here discloses PHI
        # to anyone holding SELECT.
        assert b"TESTERSON" not in stored

    def test_download_returns_the_original_bytes(self, client, auth, cleanup_documents) -> None:
        data = make_pdf(2)
        document_id = uuid.UUID(_upload(client, auth, data).json()["id"])
        cleanup_documents.append(document_id)

        response = client.get(f"/api/v1/documents/{document_id}/file", headers=auth)
        assert response.status_code == 200
        assert response.content == data
        assert response.headers["content-disposition"].startswith("attachment")
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_tampering_with_the_file_on_disk_is_detected(
        self, client, auth, cleanup_documents
    ) -> None:
        document_id = uuid.UUID(_upload(client, auth, make_pdf()).json()["id"])
        cleanup_documents.append(document_id)

        with SessionLocal() as session:
            storage_key = session.get(Document, document_id).storage_key
        root = get_settings().data_dir / "documents"
        path: Path = root / storage_key[:2] / storage_key[2:4] / storage_key

        blob = bytearray(path.read_bytes())
        blob[-1] ^= 0x01
        path.write_bytes(bytes(blob))

        # GCM authentication fails. A silent partial read would be far worse.
        response = client.get(f"/api/v1/documents/{document_id}/file", headers=auth)
        assert response.status_code == 500
        # A clean domain error, not an escaped traceback.
        assert response.json()["error"] == "document_unreadable"
        assert "InvalidTag" not in response.text


def _insert_document(
    admin: User,
    *,
    last_name: str | None = None,
    first_name: str | None = None,
    dob: date | None = None,
    run_number: str | None = None,
    status: DocumentStatus = DocumentStatus.COMPLETED,
    with_extraction: bool = False,
    mean_confidence: float | None = None,
) -> uuid.UUID:
    """A completed document row straight in the DB -- search reads only the
    denormalized identity columns, so this needs no storage or pipeline."""
    with SessionLocal() as session:
        document = Document(
            uploaded_by=admin.id,
            doc_type=DocumentType.EPCR,
            original_filename_enc=get_cipher().encrypt(b"f.pdf", aad=b"document.original_filename"),
            storage_key=uuid.uuid4().hex,
            content_type="application/pdf",
            size_bytes=1234,
            page_count=1,
            sha256=uuid.uuid4().hex * 2,
            status=status,
            patient_last_name=last_name,
            patient_first_name=first_name,
            patient_dob=dob,
            run_number=run_number,
        )
        session.add(document)
        session.flush()
        document_id = document.id
        if with_extraction:
            session.add(
                Extraction(
                    document_id=document_id,
                    fields={"last_name": {"value": last_name, "confidence": 1.0}},
                    llm_model="fake-llm/1.0",
                    duration_ms=10,
                    repaired=False,
                )
            )
        if mean_confidence is not None:
            session.add(
                OcrResult(
                    document_id=document_id,
                    raw_text="",
                    page_texts=[],
                    mean_confidence=mean_confidence,
                    engine_version="test",
                    page_count=1,
                    duration_ms=1,
                )
            )
        session.commit()
    return document_id


class TestSearch:
    def test_search_requires_auth(self, client: TestClient) -> None:
        assert client.get("/api/v1/documents").status_code == 401

    def test_filter_by_patient_name(self, client, auth, admin, cleanup_documents) -> None:
        wanted = _insert_document(admin, last_name="TESTERSON", first_name="JANE")
        other = _insert_document(admin, last_name="SMITH", first_name="JOHN")
        cleanup_documents.extend([wanted, other])

        body = client.get("/api/v1/documents?patient_name=WOJC", headers=auth).json()
        ids = {item["id"] for item in body["items"]}
        assert str(wanted) in ids
        assert str(other) not in ids

    def test_filter_by_run_number(self, client, auth, admin, cleanup_documents) -> None:
        match = _insert_document(admin, last_name="A", run_number="RUN-42")
        miss = _insert_document(admin, last_name="B", run_number="RUN-99")
        cleanup_documents.extend([match, miss])

        body = client.get("/api/v1/documents?run_number=RUN-42", headers=auth).json()
        ids = {item["id"] for item in body["items"]}
        assert ids == {str(match)}

    def test_filter_by_dob_range(self, client, auth, admin, cleanup_documents) -> None:
        inside = _insert_document(admin, last_name="C", dob=date(1980, 6, 15))
        before = _insert_document(admin, last_name="D", dob=date(1970, 1, 1))
        cleanup_documents.extend([inside, before])

        body = client.get(
            "/api/v1/documents?dob_from=1980-01-01&dob_to=1980-12-31", headers=auth
        ).json()
        ids = {item["id"] for item in body["items"]}
        assert str(inside) in ids
        assert str(before) not in ids

    def test_keyset_paging_walks_all_rows_without_overlap(
        self, client, auth, admin, cleanup_documents
    ) -> None:
        made = [_insert_document(admin, last_name="PAGE", run_number=f"P{i}") for i in range(3)]
        cleanup_documents.extend(made)

        first = client.get("/api/v1/documents?patient_name=PAGE&limit=2", headers=auth).json()
        assert len(first["items"]) == 2
        assert first["next_cursor"] is not None

        cur = first["next_cursor"]
        second = client.get(
            "/api/v1/documents?patient_name=PAGE&limit=2"
            f"&cursor_created_at={cur['created_at']}&cursor_id={cur['id']}",
            headers=auth,
        ).json()
        seen = {i["id"] for i in first["items"]} | {i["id"] for i in second["items"]}
        assert seen == {str(m) for m in made}  # all three, no duplicates

    def test_mean_confidence_is_joined_in(self, client, auth, admin, cleanup_documents) -> None:
        scored = _insert_document(admin, last_name="SCORED", mean_confidence=0.42)
        unscored = _insert_document(admin, last_name="UNSCORED")
        cleanup_documents.extend([scored, unscored])

        body = client.get("/api/v1/documents?patient_name=SCORED", headers=auth).json()
        assert body["items"][0]["mean_confidence"] == 0.42

        body = client.get("/api/v1/documents?patient_name=UNSCORED", headers=auth).json()
        # OCR hasn't run (no OcrResult row) -- the LEFT JOIN must not drop the document.
        assert body["items"][0]["mean_confidence"] is None

    def test_empty_result_is_an_empty_page(self, client, auth) -> None:
        body = client.get("/api/v1/documents?patient_name=NOSUCHNAMEZZZ", headers=auth).json()
        assert body["items"] == []
        assert body["next_cursor"] is None

    def test_review_queue_defaults_to_completed_only(
        self, client, auth, admin, cleanup_documents
    ) -> None:
        # The review defect this effort fixes: a document that never finished,
        # crashed, or failed the extraction gate must not show up by default.
        done = _insert_document(admin, last_name="GATEOK", status=DocumentStatus.COMPLETED)
        pending = _insert_document(admin, last_name="GATEOK", status=DocumentStatus.PENDING)
        failed = _insert_document(admin, last_name="GATEOK", status=DocumentStatus.FAILED)
        gate_failed = _insert_document(
            admin, last_name="GATEOK", status=DocumentStatus.EXTRACTION_FAILED
        )
        cleanup_documents.extend([done, pending, failed, gate_failed])

        body = client.get("/api/v1/documents?patient_name=GATEOK", headers=auth).json()
        ids = {item["id"] for item in body["items"]}
        assert ids == {str(done)}

    def test_review_queue_status_can_be_overridden_explicitly(
        self, client, auth, admin, cleanup_documents
    ) -> None:
        gate_failed = _insert_document(
            admin, last_name="GATEOVERRIDE", status=DocumentStatus.EXTRACTION_FAILED
        )
        cleanup_documents.append(gate_failed)

        body = client.get(
            "/api/v1/documents?patient_name=GATEOVERRIDE&status=extraction_failed", headers=auth
        ).json()
        ids = {item["id"] for item in body["items"]}
        assert ids == {str(gate_failed)}

    def test_search_is_audited_without_the_query_terms(self, client, auth, admin) -> None:
        client.get("/api/v1/documents?patient_name=TESTERSON", headers=auth)
        with SessionLocal() as session:
            row = (
                session.execute(
                    select(AuditLog)
                    .where(AuditLog.user_id == admin.id)
                    .where(AuditLog.action == AuditAction.DOCUMENT_VIEWED.value)
                    .order_by(AuditLog.id.desc())
                )
                .scalars()
                .first()
            )
        assert row is not None
        assert row.detail["resource"] == "search"
        # The searched surname is PHI and must never land in the audit detail.
        assert "TESTERSON" not in str(row.detail)


class TestExtractionEndpoint:
    def test_requires_auth(self, client: TestClient) -> None:
        assert client.get(f"/api/v1/documents/{uuid.uuid4()}/extraction").status_code == 401

    def test_returns_grounded_fields(self, client, auth, admin, cleanup_documents) -> None:
        document_id = _insert_document(admin, last_name="DOE", with_extraction=True)
        cleanup_documents.append(document_id)

        res = client.get(f"/api/v1/documents/{document_id}/extraction", headers=auth)
        assert res.status_code == 200
        body = res.json()
        assert body["document_id"] == str(document_id)
        assert body["llm_model"] == "fake-llm/1.0"
        assert body["fields"]["last_name"]["value"] == "DOE"
        assert body["fields"]["last_name"]["confidence"] == 1.0
        # A field the extraction omitted comes back as a null default, not missing.
        assert body["fields"]["run_number"]["value"] is None
        # Advisory flags are always present (possibly empty), computed at read
        # time -- codes and field references only, never document values.
        assert isinstance(body["suggested_flags"], list)
        # Nullable narrative summary rides the same response (EPCR only).
        assert body["narrative_summary"] is None
        assert "DOE" not in str(body["suggested_flags"])

    def test_404_before_extraction_exists(self, client, auth, admin, cleanup_documents) -> None:
        document_id = _insert_document(admin, last_name="DOE", status=DocumentStatus.OCR_RUNNING)
        cleanup_documents.append(document_id)

        assert (
            client.get(f"/api/v1/documents/{document_id}/extraction", headers=auth).status_code
            == 404
        )

    def test_unknown_document_is_404(self, client, auth) -> None:
        assert (
            client.get(f"/api/v1/documents/{uuid.uuid4()}/extraction", headers=auth).status_code
            == 404
        )

    def test_reading_extraction_is_audited(self, client, auth, admin, cleanup_documents) -> None:
        document_id = _insert_document(admin, last_name="DOE", with_extraction=True)
        cleanup_documents.append(document_id)
        client.get(f"/api/v1/documents/{document_id}/extraction", headers=auth)

        with SessionLocal() as session:
            actions = (
                session.execute(
                    select(AuditLog.detail)
                    .where(AuditLog.document_id == document_id)
                    .where(AuditLog.action == AuditAction.DOCUMENT_VIEWED.value)
                )
                .scalars()
                .all()
            )
        assert {"resource": "extraction"} in actions


def _insert_extraction(
    admin: User, *, last_name: str = "DOE", insurance_policy_id: str | None = None
) -> uuid.UUID:
    """A completed document with a full extraction row, shaped the way the
    pipeline actually persists one (`PatientExtraction.model_dump`) -- unlike
    `_insert_document(with_extraction=True)`, every field key is present, so a
    correction to any field name finds a dict to update."""
    document_id = _insert_document(admin, last_name=last_name)
    fields = PatientExtraction().model_dump(mode="json")
    fields["last_name"] = {
        "value": last_name,
        "confidence": 1.0,
        "source_page": 1,
        "source_bbox": None,
        "typed_value": last_name,
        "source": "grounded",
    }
    if insurance_policy_id is not None:
        slot = InsuranceSlot().model_dump(mode="json")
        slot["policy_id"] = {
            "value": insurance_policy_id,
            "confidence": 1.0,
            "source_page": 1,
            "source_bbox": None,
            "typed_value": insurance_policy_id,
            "source": "grounded",
        }
        fields["insurance"] = [slot]
    with SessionLocal() as session:
        session.add(
            Extraction(
                document_id=document_id,
                fields=fields,
                llm_model="fake-llm/1.0",
                duration_ms=10,
                repaired=False,
            )
        )
        session.commit()
    return document_id


class TestFieldCorrection:
    def test_requires_auth(self, client: TestClient) -> None:
        response = client.patch(
            f"/api/v1/documents/{uuid.uuid4()}/extraction/fields/last_name",
            json={"typed_value": "X"},
        )
        assert response.status_code == 401

    def test_corrects_the_typed_value_and_marks_it_corrected(
        self, client, auth, admin, cleanup_documents
    ) -> None:
        document_id = _insert_extraction(admin, last_name="DOE")
        cleanup_documents.append(document_id)

        response = client.patch(
            f"/api/v1/documents/{document_id}/extraction/fields/last_name",
            headers=auth,
            json={"typed_value": "SMITH"},
        )
        assert response.status_code == 200
        field = response.json()["fields"]["last_name"]
        assert field["typed_value"] == "SMITH"
        assert field["source"] == "corrected"
        # The grounded on-page reading is untouched -- provenance, not overwritten.
        assert field["value"] == "DOE"

    def test_correction_is_audited_without_phi(
        self, client, auth, admin, cleanup_documents
    ) -> None:
        document_id = _insert_extraction(admin, last_name="DOE")
        cleanup_documents.append(document_id)
        client.patch(
            f"/api/v1/documents/{document_id}/extraction/fields/last_name",
            headers=auth,
            json={"typed_value": "TESTERSON"},
        )
        with SessionLocal() as session:
            row = (
                session.execute(
                    select(AuditLog)
                    .where(AuditLog.document_id == document_id)
                    .where(AuditLog.action == AuditAction.EXTRACTION_FIELD_CORRECTED.value)
                )
                .scalars()
                .first()
            )
        assert row is not None
        assert row.detail == {"field": "last_name"}
        # The corrected value itself is PHI and must never reach the audit row.
        assert "TESTERSON" not in str(row.detail)

    def test_unknown_field_name_is_404(self, client, auth, admin, cleanup_documents) -> None:
        document_id = _insert_extraction(admin)
        cleanup_documents.append(document_id)
        response = client.patch(
            f"/api/v1/documents/{document_id}/extraction/fields/not_a_real_field",
            headers=auth,
            json={"typed_value": "X"},
        )
        assert response.status_code == 404

    def test_no_extraction_yet_is_404(self, client, auth, admin, cleanup_documents) -> None:
        document_id = _insert_document(admin, last_name="DOE", status=DocumentStatus.OCR_RUNNING)
        cleanup_documents.append(document_id)
        response = client.patch(
            f"/api/v1/documents/{document_id}/extraction/fields/last_name",
            headers=auth,
            json={"typed_value": "X"},
        )
        assert response.status_code == 404

    def test_unknown_document_is_404(self, client, auth) -> None:
        response = client.patch(
            f"/api/v1/documents/{uuid.uuid4()}/extraction/fields/last_name",
            headers=auth,
            json={"typed_value": "X"},
        )
        assert response.status_code == 404

    def test_corrects_an_insurance_slot_field(self, client, auth, admin, cleanup_documents) -> None:
        document_id = _insert_extraction(admin, insurance_policy_id="OLD-ID")
        cleanup_documents.append(document_id)

        response = client.patch(
            f"/api/v1/documents/{document_id}/extraction/fields/insurance.0.policy_id",
            headers=auth,
            json={"typed_value": "NEW-ID"},
        )
        assert response.status_code == 200
        slot = response.json()["fields"]["insurance"][0]
        assert slot["policy_id"]["typed_value"] == "NEW-ID"
        assert slot["policy_id"]["source"] == "corrected"

    def test_insurance_index_beyond_stored_slots_is_404(
        self, client, auth, admin, cleanup_documents
    ) -> None:
        # No insurance slots on this extraction at all -- insurance.0.* is a
        # shape-valid path but there is nothing at that index to correct.
        document_id = _insert_extraction(admin)
        cleanup_documents.append(document_id)
        response = client.patch(
            f"/api/v1/documents/{document_id}/extraction/fields/insurance.0.policy_id",
            headers=auth,
            json={"typed_value": "X"},
        )
        assert response.status_code == 404

    def test_insurance_index_beyond_max_slots_is_404(
        self, client, auth, admin, cleanup_documents
    ) -> None:
        document_id = _insert_extraction(admin)
        cleanup_documents.append(document_id)
        response = client.patch(
            f"/api/v1/documents/{document_id}/extraction/fields/insurance.5.policy_id",
            headers=auth,
            json={"typed_value": "X"},
        )
        assert response.status_code == 404

    def test_value_exceeding_max_length_is_422(
        self, client, auth, admin, cleanup_documents
    ) -> None:
        document_id = _insert_extraction(admin)
        cleanup_documents.append(document_id)
        response = client.patch(
            f"/api/v1/documents/{document_id}/extraction/fields/last_name",
            headers=auth,
            json={"typed_value": "X" * 256},
        )
        assert response.status_code == 422


class TestMarkReviewed:
    """A reviewer's explicit override: EXTRACTION_FAILED -> COMPLETED after
    correcting whatever fields they judged necessary via `correct_extraction_field`
    -- see app/api/v1/documents.py::mark_reviewed."""

    def test_requires_auth(self, client: TestClient) -> None:
        response = client.post(f"/api/v1/documents/{uuid.uuid4()}/mark-reviewed")
        assert response.status_code == 401

    def test_unknown_document_is_404(self, client, auth) -> None:
        response = client.post(f"/api/v1/documents/{uuid.uuid4()}/mark-reviewed", headers=auth)
        assert response.status_code == 404

    def test_marks_an_extraction_failed_document_completed(
        self, client, auth, admin, cleanup_documents
    ) -> None:
        document_id = _insert_document(
            admin, status=DocumentStatus.EXTRACTION_FAILED, with_extraction=True
        )
        cleanup_documents.append(document_id)
        with SessionLocal() as session:
            session.get(Document, document_id).failure_reason = (  # type: ignore[union-attr]
                "gate: missing=first_name confidence=0.00"
            )
            session.commit()

        response = client.post(f"/api/v1/documents/{document_id}/mark-reviewed", headers=auth)
        assert response.status_code == 200
        assert response.json()["status"] == DocumentStatus.COMPLETED.value

        with SessionLocal() as session:
            document = session.get(Document, document_id)
        assert document.status is DocumentStatus.COMPLETED
        # A completed document carrying a stale gate-failure reason would read
        # as still-broken everywhere that reason is surfaced.
        assert document.failure_reason is None

    def test_a_completed_document_is_a_conflict(
        self, client, auth, admin, cleanup_documents
    ) -> None:
        document_id = _insert_document(admin, status=DocumentStatus.COMPLETED)
        cleanup_documents.append(document_id)

        response = client.post(f"/api/v1/documents/{document_id}/mark-reviewed", headers=auth)
        assert response.status_code == 409

    def test_a_crashed_document_is_a_conflict(self, client, auth, admin, cleanup_documents) -> None:
        # FAILED means OCR/the LLM call itself crashed -- no Extraction row was
        # ever saved (see PipelineService._run_extraction), so there is nothing
        # for a reviewer to have corrected. Reprocess is the remedy, not this.
        document_id = _insert_document(admin, status=DocumentStatus.FAILED)
        cleanup_documents.append(document_id)

        response = client.post(f"/api/v1/documents/{document_id}/mark-reviewed", headers=auth)
        assert response.status_code == 409

    def test_is_audited(self, client, auth, admin, cleanup_documents) -> None:
        document_id = _insert_document(
            admin, status=DocumentStatus.EXTRACTION_FAILED, with_extraction=True
        )
        cleanup_documents.append(document_id)

        client.post(f"/api/v1/documents/{document_id}/mark-reviewed", headers=auth)

        with SessionLocal() as session:
            row = (
                session.execute(
                    select(AuditLog)
                    .where(AuditLog.document_id == document_id)
                    .where(AuditLog.action == AuditAction.EXTRACTION_MANUALLY_ACCEPTED.value)
                )
                .scalars()
                .first()
            )
        assert row is not None
        assert row.user_id == admin.id


class TestReprocess:
    def test_requires_auth(self, client: TestClient) -> None:
        assert client.post(f"/api/v1/documents/{uuid.uuid4()}/reprocess").status_code == 401

    def test_unknown_document_is_404(self, client, auth) -> None:
        response = client.post(f"/api/v1/documents/{uuid.uuid4()}/reprocess", headers=auth)
        assert response.status_code == 404

    def test_reprocessing_a_failed_document_reruns_the_pipeline(
        self, client, auth, cleanup_documents
    ) -> None:
        document_id = uuid.UUID(_upload(client, auth, make_pdf(1)).json()["id"])
        cleanup_documents.append(document_id)
        with SessionLocal() as session:
            session.get(Document, document_id).status = DocumentStatus.FAILED  # type: ignore[union-attr]
            session.commit()

        response = client.post(f"/api/v1/documents/{document_id}/reprocess", headers=auth)
        assert response.status_code == 202
        assert response.json()["status"] == DocumentStatus.PENDING.value

        # Celery runs eagerly in tests (see tests/conftest.py) -- the pipeline
        # has already run by the time this returns. `make_pdf(1)` is a blank page,
        # so the default fakes (FakeOcrEngine's placeholder text, FakeLlmProvider's
        # "{}") ground nothing real -- this lands EXTRACTION_FAILED, not
        # COMPLETED, same as a fresh upload of the same bytes would
        # (extraction_validation.py's gate). What this test actually verifies --
        # that reprocess re-arms and reruns the pipeline to a fresh terminal
        # status rather than leaving the stale FAILED row in place -- still holds.
        body = client.get(f"/api/v1/documents/{document_id}", headers=auth).json()
        assert body["status"] == DocumentStatus.EXTRACTION_FAILED.value
        assert body["failure_reason"] is not None

    def test_reprocessing_a_completed_document_is_allowed(
        self, client, auth, cleanup_documents
    ) -> None:
        document_id = uuid.UUID(_upload(client, auth, make_pdf(1)).json()["id"])
        cleanup_documents.append(document_id)

        response = client.post(f"/api/v1/documents/{document_id}/reprocess", headers=auth)
        assert response.status_code == 202

    def test_reprocessing_a_document_already_in_flight_is_a_conflict(
        self, client, auth, admin, cleanup_documents
    ) -> None:
        document_id = _insert_document(admin, status=DocumentStatus.OCR_RUNNING)
        cleanup_documents.append(document_id)

        response = client.post(f"/api/v1/documents/{document_id}/reprocess", headers=auth)
        assert response.status_code == 409

    def test_reprocessing_a_document_with_a_corrected_field_is_a_conflict(
        self, client, auth, admin, cleanup_documents
    ) -> None:
        # ExtractionRepository.add deletes-then-inserts the whole row on every
        # run -- a corrected field would be silently discarded, not merged.
        # Enforced server-side: a stale browser tab or a rushed click cannot
        # be relied on to see the client-side warning.
        document_id = _insert_extraction(admin, last_name="DOE")
        cleanup_documents.append(document_id)
        client.patch(
            f"/api/v1/documents/{document_id}/extraction/fields/last_name",
            headers=auth,
            json={"typed_value": "SMITH"},
        )

        response = client.post(f"/api/v1/documents/{document_id}/reprocess", headers=auth)
        assert response.status_code == 409
        assert "1 manually corrected field" in response.json()["message"]

    def test_reprocessing_with_force_overrides_the_correction_guard(
        self, client, auth, admin, cleanup_documents
    ) -> None:
        document_id = _insert_extraction(admin, last_name="DOE")
        cleanup_documents.append(document_id)
        client.patch(
            f"/api/v1/documents/{document_id}/extraction/fields/last_name",
            headers=auth,
            json={"typed_value": "SMITH"},
        )

        response = client.post(
            f"/api/v1/documents/{document_id}/reprocess?force=true", headers=auth
        )
        assert response.status_code == 202

    def test_reprocess_is_audited(self, client, auth, admin, cleanup_documents) -> None:
        document_id = uuid.UUID(_upload(client, auth, make_pdf(1)).json()["id"])
        cleanup_documents.append(document_id)
        with SessionLocal() as session:
            session.get(Document, document_id).status = DocumentStatus.FAILED  # type: ignore[union-attr]
            session.commit()

        client.post(f"/api/v1/documents/{document_id}/reprocess", headers=auth)

        with SessionLocal() as session:
            row = (
                session.execute(
                    select(AuditLog)
                    .where(AuditLog.document_id == document_id)
                    .where(AuditLog.action == AuditAction.DOCUMENT_REPROCESSED.value)
                )
                .scalars()
                .first()
            )
        assert row is not None
        assert row.user_id == admin.id


class TestPageRender:
    PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

    def test_requires_auth(self, client: TestClient) -> None:
        assert client.get(f"/api/v1/documents/{uuid.uuid4()}/page/1").status_code == 401

    def test_renders_a_page_as_png(self, client, auth, cleanup_documents) -> None:
        document_id = uuid.UUID(_upload(client, auth, make_pdf(2)).json()["id"])
        cleanup_documents.append(document_id)

        res = client.get(f"/api/v1/documents/{document_id}/page/1", headers=auth)
        assert res.status_code == 200
        assert res.headers["content-type"] == "image/png"
        assert res.content.startswith(self.PNG_MAGIC)

    def test_page_zero_is_rejected(self, client, auth, cleanup_documents) -> None:
        document_id = uuid.UUID(_upload(client, auth, make_pdf()).json()["id"])
        cleanup_documents.append(document_id)
        res = client.get(f"/api/v1/documents/{document_id}/page/0", headers=auth)
        assert res.status_code == 422

    def test_page_past_the_end_is_404(self, client, auth, cleanup_documents) -> None:
        document_id = uuid.UUID(_upload(client, auth, make_pdf(2)).json()["id"])
        cleanup_documents.append(document_id)
        res = client.get(f"/api/v1/documents/{document_id}/page/9", headers=auth)
        assert res.status_code == 404

    def test_unknown_document_is_404(self, client, auth) -> None:
        res = client.get(f"/api/v1/documents/{uuid.uuid4()}/page/1", headers=auth)
        assert res.status_code == 404

    def test_render_is_audited_as_a_page_view(self, client, auth, cleanup_documents) -> None:
        document_id = uuid.UUID(_upload(client, auth, make_pdf()).json()["id"])
        cleanup_documents.append(document_id)
        client.get(f"/api/v1/documents/{document_id}/page/1", headers=auth)

        with SessionLocal() as session:
            details = (
                session.execute(
                    select(AuditLog.detail)
                    .where(AuditLog.document_id == document_id)
                    .where(AuditLog.action == AuditAction.DOCUMENT_VIEWED.value)
                )
                .scalars()
                .all()
            )
        assert any(d and d.get("resource") == "page" for d in details)


class TestMetadataAndAudit:
    def test_get_returns_the_decrypted_filename(self, client, auth, cleanup_documents) -> None:
        document_id = uuid.UUID(_upload(client, auth, make_pdf()).json()["id"])
        cleanup_documents.append(document_id)

        body = client.get(f"/api/v1/documents/{document_id}", headers=auth).json()
        assert body["original_filename"] == PATIENT_FILENAME
        assert body["doc_type"] == DocumentType.HFS.value
        assert body["content_type"] == "application/pdf"
        # storage_key is the GCM associated data. A client has no use for it.
        assert "storage_key" not in body

    def test_unknown_document_is_404(self, client, auth) -> None:
        assert client.get(f"/api/v1/documents/{uuid.uuid4()}", headers=auth).status_code == 404

    def test_upload_view_and_download_are_audited(self, client, auth, admin, cleanup_documents):
        document_id = uuid.UUID(_upload(client, auth, make_pdf()).json()["id"])
        cleanup_documents.append(document_id)

        client.get(f"/api/v1/documents/{document_id}", headers=auth)
        client.get(f"/api/v1/documents/{document_id}/file", headers=auth)

        with SessionLocal() as session:
            stmt = select(AuditLog).where(AuditLog.document_id == document_id)
            actions = {row.action for row in session.execute(stmt).scalars()}

        # The pipeline dispatches as a Celery task, eager (and thus
        # deterministic) in tests -- assert the actions this test cares about
        # are present, not an exact set.
        assert actions >= {
            AuditAction.DOCUMENT_UPLOADED.value,
            AuditAction.DOCUMENT_VIEWED.value,
            AuditAction.DOCUMENT_DOWNLOADED.value,
        }


class TestRelatedDocuments:
    def test_unknown_document_is_404(self, client, auth) -> None:
        response = client.get(f"/api/v1/documents/{uuid.uuid4()}/related", headers=auth)
        assert response.status_code == 404

    def test_a_document_with_no_shared_identity_has_no_related_documents(
        self, client, auth, admin, cleanup_documents
    ) -> None:
        document_id = _insert_document(admin, run_number="LONE-1")
        cleanup_documents.append(document_id)

        body = client.get(f"/api/v1/documents/{document_id}/related", headers=auth).json()
        assert body["related"] == []
        assert body["mismatches"] == []

    def test_documents_sharing_a_run_number_are_related(
        self, client, auth, admin, cleanup_documents
    ) -> None:
        anchor = _insert_document(admin, run_number="R100", last_name="TESTERSON")
        sibling = _insert_document(admin, run_number="R100", last_name="TESTERSON")
        cleanup_documents.extend([anchor, sibling])

        body = client.get(f"/api/v1/documents/{anchor}/related", headers=auth).json()
        assert {item["id"] for item in body["related"]} == {str(sibling)}
        assert body["mismatches"] == []

    def test_disagreeing_dob_across_the_same_run_number_is_flagged(
        self, client, auth, admin, cleanup_documents
    ) -> None:
        # The sample/insurance_eligibility.pdf scenario: an eligibility
        # response sharing an encounter's run number but disagreeing on DOB.
        anchor = _insert_document(admin, run_number="R200", dob=date(1990, 1, 1))
        mismatched = _insert_document(admin, run_number="R200", dob=date(1985, 6, 15))
        cleanup_documents.extend([anchor, mismatched])

        body = client.get(f"/api/v1/documents/{anchor}/related", headers=auth).json()
        assert body["mismatches"] == [
            {"field": "patient_dob", "values": ["1985-06-15", "1990-01-01"]}
        ]

    def test_viewing_related_documents_is_audited_without_phi(
        self, client, auth, admin, cleanup_documents
    ) -> None:
        anchor = _insert_document(admin, run_number="R300", last_name="TESTERSON")
        sibling = _insert_document(admin, run_number="R300", last_name="TESTERSON")
        cleanup_documents.extend([anchor, sibling])

        client.get(f"/api/v1/documents/{anchor}/related", headers=auth)

        with SessionLocal() as session:
            stmt = select(AuditLog).where(
                AuditLog.document_id == anchor, AuditLog.action == AuditAction.DOCUMENT_VIEWED.value
            )
            rows = list(session.execute(stmt).scalars())

        related_views = [
            row for row in rows if row.detail and row.detail.get("resource") == "related"
        ]
        assert len(related_views) == 1
        assert related_views[0].detail == {"resource": "related", "count": 1}
        assert "TESTERSON" not in str(related_views[0].detail)


class TestAsciiFallback:
    """Pure helper, no database: the download filename header must never
    carry characters h11 refuses (CR/LF -> 500 on download)."""

    def test_control_characters_are_stripped(self) -> None:
        from app.api.v1.documents import _ascii_fallback

        assert _ascii_fallback("evil\r\nX-Injected: 1.pdf") == "evilX-Injected: 1.pdf"

    def test_empty_after_stripping_falls_back(self) -> None:
        from app.api.v1.documents import _ascii_fallback

        assert _ascii_fallback("\r\n\x00") == "document"
