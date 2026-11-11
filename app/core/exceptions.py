"""Domain errors and their HTTP translation.

Error bodies leaving this application carry a stable machine code, a message
written by us, and a request id. They never carry an exception string, a
traceback, or a database value -- any of which may embed PHI.
"""

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.logging import get_logger

logger = get_logger(__name__)

# Starlette renamed HTTP_422_UNPROCESSABLE_ENTITY to _CONTENT and deprecated the
# old name. Pinning the integer decouples us from which spelling is installed.
HTTP_422_UNPROCESSABLE = 422


class AppError(Exception):
    """Base for errors this application raises deliberately."""

    code: str = "internal_error"
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    message: str = "An unexpected error occurred."

    def __init__(self, message: str | None = None, **context: Any) -> None:
        self.message = message or self.message
        # Structured, PHI-free context for the log line. Never serialised to the client.
        self.context = context
        super().__init__(self.message)


class NotFoundError(AppError):
    code = "not_found"
    status_code = status.HTTP_404_NOT_FOUND
    message = "The requested resource does not exist."


class ValidationError(AppError):
    code = "validation_error"
    status_code = HTTP_422_UNPROCESSABLE
    message = "The request payload is invalid."


class AuthenticationError(AppError):
    code = "authentication_failed"
    status_code = status.HTTP_401_UNAUTHORIZED
    message = "Authentication failed."


class AuthorizationError(AppError):
    code = "forbidden"
    status_code = status.HTTP_403_FORBIDDEN
    message = "You do not have permission to perform this action."


class ConflictError(AppError):
    """The resource exists but is not in a state the request can act on --
    e.g. reprocessing a document that is already mid-pipeline."""

    code = "conflict"
    status_code = status.HTTP_409_CONFLICT
    message = "The request conflicts with the resource's current state."


class AllFilesRejectedError(AppError):
    """Every file in a claim upload failed validation -- see
    `app.services.claim_service.ClaimService.create`. Distinct from a bare
    `ValidationError`: the request shape was fine, every individual file's
    content was not."""

    code = "all_files_rejected"
    status_code = HTTP_422_UNPROCESSABLE
    message = "Every file in this upload was rejected; see `rejected` for reasons."


class DocumentUnreadableError(AppError):
    """Stored ciphertext failed authentication, or its file is gone.

    500, not 404: the row exists and promises a document. Failing to produce it
    is a data-integrity fault the operator must see, not a missing resource.
    """

    code = "document_unreadable"
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    message = "The stored document could not be read."


def _body(code: str, message: str, request: Request) -> dict[str, Any]:
    return {
        "error": code,
        "message": message,
        "request_id": getattr(request.state, "request_id", None),
    }


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.warning("app_error", code=exc.code, path=request.url.path, **exc.context)
        # RFC 6750: a 401 from a bearer-protected resource must say so, or clients
        # cannot tell "log in" from "you may not".
        headers = (
            {"WWW-Authenticate": "Bearer"}
            if exc.status_code == status.HTTP_401_UNAUTHORIZED
            else None
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(exc.code, exc.message, request),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default handler echoes the offending input back to the client.
        # For a PHI-bearing upload endpoint that is a disclosure. We return only
        # the field locations that failed.
        fields = [".".join(str(part) for part in err["loc"]) for err in exc.errors()]
        logger.info("request_validation_failed", path=request.url.path, fields=fields)
        return JSONResponse(
            status_code=HTTP_422_UNPROCESSABLE,
            content={
                **_body("validation_error", "The request payload is invalid.", request),
                "fields": fields,
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # exc_info is scrubbed by redact_processor before it reaches a handler.
        logger.error("unhandled_exception", path=request.url.path, exc_info=exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_body("internal_error", "An unexpected error occurred.", request),
        )
