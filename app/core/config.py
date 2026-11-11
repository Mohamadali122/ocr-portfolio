"""Application settings. The single place environment variables are read."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "staging", "production"]

_PLACEHOLDER_SECRET = "generate-me-do-not-ship-this-value"  # noqa: S105  not a credential
_PLACEHOLDER_STORAGE_KEY = "generate-me-32-random-bytes-base64-encoded"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "local-doc-ocr"
    app_version: str = "0.1.0"
    environment: Environment = "local"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    jwt_secret_key: SecretStr
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    # Short by intent. HIPAA 164.312(a)(2)(iii) requires automatic logoff; a
    # 15-minute access token is the enforcement point for an unattended session.
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7
    # Rejected at signup and at seed time. Not enforced against existing hashes.
    min_password_length: int = 12

    postgres_host: str = "db"
    postgres_port: int = 5432
    # Owner role. Alembic connects with this -- DDL and, for now, the CLI's user
    # writes need owner privileges the restricted app role does not have.
    postgres_user: str = Field(min_length=1)
    postgres_password: SecretStr
    postgres_db: str = Field(min_length=1)
    # Runtime role. `app/db/session.py` connects as this one, not the owner --
    # least privilege for the process that terminates untrusted HTTP requests.
    # Created and granted by the `add_app_role` migration, which also revokes
    # UPDATE/DELETE on audit_logs from it: the append-only guarantee enforced by
    # the database, not just by AuditRepository exposing no such method.
    postgres_app_user: str = Field(min_length=1)
    postgres_app_password: SecretStr
    db_pool_size: int = 5
    db_pool_max_overflow: int = 10
    db_echo: bool = False

    # Root for encrypted document storage. Created at startup if absent.
    data_dir: Path = Path("/srv/data")

    # `id:base64key,id:base64key,...` -- a small key ring, not a single key, so
    # rotating in a new one (bump `storage_encryption_key_current`) doesn't
    # brick documents already encrypted under an older id, as long as that id
    # stays listed here. See `app/core/crypto.py::CipherRing`.
    storage_encryption_keys: SecretStr
    # Which id in the ring new writes encrypt under. Existing ciphertext embeds
    # its own id (app/core/crypto.py) and is decrypted under whichever cipher
    # that names, not necessarily this one.
    storage_encryption_key_current: str = "v1"

    # Caps, not ergonomics. A 5000-page PDF is a denial-of-service against a
    # CPU-only OCR host, and an oversized upload is one against memory.
    max_upload_bytes: int = 25 * 1024 * 1024
    max_pdf_pages: int = 50
    # A patient transfer bundle in practice is a handful of files (face sheet,
    # ePCR, PCS, a booking summary); this is a denial-of-service cap, not a
    # realistic count.
    max_claim_files: int = 20

    # TAGS.docx is the tag registry's source of truth (app/services/tag_registry.py);
    # tag_rules.toml sits beside the module it's read by, inside the ./app bind
    # mount, so editing a rule takes effect on `docker compose restart worker api`
    # with no rebuild -- same as editing any other file under app/.
    tags_docx_path: Path = Path("/srv/app/TAGS.docx")
    tag_rules_path: Path = Path(__file__).resolve().parent.parent / "services" / "tag_rules.toml"

    # How long `ClaimFinalizer.try_finalize` (app/services/claim_service.py)
    # waits for every member document to reach a terminal status before
    # finalizing anyway with whatever is done -- the claim-level version of
    # DocumentRepository.reap_orphaned's reasoning, for the case where a
    # member document's own Celery task never reaches the broker at all.
    claim_finalize_timeout_seconds: int = 3600

    # Backstop for a document Celery's own redelivery didn't catch (task never
    # reached the broker, or got purged) -- see DocumentRepository.reap_stale.
    # Must clear worst-case single-document processing time (a large PDF with
    # no text layer, full rasterize + recognize) with real margin, or a
    # still-alive worker's document gets wrongly reaped mid-run.
    document_stale_after_seconds: int = 1800
    document_reap_interval_seconds: int = 300

    # Redis broker visibility timeout (task_acks_late + task_reject_on_worker_lost,
    # app/worker.py): how long an unacked task sits before the broker assumes
    # the worker died and redelivers it to another one. Must also clear
    # worst-case processing time -- if a real still-running task ever exceeds
    # this, it gets redelivered and double-processed while still alive.
    celery_broker_visibility_timeout_seconds: int = 7200

    ocr_lang: str = "en"
    # Textline orientation corrects pages scanned upside-down or sideways (called
    # use_angle_cls before paddleocr 3.x). Faxed face sheets arrive rotated often
    # enough to be worth the ~15% time cost.
    ocr_use_textline_orientation: bool = True
    # Memory bound as much as a quality knob: 600 DPI is 4x the pixels of 300.
    ocr_dpi: int = 300
    # Below this, the document is flagged for the reviewer rather than trusted.
    # Surfaced, never used to reject: a bad scan is still the only scan there is.
    ocr_low_confidence_threshold: float = 0.80

    # LM Studio runs natively on the Windows host for GPU access; the container
    # reaches it at host.docker.internal, never localhost.
    llm_base_url: str = "http://host.docker.internal:1234/v1"
    llm_model: str = "local-model"
    llm_request_timeout_seconds: int = 120
    # Deterministic extraction, not creative writing. Always 0 for this task.
    llm_temperature: float = 0.0
    # Explicit output budget. Unset, LM Studio caps completions at its own
    # default, which truncates the JSON of large documents mid-value
    # ("EOF while parsing" -- the model ran out of room before closing the
    # object).
    llm_max_completion_tokens: int = 4096

    # SOP defaults emitted when a field is absent (marked `sop_default`, never
    # confused with a page reading). The real CBO mailing address and the alias
    # convention are operational -- set them here; the placeholders ship unset.
    sop_cbo_address: str = "CBO"
    sop_alias_name: str = "UNKNOWN"

    # Fixed-window. `redis_url` set -> shared across every worker/process via
    # Redis (`limits`' redis storage backend); unset -> falls back to
    # slowapi/limits' in-memory `memory://`, correct only for a single process.
    # `_require_redis_outside_local` below refuses to boot without it anywhere
    # but local dev, so a multi-worker deploy can't silently fall back to a
    # per-process limiter that doesn't actually limit anything shared.
    redis_url: str | None = None
    rate_limit_login_per_minute: int = 10
    rate_limit_upload_per_minute: int = 30

    @property
    def database_url(self) -> str:
        """Owner role. Alembic only -- see `postgres_app_user` above."""
        return (
            f"postgresql+psycopg://{self.postgres_user}:"
            f"{self.postgres_password.get_secret_value()}@"
            f"{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def app_database_url(self) -> str:
        """Restricted role. `app/db/session.py` connects with this one."""
        return (
            f"postgresql+psycopg://{self.postgres_app_user}:"
            f"{self.postgres_app_password.get_secret_value()}@"
            f"{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @model_validator(mode="after")
    def _forbid_debug_in_production(self) -> "Settings":
        if self.environment == "production" and (self.debug or self.db_echo):
            # db_echo logs bound parameters. Those parameters are PHI.
            raise ValueError("debug and db_echo must be disabled in production")
        return self

    @model_validator(mode="after")
    def _require_a_real_jwt_secret(self) -> "Settings":
        secret = self.jwt_secret_key.get_secret_value()
        if len(secret) < 32:
            raise ValueError("jwt_secret_key must be at least 32 characters")
        if self.environment != "local" and secret == _PLACEHOLDER_SECRET:
            # The value shipped in .env.example. Anyone with the repo can mint
            # an administrator token.
            raise ValueError("jwt_secret_key is still the .env.example placeholder")
        return self

    @model_validator(mode="after")
    def _require_a_real_storage_key(self) -> "Settings":
        # The key still sits in .env beside the ciphertext -- BitLocker, not this
        # check, is the control against device theft. This check only catches the
        # narrower mistake of shipping the unmodified example file: every stored
        # document would be encrypted under a key anyone with the repo also has.
        ring = self.storage_key_ring
        current = ring.get(self.storage_encryption_key_current)
        if current is None:
            raise ValueError(
                f"storage_encryption_key_current {self.storage_encryption_key_current!r} "
                "is not one of the ids in storage_encryption_keys"
            )
        if self.environment != "local" and current == _PLACEHOLDER_STORAGE_KEY:
            raise ValueError("storage_encryption_key is still the .env.example placeholder")
        return self

    @model_validator(mode="after")
    def _require_redis_outside_local(self) -> "Settings":
        # An in-memory rate limiter is per-process: correct for local dev's one
        # uvicorn worker, silently wrong (limits nothing shared) the moment a
        # deploy runs more than one worker or process. Refuse to boot without a
        # shared backend anywhere that might have more than one.
        if self.environment != "local" and not self.redis_url:
            raise ValueError("redis_url is required outside environment=local")
        return self

    @property
    def storage_key_ring(self) -> dict[str, str]:
        """`{key_id: base64_key}`, parsed from `storage_encryption_keys`
        (`id:key,id:key,...`). See `app/core/crypto.py::CipherRing`."""
        ring: dict[str, str] = {}
        for entry in self.storage_encryption_keys.get_secret_value().split(","):
            key_id, sep, value = entry.partition(":")
            if not sep or not key_id.strip() or not value.strip():
                raise ValueError("storage_encryption_keys must be `id:key,id:key,...`")
            ring[key_id.strip()] = value.strip()
        return ring


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor. Import this, never instantiate Settings directly."""
    return Settings()  # values come from env
