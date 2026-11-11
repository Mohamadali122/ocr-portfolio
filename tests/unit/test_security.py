import uuid

import jwt
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.security import (
    create_token,
    decode_token,
    hash_password,
    needs_rehash,
    verify_password,
)


class TestPasswordHashing:
    def test_hash_is_argon2id_and_verifies(self) -> None:
        digest = hash_password("correct horse battery staple")
        assert digest.startswith("$argon2id$")
        assert verify_password("correct horse battery staple", digest)

    def test_wrong_password_is_rejected(self) -> None:
        assert not verify_password("wrong", hash_password("right-and-long-enough"))

    def test_salt_makes_identical_passwords_hash_differently(self) -> None:
        assert hash_password("same-password-twice") != hash_password("same-password-twice")

    def test_long_passphrase_is_not_silently_truncated(self) -> None:
        # bcrypt truncates at 72 bytes: two passphrases sharing a 72-byte prefix
        # would verify against each other's hash. Argon2 must not.
        prefix = "x" * 72
        digest = hash_password(prefix + "AAAA")
        assert not verify_password(prefix + "BBBB", digest)

    def test_malformed_hash_is_rejected_not_raised(self) -> None:
        assert not verify_password("anything", "not-a-hash")
        assert needs_rehash("not-a-hash")


class TestTokens:
    def _token(self, settings: Settings, **overrides: object) -> str:
        kwargs: dict = {
            "settings": settings,
            "subject": uuid.uuid4(),
            "token_type": "access",
            "token_version": 0,
        }
        kwargs.update(overrides)
        return create_token(**kwargs)

    def test_round_trip(self, settings: Settings) -> None:
        subject = uuid.uuid4()
        token = self._token(settings, subject=subject)
        payload = decode_token(settings=settings, token=token, expected_type="access")
        assert payload["sub"] == str(subject)
        assert payload["typ"] == "access"
        assert payload["ver"] == 0

    def test_refresh_token_is_rejected_where_an_access_token_is_required(
        self, settings: Settings
    ) -> None:
        # Otherwise a 7-day refresh token works as a bearer credential.
        token = self._token(settings, token_type="refresh")
        with pytest.raises(jwt.InvalidTokenError):
            decode_token(settings=settings, token=token, expected_type="access")

    def test_token_signed_with_another_key_is_rejected(self, settings: Settings) -> None:
        token = self._token(settings)
        other_key = SecretStr("a-different-secret-of-sufficient-length")
        foreign = settings.model_copy(update={"jwt_secret_key": other_key})
        with pytest.raises(jwt.InvalidSignatureError):
            decode_token(settings=foreign, token=token, expected_type="access")

    def test_alg_none_token_is_rejected(self, settings: Settings) -> None:
        # The classic JWT confusion attack: an unsigned token whose header claims
        # alg=none. `decode_token` whitelists algorithms, so this must fail.
        forged = jwt.encode({"sub": str(uuid.uuid4()), "typ": "access"}, key="", algorithm="none")
        with pytest.raises(jwt.PyJWTError):
            decode_token(settings=settings, token=forged, expected_type="access")

    def test_expired_token_is_rejected(self, settings: Settings) -> None:
        short = settings.model_copy(update={"access_token_ttl_minutes": -1})
        token = self._token(short)
        with pytest.raises(jwt.ExpiredSignatureError):
            decode_token(settings=settings, token=token, expected_type="access")

    def test_token_missing_required_claims_is_rejected(self, settings: Settings) -> None:
        bare = jwt.encode(
            {"sub": str(uuid.uuid4())},
            settings.jwt_secret_key.get_secret_value(),
            algorithm=settings.jwt_algorithm,
        )
        with pytest.raises(jwt.MissingRequiredClaimError):
            decode_token(settings=settings, token=bare, expected_type="access")
