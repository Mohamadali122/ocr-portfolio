import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import AuditServiceDep, DbDep, client_ip, require_role
from app.models.audit_log import AuditAction
from app.models.user import User, UserRole
from app.repositories.audit_repository import AuditRepository
from app.schemas.audit_log import AuditLogCursor, AuditLogItem, AuditLogPage

router = APIRouter(prefix="/audit-logs", tags=["audit-logs"])


@router.get("", response_model=AuditLogPage)
def search_audit_logs(
    user: Annotated[User, Depends(require_role(UserRole.ADMIN, UserRole.AUDITOR))],
    db: DbDep,
    audit: AuditServiceDep,
    ip: Annotated[str | None, Depends(client_ip)],
    user_id: Annotated[uuid.UUID | None, Query()] = None,
    document_id: Annotated[uuid.UUID | None, Query()] = None,
    claim_id: Annotated[uuid.UUID | None, Query()] = None,
    action: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor_occurred_at: Annotated[datetime | None, Query()] = None,
    cursor_id: Annotated[int | None, Query()] = None,
) -> AuditLogPage:
    """The compliance trail itself. `AuditLog` is PHI-free by construction (see
    the model docstring), so unlike every other read in this system this one
    needs no redaction -- only a role gate. Restricted to admin/auditor;
    reading it is still audited, same convention as every other disclosure.
    """
    cursor = (
        (cursor_occurred_at, cursor_id)
        if cursor_occurred_at is not None and cursor_id is not None
        else None
    )
    rows = AuditRepository(db).search(
        user_id=user_id,
        document_id=document_id,
        claim_id=claim_id,
        action=action,
        limit=limit + 1,  # one extra row tells us whether a next page exists
        cursor=cursor,
    )

    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = AuditLogCursor(occurred_at=last.occurred_at, id=last.id)

    audit.record(
        action=AuditAction.AUDIT_LOG_VIEWED,
        user_id=user.id,
        ip_address=ip,
        detail={"count": len(rows)},
    )
    return AuditLogPage(
        items=[AuditLogItem.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )
