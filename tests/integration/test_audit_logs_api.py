import uuid

import pytest
from fastapi.testclient import TestClient

from app.models.audit_log import AuditAction
from app.models.user import User
from tests.integration.conftest import TEST_PASSWORD


@pytest.fixture
def auth(client: TestClient, admin: User) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login", json={"email": admin.email, "password": TEST_PASSWORD}
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


class TestAuditLogSearch:
    def test_requires_auth(self, client: TestClient) -> None:
        assert client.get("/api/v1/audit-logs").status_code == 401

    def test_lists_recent_entries_most_recent_first(self, client, auth, admin) -> None:
        # Logging in itself wrote a LOGIN_SUCCEEDED row (the `auth` fixture).
        body = client.get("/api/v1/audit-logs?limit=5", headers=auth).json()
        assert len(body["items"]) > 0
        occurred_at = [item["occurred_at"] for item in body["items"]]
        assert occurred_at == sorted(occurred_at, reverse=True)

    def test_filter_by_action(self, client, auth, admin) -> None:
        body = client.get(
            f"/api/v1/audit-logs?action={AuditAction.LOGIN_SUCCEEDED.value}", headers=auth
        ).json()
        assert len(body["items"]) > 0
        assert all(item["action"] == AuditAction.LOGIN_SUCCEEDED.value for item in body["items"])

    def test_filter_by_user_id(self, client, auth, admin) -> None:
        body = client.get(f"/api/v1/audit-logs?user_id={admin.id}", headers=auth).json()
        assert len(body["items"]) > 0
        assert all(item["user_id"] == str(admin.id) for item in body["items"])

    def test_unknown_user_id_is_an_empty_page(self, client, auth) -> None:
        body = client.get(f"/api/v1/audit-logs?user_id={uuid.uuid4()}", headers=auth).json()
        assert body["items"] == []
        assert body["next_cursor"] is None

    def test_keyset_paging_walks_all_rows_without_overlap(self, client, auth, admin) -> None:
        # The `auth` fixture already wrote one row; a couple more searches pad it out.
        client.get("/api/v1/audit-logs?user_id=" + str(admin.id), headers=auth)
        client.get("/api/v1/audit-logs?user_id=" + str(admin.id), headers=auth)

        first = client.get(f"/api/v1/audit-logs?user_id={admin.id}&limit=1", headers=auth).json()
        assert len(first["items"]) == 1
        assert first["next_cursor"] is not None

        cur = first["next_cursor"]
        second = client.get(
            f"/api/v1/audit-logs?user_id={admin.id}&limit=1"
            f"&cursor_occurred_at={cur['occurred_at']}&cursor_id={cur['id']}",
            headers=auth,
        ).json()
        assert len(second["items"]) >= 1
        assert second["items"][0]["id"] != first["items"][0]["id"]

    def test_viewing_is_itself_audited(self, client, auth, admin, audit_rows) -> None:
        client.get("/api/v1/audit-logs", headers=auth)
        rows = audit_rows(admin.id)
        assert any(row.action == AuditAction.AUDIT_LOG_VIEWED.value for row in rows)
