"""Application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.v1 import admin, audit_logs, auth, claims, documents, health
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestContextMiddleware
from app.core.rate_limit import register_rate_limiting
from app.db.session import SessionLocal
from app.repositories.claim_repository import ClaimRepository
from app.services.pipeline_service import reap_orphaned_documents
from app.services.tag_registry import load_registry

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    # Defense-in-depth, not the primary recovery path now that dispatch is a
    # Celery task: Celery's own redelivery covers a worker dying mid-task (and
    # `PipelineService.claim()` makes redelivery idempotent), but not a
    # document stuck PENDING because `.delay()` itself failed to reach the
    # broker, or a task got purged. Both leave a document *_RUNNING or PENDING
    # forever with nothing to move it -- fail it now, or the client polls
    # forever. Cheap enough (one `UPDATE ... WHERE status IN (...)`) to keep as
    # a boot-time sweep regardless.
    #
    # Best-effort: a database that is not up yet must not stop the app from
    # starting and reporting itself unready.
    try:
        reap_orphaned_documents(SessionLocal)
    except Exception:
        logger.error("orphan_reaper_failed", exc_info=True)

    # Same reasoning as the document reaper just above, one level up: a claim
    # left PENDING/PROCESSING by a dead worker has no one left to finalize it.
    try:
        with SessionLocal() as session:
            count = ClaimRepository(session).reap_orphaned(
                older_than_seconds=settings.claim_finalize_timeout_seconds
            )
            session.commit()
        if count:
            logger.warning("orphaned_claims_failed", count=count)
    except Exception:
        logger.error("claim_orphan_reaper_failed", exc_info=True)

    # Fails startup on a malformed TAGS.docx or a tag_rules.toml typo (a rule
    # naming a tag the docx doesn't have) -- see app/services/tag_registry.py.
    # Better to never come up than to come up unable to tag a single claim.
    load_registry()

    logger.info(
        "application_started",
        environment=settings.environment,
        version=settings.app_version,
    )
    yield
    logger.info("application_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        # An interactive schema browser is a needless attack surface on a PHI system.
        docs_url="/docs" if settings.environment == "local" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.environment == "local" else None,
    )
    app.state.settings = settings

    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    register_rate_limiting(app)

    # Unprefixed: probes are infrastructure, not a versioned API contract.
    app.include_router(health.router)
    app.include_router(auth.router, prefix=settings.api_v1_prefix)
    app.include_router(documents.router, prefix=settings.api_v1_prefix)
    app.include_router(claims.router, prefix=settings.api_v1_prefix)
    app.include_router(audit_logs.router, prefix=settings.api_v1_prefix)
    app.include_router(admin.router, prefix=settings.api_v1_prefix)

    # Thin operational UI (login, upload, poll OCR). Mounted at /ui, not /, so it
    # cannot shadow /health, /docs, or /api/v1/*. Dir resolved from __file__ so it
    # is CWD-independent. Phase 6 replaces the results panel, keeps this shell.
    app.mount(
        "/ui",
        StaticFiles(directory=str(Path(__file__).parent / "static"), html=True),
        name="ui",
    )

    return app


app = create_app()
