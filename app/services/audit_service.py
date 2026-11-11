"""Audit writes, in two flavours. The distinction is not cosmetic.

`record` commits in its own transaction. Use it for events that must survive the
request rolling back. A failed login raises `AuthenticationError`, which unwinds
through `get_db`, which rolls back -- taking the audit row with it. The one event
an auditor most needs is the one the request transaction is guaranteed to
discard. So it does not share that transaction. The trade is deliberate: an
attempt that later rolls back still leaves a row. The attempt happened.

`record_atomic` joins the caller's transaction. Use it for success events that
reference a row created in that same transaction. An independent commit cannot
see an uncommitted `documents` row, and a `DOCUMENT_UPLOADED` entry that outlives
a rolled-back upload is a lie.

Getting this backwards is silent: `record` swallows its own failures by design,
so a broken audit write returns 201 to the client and loses the trail.
"""

import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.audit_log import AuditAction
from app.repositories.audit_repository import AuditRepository

__all__ = ["AuditService"]

logger = get_logger(__name__)


class AuditService:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def record(
        self,
        *,
        action: AuditAction,
        user_id: uuid.UUID | None = None,
        document_id: uuid.UUID | None = None,
        claim_id: uuid.UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """`detail` carries identifiers, enums, and reasons. Never PHI."""
        try:
            with self._session_factory() as session:
                AuditRepository(session).record(
                    action=action,
                    user_id=user_id,
                    document_id=document_id,
                    claim_id=claim_id,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    detail=detail,
                )
                session.commit()
        except Exception:
            # A failed audit write must never mask the operation that triggered
            # it, nor abort a legitimate request. It must be loud.
            logger.error("audit_write_failed", action=action.value, exc_info=True)

    @staticmethod
    def record_atomic(
        session: Session,
        *,
        action: AuditAction,
        user_id: uuid.UUID | None = None,
        document_id: uuid.UUID | None = None,
        claim_id: uuid.UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Join the caller's transaction. Commits or rolls back with it.

        No exception swallowing: if this write fails, the operation it describes
        must fail too. An unauditable success is not a success.
        """
        AuditRepository(session).record(
            action=action,
            user_id=user_id,
            document_id=document_id,
            claim_id=claim_id,
            ip_address=ip_address,
            user_agent=user_agent,
            detail=detail,
        )
