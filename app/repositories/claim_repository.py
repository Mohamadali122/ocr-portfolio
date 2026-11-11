import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, Update, func, or_, select, tuple_, update
from sqlalchemy.orm import Session

from app.models.claim import Claim, ClaimDocument, ClaimStatus, ClaimTag, ClaimTagSource
from app.models.document import Document
from app.models.extraction import Extraction

# A restart kills every in-flight Celery task the same way it kills an
# in-process one -- see DocumentRepository's own `_ORPHANABLE`/`reap_orphaned`
# docstrings. A claim stuck PROCESSING with no worker left to finalize it
# is the claim-level version of that same failure mode; `ClaimFinalizer`'s
# deadline (`app.services.claim_service`) is the primary defense, this is
# defense-in-depth for the same reason the document reaper is.
_ORPHANABLE = (ClaimStatus.PENDING, ClaimStatus.PROCESSING)


class ClaimRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _rowcount(self, stmt: Update) -> int:
        return cast("CursorResult[Any]", self._session.execute(stmt)).rowcount

    def add(self, claim: Claim) -> Claim:
        self._session.add(claim)
        self._session.flush()
        return claim

    def get_by_id(self, claim_id: uuid.UUID) -> Claim | None:
        return self._session.get(Claim, claim_id)

    def add_member(
        self, claim_id: uuid.UUID, document_id: uuid.UUID, position: int
    ) -> ClaimDocument:
        member = ClaimDocument(claim_id=claim_id, document_id=document_id, position=position)
        self._session.add(member)
        self._session.flush()
        return member

    def get_members(
        self, claim_id: uuid.UUID
    ) -> list[tuple[ClaimDocument, Document, Extraction | None]]:
        """Member documents in upload order, each paired with its `Document`
        row and (if the AI stage has produced one) its `Extraction` -- the
        exact shape `app.services.claim_merge.merge` and the claim detail/
        extraction endpoints need."""
        stmt = (
            select(ClaimDocument, Document, Extraction)
            .join(Document, Document.id == ClaimDocument.document_id)
            .outerjoin(Extraction, Extraction.document_id == Document.id)
            .where(ClaimDocument.claim_id == claim_id)
            .order_by(ClaimDocument.position)
        )
        return [(row[0], row[1], row[2]) for row in self._session.execute(stmt).all()]

    def claim_for_reprocess(self, claim_id: uuid.UUID) -> bool:
        """Re-arm a terminal claim (COMPLETED/PARTIAL/FAILED) back to
        PROCESSING for a reviewer-requested re-run -- same atomic-guard
        reasoning as `DocumentRepository.claim_for_reprocess`."""
        stmt = (
            update(Claim)
            .where(
                Claim.id == claim_id,
                Claim.status.in_((ClaimStatus.COMPLETED, ClaimStatus.PARTIAL, ClaimStatus.FAILED)),
            )
            .values(
                status=ClaimStatus.PROCESSING,
                failure_reason=None,
                finalized_at=None,
                processing_started_at=datetime.now(UTC),
            )
        )
        return self._rowcount(stmt) == 1

    def finalize(
        self, claim_id: uuid.UUID, *, status: ClaimStatus, failure_reason: str | None
    ) -> None:
        self._session.execute(
            update(Claim)
            .where(Claim.id == claim_id)
            .values(status=status, failure_reason=failure_reason, finalized_at=datetime.now(UTC))
        )

    def apply_patient_identity(
        self,
        claim_id: uuid.UUID,
        *,
        last_name: str | None,
        first_name: str | None,
        dob: date | None,
        run_number: str | None,
    ) -> None:
        self._session.execute(
            update(Claim)
            .where(Claim.id == claim_id)
            .values(
                patient_last_name=last_name,
                patient_first_name=first_name,
                patient_dob=dob,
                run_number=run_number,
            )
        )

    def search(
        self,
        *,
        patient_name: str | None = None,
        run_number: str | None = None,
        dob_from: date | None = None,
        dob_to: date | None = None,
        status: ClaimStatus | None = None,
        limit: int = 50,
        cursor: tuple[datetime, uuid.UUID] | None = None,
    ) -> list[Claim]:
        """Keyset-paginated, newest first -- same shape as
        `DocumentRepository.search`."""
        stmt = select(Claim)
        if status is not None:
            stmt = stmt.where(Claim.status == status)
        if patient_name:
            like = f"{patient_name}%"
            stmt = stmt.where(
                or_(
                    Claim.patient_last_name.ilike(like),
                    Claim.patient_first_name.ilike(like),
                )
            )
        if run_number:
            stmt = stmt.where(Claim.run_number == run_number)
        if dob_from:
            stmt = stmt.where(Claim.patient_dob >= dob_from)
        if dob_to:
            stmt = stmt.where(Claim.patient_dob <= dob_to)
        if cursor is not None:
            stmt = stmt.where(tuple_(Claim.created_at, Claim.id) < cursor)
        stmt = stmt.order_by(Claim.created_at.desc(), Claim.id.desc()).limit(limit)
        return list(self._session.execute(stmt).scalars())

    # -- tags ----------------------------------------------------------

    def get_tags(self, claim_id: uuid.UUID) -> list[ClaimTag]:
        stmt = select(ClaimTag).where(ClaimTag.claim_id == claim_id).order_by(ClaimTag.tag_name)
        return list(self._session.execute(stmt).scalars())

    def get_tag(self, claim_id: uuid.UUID, tag_name: str) -> ClaimTag | None:
        stmt = select(ClaimTag).where(ClaimTag.claim_id == claim_id, ClaimTag.tag_name == tag_name)
        return self._session.execute(stmt).scalar_one_or_none()

    def replace_auto_tags(
        self,
        claim_id: uuid.UUID,
        assignments: list[tuple[str, str, float, list[dict[str, Any]]]],
    ) -> None:
        """Full replace of this claim's `source == auto` rows with the tag
        evaluator's current output -- `assignments` is
        `(tag_name, rule_id, confidence, evidence)` tuples.

        Never touches a `reviewer`-sourced row, and never resurrects a tag a
        reviewer explicitly dismissed (`dismissed_at is not None`) even if the
        current ruleset would fire it again -- a reprocess must not silently
        undo a reviewer's judgment call.
        """
        existing = {tag.tag_name: tag for tag in self.get_tags(claim_id)}
        new_names = {name for name, *_ in assignments}

        for tag_name, tag in existing.items():
            stale_auto = (
                tag.source is ClaimTagSource.AUTO
                and tag.dismissed_at is None
                and tag_name not in new_names
            )
            if stale_auto:
                self._session.delete(tag)

        for tag_name, rule_id, confidence, evidence in assignments:
            current = existing.get(tag_name)
            if current is not None:
                if current.source is ClaimTagSource.REVIEWER or current.dismissed_at is not None:
                    continue
                current.rule_id = rule_id
                current.confidence = confidence
                current.evidence = evidence
                continue
            self._session.add(
                ClaimTag(
                    claim_id=claim_id,
                    tag_name=tag_name,
                    source=ClaimTagSource.AUTO,
                    rule_id=rule_id,
                    confidence=confidence,
                    evidence=evidence,
                )
            )
        self._session.flush()

    def add_reviewer_tag(self, claim_id: uuid.UUID, tag_name: str) -> ClaimTag | None:
        """Returns `None` if the tag is already assigned (auto or reviewer)
        and not dismissed -- the caller (`ClaimService.add_tag`) turns that
        into a 409. Un-dismissing a previously-dismissed tag reassigns it as
        `reviewer`-sourced, since a human decided it applies again."""
        existing = self.get_tag(claim_id, tag_name)
        if existing is not None:
            if existing.dismissed_at is None:
                return None
            existing.source = ClaimTagSource.REVIEWER
            existing.dismissed_at = None
            existing.dismissed_by = None
            self._session.flush()
            return existing
        tag = ClaimTag(
            claim_id=claim_id, tag_name=tag_name, source=ClaimTagSource.REVIEWER, confidence=1.0
        )
        self._session.add(tag)
        self._session.flush()
        return tag

    def dismiss_tag(self, claim_id: uuid.UUID, tag_name: str, *, dismissed_by: uuid.UUID) -> bool:
        """A `reviewer`-sourced tag (the reviewer's own addition) is deleted
        outright -- there is nothing for a later reprocess to respect. An
        `auto` tag is soft-dismissed so `replace_auto_tags` knows not to
        resurrect it."""
        tag = self.get_tag(claim_id, tag_name)
        if tag is None or tag.dismissed_at is not None:
            return False
        if tag.source is ClaimTagSource.REVIEWER:
            self._session.delete(tag)
        else:
            tag.dismissed_at = datetime.now(UTC)
            tag.dismissed_by = dismissed_by
        self._session.flush()
        return True

    def reap_orphaned(self, *, older_than_seconds: int) -> int:
        """Startup sweep, same reasoning as `DocumentRepository.reap_orphaned`.
        Age-gated on `processing_started_at` (falling back to `created_at`) so a
        restart during normal processing does not fail healthy claims."""
        cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
        epoch = func.coalesce(Claim.processing_started_at, Claim.created_at)
        stmt = (
            update(Claim)
            .where(Claim.status.in_(_ORPHANABLE), epoch < cutoff)
            .values(
                status=ClaimStatus.FAILED,
                failure_reason="processing interrupted by application restart",
            )
        )
        return self._rowcount(stmt)
