"""Password hashing and JWT issue/verify. No database, no HTTP, no business rules."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import Settings

TokenType = Literal["access", "refresh"]

# argon2id. Parameters are the argon2-cffi defaults, stated explicitly so that
# raising them later is a visible diff rather than a library upgrade.
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,  # 64 MiB
    parallelism=4,
    hash_len=32,
    salt_len=16,
)

# Verified against when the supplied email matches no user, so that a failed login
# costs the same wall-clock time whether or not the account exists. Without this,
# response latency enumerates valid accounts.
_DUMMY_HASH = _hasher.hash("bind-the-timing-not-the-truth")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def waste_time_like_a_verification() -> None:
    """Burn the cost of one Argon2 verification. Call when the user was not found."""
    verify_password("bind-the-timing-not-the-truth", _DUMMY_HASH)


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash used weaker parameters than we now require."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def create_token(
    *,
    settings: Settings,
    subject: uuid.UUID,
    token_type: TokenType,
    token_version: int,
) -> str:
    now = datetime.now(UTC)
    lifetime = (
        timedelta(minutes=settings.access_token_ttl_minutes)
        if token_type == "access"  # noqa: S105  a discriminator, not a credential
        else timedelta(days=settings.refresh_token_ttl_days)
    )
    payload: dict[str, Any] = {
        "sub": str(subject),
        "typ": token_type,
        # `ver` pins the token to a generation of the user row. Bumping
        # users.token_version invalidates every token that user holds, without a
        # denylist table.
        "ver": token_version,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "nbf": now,
        "exp": now + lifetime,
    }
    return jwt.encode(
        payload, settings.jwt_secret_key.get_secret_value(), algorithm=settings.jwt_algorithm
    )


def decode_token(*, settings: Settings, token: str, expected_type: TokenType) -> dict[str, Any]:
    """Decode and validate. Raises `jwt.PyJWTError` on any failure.

    `algorithms` is a whitelist of one: passing the header's `alg` back to the
    decoder is the classic JWT confusion attack.
    """
    payload: dict[str, Any] = jwt.decode(
        token,
        settings.jwt_secret_key.get_secret_value(),
        algorithms=[settings.jwt_algorithm],
        options={"require": ["exp", "iat", "nbf", "sub", "typ", "ver", "jti"]},
    )
    if payload["typ"] != expected_type:
        # A refresh token presented as a bearer credential would otherwise grant
        # a 7-day access token.
        raise jwt.InvalidTokenError("unexpected token type")
    return payload
