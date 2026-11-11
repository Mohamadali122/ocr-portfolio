"""The LLM seam.

`services/` imports this file. It must never import `openai_compat.py`. Swapping
LM Studio for a different OpenAI-compatible server, or for an in-process model,
is a new adapter and one line in `api/deps.py` -- not a change to any use-case.

The provider returns a raw string, not a parsed structure: unlike OCR, there is
no domain-shaped output to standardize here. Parsing and validating the model's
JSON is `ExtractionService`'s job, so a fake for tests only ever has to return
text, not build dataclasses.
"""

from typing import Protocol, runtime_checkable


class LlmError(Exception):
    """The provider could not produce a completion."""


class LlmRequestRejectedError(LlmError):
    """The LLM server responded with a 4xx. Permanent for this input -- wrong
    model id, malformed request, or context length exceeded. Retrying the
    same document will not succeed. Distinct from a transport failure (server
    down, connection refused), which is transient."""


class LlmTransientError(LlmError):
    """Transport failure (connection refused, timeout, LM Studio restarting).
    Safe to retry the same input -- unlike a plain `LlmError`, which also
    covers truncated/malformed output that a retry of the same deterministic
    prompt would only reproduce."""


@runtime_checkable
class LlmProvider(Protocol):
    def complete(self, *, system: str, user: str, temperature: float = 0.0) -> str:
        """Return the assistant message content. Raises `LlmError`."""
        ...

    @property
    def version(self) -> str:
        """The model id the server reports, recorded on every extraction."""
        ...
