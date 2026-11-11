import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.core.middleware import REQUEST_ID_HEADER


def test_health_is_unauthenticated_and_reports_liveness(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_response_carries_no_database_state(client: TestClient) -> None:
    # Liveness must not touch the database, or a database outage restart-loops
    # the container instead of merely failing readiness.
    assert set(client.get("/health").json()) == {"status", "service", "version", "environment"}


def test_request_id_is_echoed_back(client: TestClient) -> None:
    response = client.get("/health", headers={REQUEST_ID_HEADER: "abc-123"})
    assert response.headers[REQUEST_ID_HEADER] == "abc-123"


def test_request_id_is_generated_when_absent(client: TestClient) -> None:
    assert client.get("/health").headers[REQUEST_ID_HEADER]


def test_readiness_reports_degraded_when_database_is_unreachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The failure is injected rather than inferred from the environment: this
    # suite runs both inside the compose network, where Postgres is reachable,
    # and on a bare host, where it is not.
    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr("app.api.v1.health.engine.connect", _refuse)

    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "database": "unavailable"}


def test_readiness_probe_returns_no_table_rows(client: TestClient) -> None:
    # Whatever the database says, the probe body is two enum fields. An
    # unauthenticated endpoint must not be able to disclose a row.
    assert set(client.get("/health/ready").json()) == {"status", "database"}
