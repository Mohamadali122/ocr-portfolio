import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings, get_settings

_REQUIRED = {
    "postgres_user": "u",
    "postgres_password": SecretStr("p"),
    "postgres_db": "d",
    "postgres_app_user": "au",
    "postgres_app_password": SecretStr("ap"),
    "jwt_secret_key": SecretStr("a-secret-that-is-comfortably-over-32-chars"),
}


def _settings(**overrides: object) -> Settings:
    # `_env_file=None` so these don't pick up the real .env's secrets; every
    # required field is supplied explicitly so construction never falls through
    # to environment variables either.
    fields: dict[str, object] = {
        "storage_encryption_keys": SecretStr("v1:some-key-value"),
        **_REQUIRED,
        **overrides,
    }
    return Settings(_env_file=None, **fields)  # type: ignore[call-arg]


def test_access_token_ttl_defaults_to_15_minutes() -> None:
    # HIPAA 164.312(a)(2)(iii) automatic logoff -- see app/core/config.py.
    assert get_settings().access_token_ttl_minutes == 15


class TestStorageKeyRing:
    def test_parses_multiple_ids(self) -> None:
        settings = _settings(storage_encryption_keys=SecretStr("v1:aaa,v0:bbb"))
        assert settings.storage_key_ring == {"v1": "aaa", "v0": "bbb"}

    def test_current_id_must_be_in_the_ring(self) -> None:
        with pytest.raises(ValidationError, match="not one of the ids"):
            _settings(
                storage_encryption_keys=SecretStr("v0:aaa"),
                storage_encryption_key_current="v1",
            )

    def test_malformed_ring_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _settings(storage_encryption_keys=SecretStr("not-a-ring"))

    def test_placeholder_current_key_rejected_outside_local(self) -> None:
        with pytest.raises(ValidationError, match="placeholder"):
            _settings(
                environment="staging",
                redis_url="redis://localhost:6379/0",
                storage_encryption_keys=SecretStr("v1:generate-me-32-random-bytes-base64-encoded"),
            )

    def test_placeholder_current_key_allowed_in_local(self) -> None:
        settings = _settings(
            environment="local",
            storage_encryption_keys=SecretStr("v1:generate-me-32-random-bytes-base64-encoded"),
        )
        assert settings.storage_encryption_key_current == "v1"

    def test_placeholder_in_a_non_current_id_does_not_block_startup(self) -> None:
        # Only the id actually in use has to be real; an old id kept around
        # solely so it can still decrypt legacy documents is not "the" key.
        settings = _settings(
            environment="staging",
            redis_url="redis://localhost:6379/0",
            storage_encryption_keys=SecretStr(
                "v0:generate-me-32-random-bytes-base64-encoded,v1:a-real-generated-key"
            ),
            storage_encryption_key_current="v1",
        )
        assert settings.storage_key_ring["v0"] == "generate-me-32-random-bytes-base64-encoded"


class TestRedisRequiredOutsideLocal:
    def test_missing_redis_url_rejected_outside_local(self) -> None:
        with pytest.raises(ValidationError, match="redis_url"):
            _settings(environment="staging", redis_url=None)

    def test_missing_redis_url_allowed_in_local(self) -> None:
        settings = _settings(environment="local", redis_url=None)
        assert settings.redis_url is None

    def test_redis_url_set_is_accepted_outside_local(self) -> None:
        settings = _settings(environment="production", redis_url="redis://localhost:6379/0")
        assert settings.redis_url == "redis://localhost:6379/0"
