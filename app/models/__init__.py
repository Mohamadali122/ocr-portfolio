"""Every ORM class must be imported here.

Alembic autogenerate diffs `Base.metadata` against the live database. A model
that is never imported is never registered, and its table is silently omitted
from the migration.
"""

from app.models.audit_log import AuditAction, AuditLog
from app.models.claim import Claim, ClaimDocument, ClaimStatus, ClaimTag, ClaimTagSource
from app.models.document import Document, DocumentStatus, DocumentType
from app.models.extraction import Extraction
from app.models.ocr_result import OcrResult
from app.models.user import User, UserRole

__all__ = [
    "AuditAction",
    "AuditLog",
    "Claim",
    "ClaimDocument",
    "ClaimStatus",
    "ClaimTag",
    "ClaimTagSource",
    "Document",
    "DocumentStatus",
    "DocumentType",
    "Extraction",
    "OcrResult",
    "User",
    "UserRole",
]
