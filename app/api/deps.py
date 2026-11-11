"""Dependency wiring. The only module that binds concrete adapters to services."""

from collections.abc import Callable
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.crypto import CipherRing, get_cipher
from app.core.exceptions import AuthenticationError, AuthorizationError
from app.core.net import resolve_client_ip
from app.db.session import SessionLocal, get_db
from app.llm.base import LlmProvider
from app.llm.openai_compat import OpenAiCompatProvider
from app.models.user import User, UserRole
from app.ocr.base import OcrEngine
from app.ocr.paddle_engine import PaddleOcrEngine
from app.repositories.claim_repository import ClaimRepository
from app.repositories.document_repository import DocumentRepository
from app.repositories.user_repository import UserRepository
from app.services.audit_service import AuditService
from app.services.auth_service import AuthService
from app.services.claim_service import ClaimService
from app.services.document_service import DocumentService
from app.services.extraction_service import ExtractionService
from app.services.patient_grouping_service import PatientGroupingService
from app.storage.base import StorageBackend
from app.storage.encrypted_local import EncryptedLocalStorage

# auto_error=False so a missing header raises our AuthenticationError, producing
# the same PHI-free body shape as every other error in the application.
_bearer = HTTPBearer(auto_error=False)

SettingsDep = Annotated[Settings, Depends(get_settings)]
DbDep = Annotated[Session, Depends(get_db)]


def get_user_repository(db: DbDep) -> UserRepository:
    return UserRepository(db)


def get_audit_service() -> AuditService:
    # Its own session factory, not the request's session. See app/services/audit_service.py.
    return AuditService(SessionLocal)


def get_auth_service(
    settings: SettingsDep,
    users: Annotated[UserRepository, Depends(get_user_repository)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> AuthService:
    return AuthService(users=users, audit=audit, settings=settings)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
AuditServiceDep = Annotated[AuditService, Depends(get_audit_service)]


def get_storage(settings: SettingsDep) -> StorageBackend:
    """The only place the concrete storage adapter is named.

    Swapping to S3 or a KMS-backed store changes this function and nothing else:
    `DocumentService` sees only the protocol.
    """
    return EncryptedLocalStorage(root=settings.data_dir / "documents", cipher=get_cipher())


def get_document_service(
    settings: SettingsDep,
    db: DbDep,
    storage: Annotated[StorageBackend, Depends(get_storage)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> DocumentService:
    return DocumentService(
        session=db,
        documents=DocumentRepository(db),
        storage=storage,
        cipher=get_cipher(),
        audit=audit,
        settings=settings,
    )


DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]
CipherDep = Annotated[CipherRing, Depends(get_cipher)]


def get_patient_grouping_service(db: DbDep) -> PatientGroupingService:
    return PatientGroupingService(DocumentRepository(db))


PatientGroupingServiceDep = Annotated[PatientGroupingService, Depends(get_patient_grouping_service)]


def get_claim_repository(db: DbDep) -> ClaimRepository:
    return ClaimRepository(db)


def get_claim_service(
    db: DbDep,
    claims: Annotated[ClaimRepository, Depends(get_claim_repository)],
    documents: DocumentServiceDep,
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> ClaimService:
    return ClaimService(session=db, claims=claims, documents=documents, audit=audit)


ClaimServiceDep = Annotated[ClaimService, Depends(get_claim_service)]


@lru_cache(maxsize=1)
def get_ocr_engine() -> OcrEngine:
    """One engine for the process.

    PaddleOCR loads three models on first `recognize`. Constructing a new one per
    request would reload them every time. The adapter serialises access with its
    own lock, so a single shared instance is correct as well as cheap.

    This is the only place the concrete engine is named.
    """
    settings = get_settings()
    return PaddleOcrEngine(
        lang=settings.ocr_lang,
        use_textline_orientation=settings.ocr_use_textline_orientation,
    )


@lru_cache(maxsize=1)
def get_llm_provider() -> LlmProvider:
    """One provider for the process. The only place the concrete adapter is named."""
    settings = get_settings()
    return OpenAiCompatProvider(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout_seconds=settings.llm_request_timeout_seconds,
        max_tokens=settings.llm_max_completion_tokens,
    )


def get_extraction_service(
    settings: SettingsDep,
    provider: Annotated[LlmProvider, Depends(get_llm_provider)],
) -> ExtractionService:
    return ExtractionService(
        provider,
        temperature=settings.llm_temperature,
        cbo_address=settings.sop_cbo_address,
        alias_name=settings.sop_alias_name,
    )


def client_ip(request: Request) -> str | None:
    """See `app.core.net.resolve_client_ip` for the trust rule."""
    peer = request.client.host if request.client else None
    return resolve_client_ip(peer, request.headers.get("x-forwarded-for"))


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    auth: AuthServiceDep,
) -> User:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Missing bearer token.")
    return auth.user_from_access_token(credentials.credentials)


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(*allowed: UserRole) -> Callable[[User], User]:
    """Route guard. Unused in the pilot -- one admin -- and present so that Phase 8
    adds a role check by decorating a route, not by rewriting the auth layer."""

    def _guard(user: CurrentUser) -> User:
        if user.role not in allowed:
            raise AuthorizationError()
        return user

    return _guard
