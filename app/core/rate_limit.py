"""Rate limiting. Shared across processes via Redis when `redis_url` is set
(every environment but local dev requires it -- see
`Settings._require_redis_outside_local`); falls back to slowapi/limits'
in-memory `memory://` otherwise, which only ever limits the process it runs
in. `storage_uri` is chosen once at import time, not per-request: every
`@limiter.limit(...)` decorator (auth.py, documents.py, admin.py) binds to
this module-level `limiter` at import/decoration time too, so there is no
later point to swap it in.

Keyed by the same trust rule as the audit trail's `client_ip`
(`app/core/net.py`), not slowapi's default `get_remote_address`, so a request
through a future trusted loopback proxy is limited by the real client, not by
the proxy's own address for every caller behind it. Imports `core.net`
directly rather than `app.api.deps.client_ip` -- `app/core` sits below
`app/api` in the layering and must not depend on it.
"""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.net import resolve_client_ip

logger = get_logger(__name__)


def _key(request: Request) -> str:
    peer = request.client.host if request.client else None
    return resolve_client_ip(peer, request.headers.get("x-forwarded-for")) or "unknown"


limiter = Limiter(key_func=_key, storage_uri=get_settings().redis_url or "memory://")


def register_rate_limiting(app: FastAPI) -> None:
    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limited(request: Request, exc: RateLimitExceeded) -> JSONResponse:
        # Same body shape as every other error (app/core/exceptions.py::_body) --
        # a 429 is not exempt from the "no raw exception text" rule.
        logger.warning("rate_limited", path=request.url.path)
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "error": "rate_limited",
                "message": "Too many requests. Try again shortly.",
                "request_id": getattr(request.state, "request_id", None),
            },
            headers={"Retry-After": "60"},
        )
