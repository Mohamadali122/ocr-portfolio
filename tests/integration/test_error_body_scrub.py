"""Error bodies leaving the API: code + our own message + request id, never a
raw exception string, a database value, or the offending input echoed back.

app/core/exceptions.py enforces this centrally (three handlers cover every
exception this app can raise), but the response body is what a client -- or
an attacker -- actually receives. This audits the body, not the handler code:
if a future endpoint bypasses the shared handlers, or an error message drifts
into the "exc.context"-only channel a body never sees, this catches it here.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.models.user import User
from app.repositories.document_repository import DocumentRepository
from tests.integration.conftest import TEST_PASSWORD

# A synthetic patient name, distinct from other test modules' fixtures, used
# only to prove it never appears in a response body.
_PHI_NAME = "KOWALCZYK"

# Every error body's keys, whatever the status code -- see
# app/core/exceptions.py::_body. Validation errors add "fields".
_BASE_KEYS = {"error", "message", "request_id"}


@pytest.fixture
def auth(client: TestClient, admin: User) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login", json={"email": admin.email, "password": TEST_PASSWORD}
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


class TestAuthenticationErrorBody:
    def test_unknown_email_body_is_generic(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "no-such-user@example.com", "password": "whatever-12345"},
        )
        assert response.status_code == 401
        body = response.json()
        assert body.keys() == _BASE_KEYS
        assert body["error"] == "authentication_failed"
        # Never confirms or denies the email exists -- an enumeration oracle.
        assert "no-such-user" not in body["message"]
        assert response.headers["www-authenticate"] == "Bearer"

    def test_wrong_password_body_is_identical_in_shape(
        self, client: TestClient, admin: User
    ) -> None:
        wrong = client.post(
            "/api/v1/auth/login", json={"email": admin.email, "password": "definitely-wrong-pw"}
        )
        unknown = client.post(
            "/api/v1/auth/login",
            json={"email": "no-such-user@example.com", "password": "definitely-wrong-pw"},
        )
        # Same code, same message -- see security.py::waste_time_like_a_verification.
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json()["error"] == unknown.json()["error"]
        assert wrong.json()["message"] == unknown.json()["message"]

    def test_missing_bearer_token_body_is_generic(self, client: TestClient) -> None:
        response = client.get("/api/v1/documents")
        assert response.status_code == 401
        assert response.json().keys() == _BASE_KEYS


class TestValidationErrorBody:
    """The default FastAPI handler echoes the offending input; ours does not
    (app/core/exceptions.py::_validation_error). This is the one place a raw
    value from the request would otherwise land verbatim in the response."""

    def test_upload_with_no_file_reports_field_locations_only(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        response = client.post("/api/v1/documents", headers=auth, data={"doc_type": "hfs"})
        assert response.status_code == 422
        body = response.json()
        assert body.keys() == _BASE_KEYS | {"fields"}
        assert isinstance(body["fields"], list) and body["fields"]
        assert all(isinstance(f, str) for f in body["fields"])

    def test_bad_query_value_is_not_echoed_back(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        # dob_from expects an ISO date. This value is deliberately PHI-shaped
        # (looks like a patient name someone fat-fingered into a date field)
        # so this test would catch it landing back in the response verbatim.
        response = client.get(f"/api/v1/documents?dob_from={_PHI_NAME}", headers=auth)
        assert response.status_code == 422
        body = response.json()
        assert body.keys() == _BASE_KEYS | {"fields"}
        assert _PHI_NAME not in response.text
        assert any("dob_from" in field for field in body["fields"])


class TestNotFoundErrorBody:
    def test_unknown_document_id_body_is_generic(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        response = client.get(f"/api/v1/documents/{uuid.uuid4()}", headers=auth)
        assert response.status_code == 404
        assert response.json().keys() == _BASE_KEYS


class TestUnhandledExceptionErrorBody:
    def test_an_unexpected_failure_never_leaks_its_message(
        self, client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulates a bug or a database fault mid-query. `search`'s real
        # implementation would never embed a value like this -- the point is
        # that even if some future code path raised with PHI in the message,
        # the response body would still not carry it.
        def _raise(self, **kwargs):  # noqa: ANN001, ANN003
            raise RuntimeError(f"query failed while matching patient {_PHI_NAME}")

        monkeypatch.setattr(DocumentRepository, "search", _raise)

        # The shared `client` fixture defaults to `raise_server_exceptions=True`,
        # which makes Starlette's ServerErrorMiddleware re-raise the original
        # exception after it invokes our handler -- a debugging convenience that
        # would otherwise surface this RuntimeError as a test failure instead of
        # the 500 response we actually want to assert on.
        no_raise_client = TestClient(client.app, raise_server_exceptions=False)
        response = no_raise_client.get("/api/v1/documents", headers=auth)
        assert response.status_code == 500
        body = response.json()
        assert body.keys() == _BASE_KEYS
        assert body["error"] == "internal_error"
        assert _PHI_NAME not in response.text
        assert "RuntimeError" not in response.text
