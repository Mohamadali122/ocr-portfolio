"""The audit trail's own read shape.

`AuditLog` is PHI-free by construction -- `detail` never carries it (see the
model docstring) -- so this schema needs no redaction pass, unlike everything
else in `app/schemas`.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


class AuditLogItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: uuid.UUID | None
    document_id: uuid.UUID | None
    claim_id: uuid.UUID | None
    action: str
    ip_address: str | None
    occurred_at: datetime
    detail: dict[str, Any] | None

    @field_validator("ip_address", mode="before")
    @classmethod
    def _stringify_ip(cls, value: object) -> str | None:
        # The column is Postgres INET; SQLAlchemy/psycopg hands back an
        # ipaddress.IPv4Address/IPv6Address, not a str.
        return str(value) if value is not None else None


class AuditLogCursor(BaseModel):
    """Keyset position: the (occurred_at, id) of the last row returned.

    `id` alone is not a safe substitute for time order here -- `occurred_at`'s
    `server_default=func.now()` is the transaction's start time, not statement
    execution time, so a long-running transaction's audit row can commit with a
    higher `id` than a short one that started later. Both fields are compared,
    same as `DocumentCursor`.
    """

    occurred_at: datetime
    id: int


class AuditLogPage(BaseModel):
    items: list[AuditLogItem]
    next_cursor: AuditLogCursor | None = None
