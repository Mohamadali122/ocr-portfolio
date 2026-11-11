"""Claim ingestion (request-scoped) and claim finalize (worker-scoped).

Two classes, deliberately: `ClaimService` mirrors `DocumentService` -- built
per-request with the request's `Session`, one method per endpoint.
`ClaimFinalizer` mirrors `PipelineService` -- built once per worker task
invocation, owns its own session via a `session_factory`, and never touches a
request's session. A claim genuinely has both a request-time API surface and
a worker-time one; splitting them keeps each honest about which session
lifecycle it belongs to, the same distinction `AuditService.record` vs
`record_atomic` exists to enforce for audit writes.

`ClaimService.create` reuses `DocumentService.upload` per file, unchanged --
magic-byte validation, the 25 MB streaming cap, encryption, sha256 dedup, and
its own `DOCUMENT_UPLOADED` audit all apply exactly as they do for a single
upload. One bad file must not sink an otherwise-good bundle: a per-file
failure is collected as a rejection, not raised, and the claim is only
refused outright if every file was rejected.

`ClaimFinalizer.try_finalize` is the join point for the fan-out
`app.worker.finalize_claim_task` polls: it returns `False` (retry) while any
member document is still mid-pipeline, unless `finalize_timeout_seconds` has
elapsed since the claim was created, in which case it finalizes with
whatever is terminal rather than polling forever -- the claim-level version
of `DocumentRepository.reap_orphaned`'s reasoning.
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.exceptions import AllFilesRejectedError, AppError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models.audit_log import AuditAction
from app.models.claim import Claim, ClaimStatus, ClaimTag
from app.models.document import DocumentStatus, DocumentType
from app.models.user import User
from app.repositories.claim_repository import ClaimRepository
from app.repositories.ocr_repository import OcrRepository
from app.services import claim_merge, tagging
from app.services.audit_service import AuditService
from app.services.claim_merge import MemberDocument, MergedExtraction
from app.services.document_service import DocumentService
from app.services.normalization import searchable_dob, searchable_name
from app.services.tag_registry import Registry, TagDefinition, load_registry
from app.services.tagging import ClaimContext, ClaimDocumentText, Evidence

logger = get_logger(__name__)

_NON_TERMINAL = (DocumentStatus.PENDING, DocumentStatus.OCR_RUNNING, DocumentStatus.AI_RUNNING)


@dataclass(frozen=True, slots=True)
class UploadedFile:
    filename: str
    content_type: str | None
    data: bytes


@dataclass(frozen=True, slots=True)
class AcceptedDocument:
    filename: str
    document_id: uuid.UUID
    status: DocumentStatus
    deduplicated: bool


@dataclass(frozen=True, slots=True)
class RejectedFile:
    filename: str
    reason: str


@dataclass(frozen=True, slots=True)
class ClaimCreationResult:
    claim_id: uuid.UUID
    accepted: tuple[AcceptedDocument, ...]
    rejected: tuple[RejectedFile, ...]


class ClaimService:
    def __init__(
        self,
        *,
        session: Session,
        claims: ClaimRepository,
        documents: DocumentService,
        audit: AuditService,
    ) -> None:
        self._session = session
        self._claims = claims
        self._documents = documents
        self._audit = audit

    def create(
        self,
        *,
        files: Sequence[UploadedFile],
        user: User,
        ip_address: str | None,
        user_agent: str | None,
    ) -> ClaimCreationResult:
        accepted: list[AcceptedDocument] = []
        rejected: list[RejectedFile] = []
        for f in files:
            try:
                created = self._documents.upload(
                    data=f.data,
                    filename=f.filename,
                    declared_content_type=f.content_type,
                    doc_type=DocumentType.UNKNOWN,
                    user=user,
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
            except AppError as exc:
                # DocumentService.upload already audited DOCUMENT_UPLOAD_REJECTED
                # for this file, in its own transaction -- see its docstring.
                rejected.append(RejectedFile(filename=f.filename, reason=type(exc).__name__))
                continue
            accepted.append(
                AcceptedDocument(
                    filename=f.filename,
                    document_id=created.id,
                    status=created.status,
                    deduplicated=created.deduplicated,
                )
            )

        if not accepted:
            raise AllFilesRejectedError()

        claim = self._claims.add(
            Claim(created_by=user.id, processing_started_at=datetime.now(UTC))
        )
        position = 0
        seen_document_ids: set[uuid.UUID] = set()
        for doc in accepted:
            # Two uploaded files can resolve to the same `Document` row --
            # genuinely identical bytes (sha256 dedup), or the same file
            # attached twice by mistake. `claim_documents` has a unique
            # (claim_id, document_id) constraint (a claim's member list is a
            # set of distinct documents, not a multiset), so add it once;
            # `doc.deduplicated` still reports honestly for every accepted
            # file, whichever position(s) it was uploaded at.
            if doc.document_id in seen_document_ids:
                continue
            seen_document_ids.add(doc.document_id)
            self._claims.add_member(claim.id, doc.document_id, position)
            position += 1

        AuditService.record_atomic(
            self._session,
            action=AuditAction.CLAIM_CREATED,
            user_id=user.id,
            claim_id=claim.id,
            ip_address=ip_address,
            user_agent=user_agent,
            detail={"document_count": len(accepted), "rejected_count": len(rejected)},
        )
        self._session.commit()
        logger.info(
            "claim_created",
            claim_id=str(claim.id),
            document_count=len(accepted),
            rejected_count=len(rejected),
        )
        return ClaimCreationResult(
            claim_id=claim.id, accepted=tuple(accepted), rejected=tuple(rejected)
        )

    def get(self, claim_id: uuid.UUID) -> Claim:
        claim = self._claims.get_by_id(claim_id)
        if claim is None:
            raise NotFoundError("No such claim.")
        return claim

    def members(self, claim_id: uuid.UUID) -> list[tuple[uuid.UUID, int, DocumentStatus]]:
        self.get(claim_id)
        return [
            (doc.id, cd.position, doc.status) for cd, doc, _ in self._claims.get_members(claim_id)
        ]

    def merged_extraction(self, claim_id: uuid.UUID) -> MergedExtraction:
        self.get(claim_id)
        members = self._claims.get_members(claim_id)
        member_inputs = [
            MemberDocument(
                position=cd.position,
                document_id=doc.id,
                doc_type=doc.doc_type,
                extraction=extraction,
            )
            for cd, doc, extraction in members
        ]
        return claim_merge.merge(member_inputs)

    def tags(self, claim_id: uuid.UUID) -> tuple[list[ClaimTag], list[TagDefinition]]:
        self.get(claim_id)
        assigned = self._claims.get_tags(claim_id)
        manual = list(load_registry().manual_review)
        return assigned, manual

    def add_tag(
        self, claim_id: uuid.UUID, tag_name: str, *, user: User, ip_address: str | None
    ) -> ClaimTag:
        self.get(claim_id)
        definition = load_registry().by_key(tag_name)
        if definition is None:
            raise NotFoundError("Unknown tag.")
        tag = self._claims.add_reviewer_tag(claim_id, definition.name)
        if tag is None:
            raise ConflictError("This tag is already assigned to the claim.")
        AuditService.record_atomic(
            self._session,
            action=AuditAction.CLAIM_TAG_ADDED,
            user_id=user.id,
            claim_id=claim_id,
            ip_address=ip_address,
            detail={"tag": definition.name},
        )
        self._session.commit()
        return tag

    def dismiss_tag(
        self, claim_id: uuid.UUID, tag_name: str, *, user: User, ip_address: str | None
    ) -> None:
        self.get(claim_id)
        definition = load_registry().by_key(tag_name)
        canonical_name = definition.name if definition is not None else tag_name
        removed = self._claims.dismiss_tag(claim_id, canonical_name, dismissed_by=user.id)
        if not removed:
            raise NotFoundError("Tag is not assigned to this claim.")
        AuditService.record_atomic(
            self._session,
            action=AuditAction.CLAIM_TAG_DISMISSED,
            user_id=user.id,
            claim_id=claim_id,
            ip_address=ip_address,
            detail={"tag": canonical_name},
        )
        self._session.commit()


def _evidence_json(evidence: Evidence) -> dict[str, Any]:
    return {
        "kind": evidence.kind,
        "field": evidence.field,
        "document_id": str(evidence.document_id) if evidence.document_id is not None else None,
        "page": evidence.page,
        "snippet": evidence.snippet,
    }


class ClaimFinalizer:
    """Worker-scoped. Constructed once per `app.worker.finalize_claim_task`
    invocation, exactly like `PipelineService` -- see the module docstring."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        registry: Registry,
        finalize_timeout_seconds: int,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._finalize_timeout_seconds = finalize_timeout_seconds

    def try_finalize(self, claim_id: uuid.UUID) -> bool:
        """`True` once the claim has reached a terminal status (or there was
        nothing to finalize -- a missing or memberless claim). `False` means
        at least one member document is still mid-pipeline and the deadline
        has not passed; `app.worker.finalize_claim_task` retries."""
        with self._session_factory() as session:
            claims = ClaimRepository(session)
            # ponytail: per-claim row lock is cheap; no advisory-lock scheme needed
            # unless claim volume grows by orders of magnitude.
            claim = session.get(Claim, claim_id, with_for_update=True)
            if claim is None:
                return True
            members = claims.get_members(claim_id)
            if not members:
                return True

            if claim.status not in (ClaimStatus.PENDING, ClaimStatus.PROCESSING):
                return True

            statuses = [doc.status for _, doc, _ in members]
            still_running = any(status in _NON_TERMINAL for status in statuses)
            processing_epoch = claim.processing_started_at or claim.created_at
            deadline_passed = datetime.now(UTC) - processing_epoch > timedelta(
                seconds=self._finalize_timeout_seconds
            )
            if still_running and not deadline_passed:
                return False

            completed = sum(1 for status in statuses if status is DocumentStatus.COMPLETED)
            if completed == len(members):
                new_status = ClaimStatus.COMPLETED
            elif completed == 0:
                new_status = ClaimStatus.FAILED
            else:
                new_status = ClaimStatus.PARTIAL

            member_inputs = [
                MemberDocument(
                    position=cd.position,
                    document_id=doc.id,
                    doc_type=doc.doc_type,
                    extraction=extraction,
                )
                for cd, doc, extraction in members
            ]
            merged = claim_merge.merge(member_inputs)

            ocr_repo = OcrRepository(session)
            documents_text: list[ClaimDocumentText] = []
            for cd, doc, extraction in members:
                if extraction is None:
                    continue
                ocr = ocr_repo.get_by_document_id(doc.id)
                if ocr is None:
                    continue
                documents_text.append(
                    ClaimDocumentText(
                        document_id=doc.id,
                        position=cd.position,
                        doc_type=doc.doc_type,
                        raw_text=ocr.raw_text,
                        page_texts=ocr.page_texts,
                    )
                )
            documents_text.sort(key=lambda d: d.position)

            doc_type_unresolved = any(
                status not in (DocumentStatus.COMPLETED, DocumentStatus.EXTRACTION_FAILED)
                for status in statuses
            )
            ctx = ClaimContext(
                merged=merged,
                doc_types_present=frozenset(doc.doc_type for _, doc, _ in members),
                documents=tuple(documents_text),
                doc_types_complete=not doc_type_unresolved,
            )
            try:
                assignments: list[tagging.TagAssignment] | None = tagging.evaluate(
                    self._registry, ctx
                )
            except tagging.RuleError:
                logger.error("claim_tag_rule_error", claim_id=str(claim_id), exc_info=True)
                assignments = None

            if assignments is not None:
                claims.replace_auto_tags(
                    claim_id,
                    [
                        (
                            a.tag_name,
                            a.rule_id,
                            a.confidence,
                            [_evidence_json(e) for e in a.evidence],
                        )
                        for a in assignments
                    ],
                )
            claims.apply_patient_identity(
                claim_id,
                last_name=searchable_name(
                    merged.fields["last_name"].typed_value, merged.fields["last_name"].source
                ),
                first_name=searchable_name(
                    merged.fields["first_name"].typed_value, merged.fields["first_name"].source
                ),
                dob=searchable_dob(merged.fields["dob"].typed_value, merged.fields["dob"].source),
                run_number=merged.fields["run_number"].typed_value,
            )

            reason = None
            if new_status is not ClaimStatus.COMPLETED:
                reason = f"documents completed: {completed}/{len(members)}"
                if still_running and deadline_passed:
                    reason += " (finalize deadline reached)"
            claims.finalize(claim_id, status=new_status, failure_reason=reason)

            AuditService.record_atomic(
                session,
                action=AuditAction.CLAIM_TAGS_ASSIGNED,
                claim_id=claim_id,
                detail={
                    "tags": [a.tag_name for a in assignments] if assignments is not None else [],
                    "count": len(assignments) if assignments is not None else 0,
                    "status": new_status.value,
                    "tags_skipped": assignments is None,
                },
            )
            session.commit()

        logger.info(
            "claim_finalized",
            claim_id=str(claim_id),
            status=new_status.value,
            tags=len(assignments) if assignments is not None else 0,
        )
        return True
