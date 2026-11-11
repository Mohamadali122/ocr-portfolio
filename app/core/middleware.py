"""Request-scoped context and the access log.

Uvicorn's built-in access logger is disabled in `configure_logging`. It prints
the raw request line, query string included -- and Phase 6 introduces
`GET /api/v1/documents?patient_name=...`. Logging that line writes a patient
name to disk on every search. This middleware logs the path only.
"""

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import get_logger

logger = get_logger("app.access")

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, binds it to the log context, echoes it on the response."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Logged with a traceback by the unhandled-exception handler; here we
            # only record that the request terminated abnormally.
            logger.info(
                "request_failed",
                method=request.method,
                path=request.url.path,  # .path excludes the query string
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            raise

        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
