"""F2 regression: processing_started_at survives reprocess."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import delete, update

from app.core.config import get_settings
from app.core.crypto import get_cipher
from app.db.session import SessionLocal
from app.models.claim import Claim, ClaimDocument, ClaimStatus
from app.models.document import Document, DocumentStatus, DocumentType
from app.models.user import User
from app.repositories.claim_repository import ClaimRepository
from app.repositories.document_repository import DocumentRepository
from app.services.claim_service import ClaimFinalizer
from app.services.tag_registry import load_registry
from app.storage.encrypted_local import EncryptedLocalStorage
from tests.factories import make_pdf


@pytest.fixture
def storage(tmp_path):
    return EncryptedLocalStorage(root=tmp_path, cipher=get_cipher())


@pytest.fixture
def stored_document(admin: User, storage) -> Iterator[UUID]:
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
            processing_started_at=datetime.now(UTC),
        )
        session.add(document)
        session.commit()
        document_id = document.id

    yield document_id

    with SessionLocal() as session:
        session.execute(delete(Document).where(Document.id == document_id))
        session.commit()


def _delete_claim(claim_id: UUID) -> None:
    with SessionLocal() as session:
        session.execute(delete(ClaimDocument).where(ClaimDocument.claim_id == claim_id))
        session.execute(delete(Claim).where(Claim.id == claim_id))
        session.commit()


class TestProcessingStartedAtEpoch:
    def test_old_created_at_with_fresh_processing_epoch_is_not_reaped(self, stored_document) -> None:
        with SessionLocal() as session:
            DocumentRepository(session).claim(
                stored_document, expected=DocumentStatus.PENDING, to=DocumentStatus.OCR_RUNNING
            )
            session.execute(
                update(Document)
                .where(Document.id == stored_document)
                .values(
                    created_at=datetime.now(UTC) - timedelta(hours=2),
                    processing_started_at=datetime.now(UTC),
                )
            )
            session.commit()

        with SessionLocal() as session:
            assert DocumentRepository(session).reap_stale(older_than_seconds=60) == 0
            session.commit()

        with SessionLocal() as session:
            doc = session.get(Document, stored_document)
            assert doc is not None
            assert doc.status is DocumentStatus.OCR_RUNNING

    def test_finalize_deadline_uses_processing_started_at_not_created_at(
        self, admin, stored_document
    ) -> None:
        with SessionLocal() as session:
            claim = Claim(
                created_by=admin.id,
                status=ClaimStatus.PENDING,
                created_at=datetime.now(UTC) - timedelta(hours=2),
                processing_started_at=datetime.now(UTC),
            )
            session.add(claim)
            session.flush()
            session.add(
                ClaimDocument(claim_id=claim.id, document_id=stored_document, position=0)
            )
            session.execute(
                update(Document)
                .where(Document.id == stored_document)
                .values(status=DocumentStatus.OCR_RUNNING)
            )
            session.commit()
            claim_id = claim.id

        try:
            finalizer = ClaimFinalizer(
                session_factory=SessionLocal,
                registry=load_registry(),
                finalize_timeout_seconds=3600,
            )
            assert finalizer.try_finalize(claim_id) is False
        finally:
            _delete_claim(claim_id)

    def test_claim_reap_orphaned_respects_processing_epoch(self, admin) -> None:
        with SessionLocal() as session:
            claim = Claim(
                created_by=admin.id,
                status=ClaimStatus.PROCESSING,
                created_at=datetime.now(UTC) - timedelta(hours=2),
                processing_started_at=datetime.now(UTC),
            )
            session.add(claim)
            session.commit()
            claim_id = claim.id

        try:
            with SessionLocal() as session:
                assert (
                    ClaimRepository(session).reap_orphaned(
                        older_than_seconds=get_settings().claim_finalize_timeout_seconds
                    )
                    == 0
                )
                session.commit()

            with SessionLocal() as session:
                assert session.get(Claim, claim_id).status is ClaimStatus.PROCESSING
        finally:
            _delete_claim(claim_id)
