"""Authentication use-cases. Depends on repositories and `app.core.security`.

Knows nothing about HTTP: it raises domain errors and returns domain objects.
"""

import uuid
from datetime import UTC, datetime

import jwt

from app.core import security
from app.core.config import Settings
from app.core.exceptions import AuthenticationError
from app.core.logging import get_logger
from app.models.audit_log import AuditAction
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.auth import TokenPair
from app.services.audit_service import AuditService

logger = get_logger(__name__)

# One message for every failure mode below. Distinguishing "no such user" from
# "wrong password" hands an attacker a free account enumeration oracle.
_GENERIC_FAILURE = "Invalid email or password."


class AuthService:
    def __init__(
        self,
        *,
        users: UserRepository,
        audit: AuditService,
        settings: Settings,
    ) -> None:
        self._users = users
        self._audit = audit
        self._settings = settings

    def authenticate(
        self,
        *,
        email: str,
        password: str,
        ip_address: str | None,
        user_agent: str | None,
    ) -> TokenPair:
        user = self._users.get_by_email(email)

        if user is None:
            # Equalize the wall-clock cost of the two branches before failing.
            security.waste_time_like_a_verification()
            self._record_failure(None, ip_address, user_agent, reason="unknown_email")
            raise AuthenticationError(_GENERIC_FAILURE)

        if not security.verify_password(password, user.password_hash):
            self._record_failure(user.id, ip_address, user_agent, reason="bad_password")
            raise AuthenticationError(_GENERIC_FAILURE)

        if not user.is_active:
            self._record_failure(user.id, ip_address, user_agent, reason="inactive_account")
            raise AuthenticationError(_GENERIC_FAILURE)

        # Transparent upgrade when the hash predates a parameter increase.
        if security.needs_rehash(user.password_hash):
            user.password_hash = security.hash_password(password)
            logger.info("password_hash_upgraded", user_id=str(user.id))

        user.last_login_at = datetime.now(UTC)
        self._audit.record(
            action=AuditAction.LOGIN_SUCCEEDED,
            user_id=user.id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        logger.info("login_succeeded", user_id=str(user.id), role=user.role.value)
        return self._issue(user)

    def refresh(self, *, refresh_token: str, ip_address: str | None) -> TokenPair:
        user = self._user_from_token(refresh_token, expected_type="refresh")
        self._audit.record(
            action=AuditAction.TOKEN_REFRESHED, user_id=user.id, ip_address=ip_address
        )
        return self._issue(user)

    def user_from_access_token(self, token: str) -> User:
        return self._user_from_token(token, expected_type="access")

    # -- internals ---------------------------------------------------------

    def _issue(self, user: User) -> TokenPair:
        return TokenPair(
            access_token=security.create_token(
                settings=self._settings,
                subject=user.id,
                token_type="access",  # noqa: S106  a discriminator, not a credential
                token_version=user.token_version,
            ),
            refresh_token=security.create_token(
                settings=self._settings,
                subject=user.id,
                token_type="refresh",  # noqa: S106
                token_version=user.token_version,
            ),
            expires_in=self._settings.access_token_ttl_minutes * 60,
        )

    def _user_from_token(self, token: str, *, expected_type: security.TokenType) -> User:
        try:
            payload = security.decode_token(
                settings=self._settings, token=token, expected_type=expected_type
            )
            subject = uuid.UUID(payload["sub"])
        except (jwt.PyJWTError, ValueError, KeyError) as exc:
            # The exception text can name the claim that failed. It never reaches
            # the client, and `redact_processor` scrubs it on the way to the log.
            logger.info("token_rejected", reason=type(exc).__name__, token_type=expected_type)
            raise AuthenticationError("Invalid or expired token.") from exc

        user = self._users.get_by_id(subject)
        if user is None or not user.is_active:
            raise AuthenticationError("Invalid or expired token.")

        if payload["ver"] != user.token_version:
            # The user's credentials were rotated after this token was minted.
            logger.info("token_rejected", reason="stale_token_version", user_id=str(user.id))
            raise AuthenticationError("Invalid or expired token.")

        return user

    def _record_failure(
        self,
        user_id: uuid.UUID | None,
        ip_address: str | None,
        user_agent: str | None,
        *,
        reason: str,
    ) -> None:
        self._audit.record(
            action=AuditAction.LOGIN_FAILED,
            user_id=user_id,
            ip_address=ip_address,
            user_agent=user_agent,
            detail={"reason": reason},
        )
        logger.info("login_failed", reason=reason, user_id=str(user_id) if user_id else None)
