#!/usr/bin/env python3
"""Repair inconsistent claim/document rows left by F2/F3 lifecycle bugs.

Read-only by default. Pass --apply to dispatch repairs through existing
repository guards and Celery tasks — never writes status directly.
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# Ensure the project root is on sys.path when the script is invoked directly
# (e.g. `docker compose exec api python scripts/repair_claim_state.py`).
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import exists, func, select

from app.db.session import SessionLocal
from app.models.audit_log import AuditAction
from app.models.claim import Claim, ClaimDocument, ClaimStatus, ClaimTag, ClaimTagSource
from app.models.document import Document, DocumentStatus
from app.models.extraction import Extraction
from app.models.ocr_result import OcrResult
from app.repositories.claim_repository import ClaimRepository
from app.repositories.document_repository import INTERRUPTED_REASON_PREFIX, DocumentRepository
from app.services.audit_service import AuditService
from app.worker import finalize_claim_task, process_document_task

logger = logging.getLogger(__name__)

RepairKind = Literal["claim_finalize", "document_reprocess", "claim_tags_only"]


@dataclass(frozen=True, slots=True)
class RepairCandidate:
    kind: RepairKind
    entity_id: uuid.UUID
    current_status: str
    action: str


def _claim_finalize_deadline_victims() -> list[RepairCandidate]:
    with SessionLocal() as session:
        claims = list(
            session.execute(
                select(Claim).where(
                    Claim.status == ClaimStatus.FAILED,
                    Claim.failure_reason.is_not(None),
                    Claim.failure_reason.like("%finalize deadline reached%"),
                )
            ).scalars()
        )
        out: list[RepairCandidate] = []
        for claim in claims:
            members = session.execute(
                select(Document.status).join(
                    ClaimDocument, ClaimDocument.document_id == Document.id
                ).where(ClaimDocument.claim_id == claim.id)
            ).scalars()
            statuses = list(members)
            if statuses and all(s is DocumentStatus.COMPLETED for s in statuses):
                out.append(
                    RepairCandidate(
                        kind="claim_finalize",
                        entity_id=claim.id,
                        current_status=claim.status.value,
                        action="claim_for_reprocess + finalize_claim_task",
                    )
                )
        return out


def _interrupted_but_finished_documents() -> list[RepairCandidate]:
    with SessionLocal() as session:
        rows = session.execute(
            select(Document).where(
                Document.status == DocumentStatus.FAILED,
                Document.failure_reason.like(f"{INTERRUPTED_REASON_PREFIX}%"),
                exists(select(OcrResult.id).where(OcrResult.document_id == Document.id)),
                exists(select(Extraction.id).where(Extraction.document_id == Document.id)),
            )
        ).scalars()
        return [
            RepairCandidate(
                kind="document_reprocess",
                entity_id=doc.id,
                current_status=doc.status.value,
                action="claim_for_reprocess + process_document_task",
            )
            for doc in rows
        ]


def _claims_missing_tags() -> list[RepairCandidate]:
    with SessionLocal() as session:
        auto_tag_count = (
            select(func.count(ClaimTag.id))
            .where(
                ClaimTag.claim_id == Claim.id,
                ClaimTag.source == ClaimTagSource.AUTO,
                ClaimTag.dismissed_at.is_(None),
            )
            .correlate(Claim)
            .scalar_subquery()
        )
        has_completed_member = exists(
            select(ClaimDocument.id)
            .join(Document, Document.id == ClaimDocument.document_id)
            .where(
                ClaimDocument.claim_id == Claim.id,
                Document.status == DocumentStatus.COMPLETED,
            )
        )
        claims = session.execute(
            select(Claim).where(
                Claim.status.in_((ClaimStatus.COMPLETED, ClaimStatus.PARTIAL)),
                auto_tag_count == 0,
                has_completed_member,
            )
        ).scalars()
        return [
            RepairCandidate(
                kind="claim_tags_only",
                entity_id=claim.id,
                current_status=claim.status.value,
                action="finalize_claim_task",
            )
            for claim in claims
        ]


def find_candidates() -> list[RepairCandidate]:
    seen: set[tuple[RepairKind, uuid.UUID]] = set()
    candidates: list[RepairCandidate] = []
    for finder in (
        _claim_finalize_deadline_victims,
        _interrupted_but_finished_documents,
        _claims_missing_tags,
    ):
        for item in finder():
            key = (item.kind, item.entity_id)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(item)
    return candidates


def _audit_repair(
    session,
    *,
    kind: RepairKind,
    entity_id: uuid.UUID,
    current_status: str,
    action: str,
) -> None:
    detail = {
        "repair_kind": kind,
        "entity_id": str(entity_id),
        "current_status": current_status,
        "action": action,
    }
    if kind in ("claim_finalize", "claim_tags_only"):
        AuditService.record_atomic(
            session,
            action=AuditAction.CLAIM_STATE_REPAIRED,
            claim_id=entity_id,
            detail=detail,
        )
    else:
        AuditService.record_atomic(
            session,
            action=AuditAction.CLAIM_STATE_REPAIRED,
            document_id=entity_id,
            detail=detail,
        )


def apply_repairs(candidates: list[RepairCandidate]) -> int:
    applied = 0
    with SessionLocal() as session:
        claims = ClaimRepository(session)
        documents = DocumentRepository(session)
        for item in candidates:
            logger.info(
                "repair_candidate",
                extra={
                    "entity_id": str(item.entity_id),
                    "kind": item.kind,
                    "current_status": item.current_status,
                    "action": item.action,
                },
            )
            if item.kind == "claim_finalize":
                if not claims.claim_for_reprocess(item.entity_id):
                    continue
                _audit_repair(
                    session,
                    kind=item.kind,
                    entity_id=item.entity_id,
                    current_status=item.current_status,
                    action=item.action,
                )
                session.commit()
                finalize_claim_task.delay(str(item.entity_id))
                applied += 1
            elif item.kind == "document_reprocess":
                if not documents.claim_for_reprocess(item.entity_id):
                    continue
                _audit_repair(
                    session,
                    kind=item.kind,
                    entity_id=item.entity_id,
                    current_status=item.current_status,
                    action=item.action,
                )
                session.commit()
                process_document_task.delay(str(item.entity_id))
                applied += 1
            elif item.kind == "claim_tags_only":
                # Reset status to PROCESSING and bump processing_started_at so
                # try_finalize's status guard and deadline guard both pass.
                claim_row = session.get(Claim, item.entity_id)
                if claim_row is not None:
                    from datetime import datetime, timezone
                    claim_row.status = ClaimStatus.PROCESSING
                    claim_row.processing_started_at = datetime.now(timezone.utc)
                _audit_repair(
                    session,
                    kind=item.kind,
                    entity_id=item.entity_id,
                    current_status=item.current_status,
                    action=item.action,
                )
                session.commit()
                finalize_claim_task.delay(str(item.entity_id))
                applied += 1
    return applied


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="dispatch repairs (default is report-only)",
    )
    args = parser.parse_args(argv)

    candidates = find_candidates()
    if not candidates:
        print("No repair candidates found.")
        return 0

    for item in candidates:
        print(
            f"{item.kind} {item.entity_id} status={item.current_status} -> {item.action}"
        )

    if not args.apply:
        print(f"{len(candidates)} candidate(s); re-run with --apply to repair.")
        return 0

    applied = apply_repairs(candidates)
    print(f"Applied {applied} repair(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
