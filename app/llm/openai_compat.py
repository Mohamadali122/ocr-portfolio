"""OpenAI-compatible HTTP adapter. Implements `LlmProvider`.

LM Studio runs natively on the Windows host, not in this container, so the
model gets the GPU directly. The container reaches it over HTTP at
host.docker.internal. This is why the provider is a thin HTTP client rather
than an in-process model -- there is nothing to load here, only a server to
call.

Never log the prompt or the response body: both carry OCR text, and the OCR
text is PHI.
"""

import httpx

from app.core.logging import get_logger
from app.llm.base import LlmError, LlmRequestRejectedError, LlmTransientError

logger = get_logger(__name__)


class OpenAiCompatProvider:
    """Implements `LlmProvider`."""

    def __init__(self, *, base_url: str, model: str, timeout_seconds: int, max_tokens: int) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._client = httpx.Client(base_url=base_url, timeout=timeout_seconds)

    @property
    def version(self) -> str:
        return self._model

    def complete(self, *, system: str, user: str, temperature: float = 0.0) -> str:
        try:
            response = self._client.post(
                "/chat/completions",
                json={
                    "model": self._model,
                    "temperature": temperature,
                    "max_tokens": self._max_tokens,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # The server accepted the connection and rejected the request --
            # permanent for this input (bad model id, context length exceeded).
            # Log the status code only; the response body may echo the prompt.
            logger.error("llm_request_rejected", status_code=exc.response.status_code)
            raise LlmRequestRejectedError(
                f"LLM rejected the request with status {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            # Transport failure (connection refused, timeout, LM Studio
            # restarting) -- transient, unlike a truncated/malformed response.
            # The exception string may embed request/response content, so log
            # only the exception class; it never carries the prompt.
            logger.error("llm_request_failed", error_type=type(exc).__name__)
            raise LlmTransientError(f"LLM request failed: {type(exc).__name__}") from exc

        body = response.json()
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            logger.error("llm_response_malformed")
            raise LlmError("LLM response did not match the expected envelope") from exc

        if not isinstance(content, str):
            logger.error("llm_response_malformed")
            raise LlmError("LLM response content was not a string")
        return content
