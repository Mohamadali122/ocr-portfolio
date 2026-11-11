import ipaddress
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.models.audit_log import AuditAction, AuditLog


def _coerce_ip(value: str | None) -> str | None:
    """Postgres INET rejects anything that is not an address, and the rejection
    aborts the INSERT. An unparseable peer address must cost us the address, not
    the audit row."""
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


class AuditRepository:
    """Insert-only by construction. This class exposes no update and no delete.

    Phase 7 makes that structural with `REVOKE UPDATE, DELETE` on the application
    role, so that a future careless `session.execute(update(AuditLog))` fails at
    the database rather than succeeding quietly.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

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
    ) -> AuditLog:
        entry = AuditLog(
            action=action.value,
            user_id=user_id,
            document_id=document_id,
            claim_id=claim_id,
            ip_address=_coerce_ip(ip_address),
            user_agent=user_agent[:255] if user_agent else None,
            detail=detail,
        )
        self._session.add(entry)
        self._session.flush()
        return entry

    def search(
        self,
        *,
        user_id: uuid.UUID | None = None,
        document_id: uuid.UUID | None = None,
        claim_id: uuid.UUID | None = None,
        action: str | None = None,
        limit: int = 50,
        cursor: tuple[datetime, int] | None = None,
    ) -> list[AuditLog]:
        """Keyset-paginated, most recent first. `cursor` is the (occurred_at, id)
        of the last row the caller already has -- see `AuditLogCursor` for why
        `id` alone cannot stand in for time order here."""
        stmt = select(AuditLog)
        if user_id is not None:
            stmt = stmt.where(AuditLog.user_id == user_id)
        if document_id is not None:
            stmt = stmt.where(AuditLog.document_id == document_id)
        if claim_id is not None:
            stmt = stmt.where(AuditLog.claim_id == claim_id)
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        if cursor is not None:
            stmt = stmt.where(tuple_(AuditLog.occurred_at, AuditLog.id) < cursor)
        stmt = stmt.order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc()).limit(limit)
        return list(self._session.execute(stmt).scalars())
