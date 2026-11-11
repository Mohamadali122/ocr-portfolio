"""Repair script integration tests."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete

from app.db.session import SessionLocal
from app.models.claim import Claim, ClaimDocument, ClaimStatus, ClaimTag
from app.models.document import Document, DocumentStatus
from app.models.extraction import Extraction
from app.models.ocr_result import OcrResult
from app.models.user import User
from app.repositories.document_repository import INTERRUPTED_REASON_PREFIX
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.repair_claim_state import apply_repairs, find_candidates

pytest_plugins = ["tests.integration.test_processing_started_at"]


def _seed_claim_missing_tags(admin: User, document_id: uuid.UUID) -> uuid.UUID:
    with SessionLocal() as session:
        claim = Claim(
            created_by=admin.id,
            status=ClaimStatus.COMPLETED,
            finalized_at=datetime.now(UTC),
            processing_started_at=datetime.now(UTC),
        )
        session.add(claim)
        session.flush()
        session.add(ClaimDocument(claim_id=claim.id, document_id=document_id, position=0))
        session.commit()
        return claim.id


def _cleanup_claim(claim_id: uuid.UUID) -> None:
    with SessionLocal() as session:
        session.execute(delete(ClaimTag).where(ClaimTag.claim_id == claim_id))
        session.execute(delete(ClaimDocument).where(ClaimDocument.claim_id == claim_id))
        session.execute(delete(Claim).where(Claim.id == claim_id))
        session.commit()


class TestRepairClaimState:
    def test_report_finds_deadline_failed_claim_with_completed_members(
        self, admin, stored_document
    ) -> None:
        with SessionLocal() as session:
            claim = Claim(
                created_by=admin.id,
                status=ClaimStatus.FAILED,
                failure_reason="documents completed: 1/1 (finalize deadline reached)",
                processing_started_at=datetime.now(UTC),
            )
            session.add(claim)
            session.flush()
            session.add(
                ClaimDocument(claim_id=claim.id, document_id=stored_document, position=0)
            )
            session.execute(
                delete(ClaimTag).where(ClaimTag.claim_id == claim.id)
            )
            document = session.get(Document, stored_document)
            document.status = DocumentStatus.COMPLETED  # type: ignore[union-attr]
            session.commit()
            claim_id = claim.id

        try:
            matches = [
                c
                for c in find_candidates()
                if c.kind == "claim_finalize" and c.entity_id == claim_id
            ]
            assert len(matches) == 1
        finally:
            _cleanup_claim(claim_id)

    def test_apply_reprocesses_deadline_failed_claim(self, admin, stored_document) -> None:
        with SessionLocal() as session:
            claim = Claim(
                created_by=admin.id,
                status=ClaimStatus.FAILED,
                failure_reason="documents completed: 1/1 (finalize deadline reached)",
                processing_started_at=datetime.now(UTC),
            )
            session.add(claim)
            session.flush()
            session.add(
                ClaimDocument(claim_id=claim.id, document_id=stored_document, position=0)
            )
            document = session.get(Document, stored_document)
            document.status = DocumentStatus.COMPLETED  # type: ignore[union-attr]
            session.commit()
            claim_id = claim.id

        try:
            candidates = [
                c
                for c in find_candidates()
                if c.entity_id == claim_id and c.kind == "claim_finalize"
            ]
            assert len(candidates) == 1
            assert apply_repairs(candidates) == 1
            remaining = [
                c
                for c in find_candidates()
                if c.entity_id == claim_id and c.kind == "claim_finalize"
            ]
            assert remaining == []
        finally:
            _cleanup_claim(claim_id)

    def test_finds_interrupted_document_with_ocr_and_extraction(
        self, admin, stored_document
    ) -> None:
        with SessionLocal() as session:
            document = session.get(Document, stored_document)
            document.status = DocumentStatus.FAILED  # type: ignore[union-attr]
            document.failure_reason = f"{INTERRUPTED_REASON_PREFIX}: test"  # type: ignore[union-attr]
            session.add(
                OcrResult(
                    document_id=stored_document,
                    raw_text="x",
                    page_texts=[],
                    mean_confidence=1.0,
                    engine_version="test",
                    page_count=1,
                    duration_ms=1,
                )
            )
            session.add(
                Extraction(
                    document_id=stored_document,
                    fields={},
                    llm_model="test",
                    duration_ms=1,
                    repaired=False,
                )
            )
            session.commit()

        matches = [
            c
            for c in find_candidates()
            if c.kind == "document_reprocess" and c.entity_id == stored_document
        ]
        assert len(matches) == 1
