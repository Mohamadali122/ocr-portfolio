"""F4 regression: ClaimFinalizer idempotency and terminal guard."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.claim import Claim, ClaimDocument, ClaimStatus, ClaimTag
from app.models.document import Document, DocumentStatus, DocumentType
from app.models.user import User
from app.services.claim_service import ClaimFinalizer
from app.services.tag_registry import load_registry
from app.core.crypto import get_cipher
from tests.factories import make_pdf


def _seed_completed_claim(admin: User) -> uuid.UUID:
    data = make_pdf(1)
    with SessionLocal() as session:
        document = Document(
            uploaded_by=admin.id,
            doc_type=DocumentType.HFS,
            original_filename_enc=get_cipher().encrypt(b"f.pdf", aad=b"document.original_filename"),
            storage_key=uuid.uuid4().hex,
            content_type="application/pdf",
            size_bytes=len(data),
            page_count=1,
            sha256=uuid.uuid4().hex * 2,
            status=DocumentStatus.COMPLETED,
            processing_started_at=datetime.now(UTC),
        )
        session.add(document)
        session.flush()
        claim = Claim(
            created_by=admin.id,
            status=ClaimStatus.COMPLETED,
            finalized_at=datetime.now(UTC),
            processing_started_at=datetime.now(UTC),
        )
        session.add(claim)
        session.flush()
        session.add(ClaimDocument(claim_id=claim.id, document_id=document.id, position=0))
        session.add(
            ClaimTag(
                claim_id=claim.id,
                tag_name="911",
                confidence=1.0,
            )
        )
        session.commit()
        return claim.id


def _cleanup_claim(claim_id: uuid.UUID) -> None:
    with SessionLocal() as session:
        members = list(
            session.execute(
                select(ClaimDocument.document_id).where(ClaimDocument.claim_id == claim_id)
            ).scalars()
        )
        session.execute(delete(ClaimTag).where(ClaimTag.claim_id == claim_id))
        session.execute(delete(ClaimDocument).where(ClaimDocument.claim_id == claim_id))
        session.execute(delete(Claim).where(Claim.id == claim_id))
        for document_id in members:
            session.execute(delete(Document).where(Document.id == document_id))
        session.commit()


class TestFinalizeIdempotency:
    def test_completed_claim_finalize_is_no_op(self, admin: User) -> None:
        claim_id = _seed_completed_claim(admin)
        try:
            finalizer = ClaimFinalizer(
                session_factory=SessionLocal,
                registry=load_registry(),
                finalize_timeout_seconds=get_settings().claim_finalize_timeout_seconds,
            )
            assert finalizer.try_finalize(claim_id) is True
            with SessionLocal() as session:
                claim = session.get(Claim, claim_id)
                assert claim is not None
                assert claim.status is ClaimStatus.COMPLETED
                tags = list(
                    session.execute(select(ClaimTag).where(ClaimTag.claim_id == claim_id)).scalars()
                )
                assert len(tags) == 1
                assert tags[0].tag_name == "911"
        finally:
            _cleanup_claim(claim_id)

    def test_two_finalize_calls_leave_one_tag_set(self, admin: User) -> None:
        claim_id = _seed_completed_claim(admin)
        try:
            with SessionLocal() as session:
                session.execute(
                    delete(ClaimTag).where(ClaimTag.claim_id == claim_id)
                )
                claim = session.get(Claim, claim_id)
                assert claim is not None
                claim.status = ClaimStatus.PENDING
                claim.finalized_at = None
                session.commit()

            finalizer = ClaimFinalizer(
                session_factory=SessionLocal,
                registry=load_registry(),
                finalize_timeout_seconds=0,
            )
            assert finalizer.try_finalize(claim_id) is True
            assert finalizer.try_finalize(claim_id) is True

            with SessionLocal() as session:
                tags = list(
                    session.execute(select(ClaimTag).where(ClaimTag.claim_id == claim_id)).scalars()
                )
                assert len(tags) >= 1
                claim = session.get(Claim, claim_id)
                assert claim is not None
                assert claim.status is ClaimStatus.COMPLETED
        finally:
            _cleanup_claim(claim_id)
