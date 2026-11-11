"""`OpenAiCompatProvider.complete` must tell a permanent rejection (4xx --
retrying this document will never succeed) apart from a transient transport
failure (server down, connection refused).

`httpx.MockTransport` stands in for LM Studio -- no real network, no new
dependency, httpx already ships it.
"""

import json

import httpx
import pytest

from app.llm.base import LlmRequestRejectedError, LlmTransientError
from app.llm.openai_compat import OpenAiCompatProvider


def _provider_with_transport(
    transport: httpx.MockTransport, *, max_tokens: int = 4096
) -> OpenAiCompatProvider:
    provider = OpenAiCompatProvider(
        base_url="http://host.docker.internal:1234/v1",
        model="local-model",
        timeout_seconds=5,
        max_tokens=max_tokens,
    )
    provider._client = httpx.Client(base_url=provider._client.base_url, transport=transport)
    return provider


def test_a_4xx_response_raises_llm_request_rejected_not_transient_llm_error() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(400, json={"error": "context length exceeded"})
    )
    provider = _provider_with_transport(transport)

    with pytest.raises(LlmRequestRejectedError):
        provider.complete(system="sys", user="usr")


def test_a_connection_failure_raises_the_transient_llm_error() -> None:
    # Distinct from truncated/malformed output (plain LlmError, permanent for
    # this deterministic input) -- a dropped connection is worth Celery
    # retrying, see app/worker.py's autoretry handling.
    def _raise_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(_raise_connect_error)
    provider = _provider_with_transport(transport)

    with pytest.raises(LlmTransientError) as exc_info:
        provider.complete(system="sys", user="usr")
    assert not isinstance(exc_info.value, LlmRequestRejectedError)


def test_max_tokens_is_sent_on_every_request() -> None:
    # No max_tokens in the request body -- LM Studio caps output at its own
    # default, which is exactly what truncates the JSON of a large document
    # mid-value ("EOF while parsing"). Must be sent explicitly.
    captured: list[dict] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    transport = httpx.MockTransport(_capture)
    provider = _provider_with_transport(transport, max_tokens=2048)

    provider.complete(system="sys", user="usr")

    assert captured[0]["max_tokens"] == 2048
