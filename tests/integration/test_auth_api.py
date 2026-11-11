import time

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.audit_log import AuditAction, AuditLog
from app.models.user import User
from tests.integration.conftest import TEST_PASSWORD, OwnerSessionLocal


def _login(client: TestClient, email: str, password: str):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


class TestLogin:
    def test_valid_credentials_return_a_token_pair(self, client: TestClient, admin: User) -> None:
        response = _login(client, admin.email, TEST_PASSWORD)
        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == 15 * 60
        assert body["access_token"] and body["refresh_token"]

    def test_email_is_case_insensitive(self, client: TestClient, admin: User) -> None:
        assert _login(client, admin.email.upper(), TEST_PASSWORD).status_code == 200

    def test_wrong_password_and_unknown_email_are_indistinguishable(
        self, client: TestClient, admin: User
    ) -> None:
        wrong = _login(client, admin.email, "not-the-password")
        unknown = _login(client, "nobody@example.com", "not-the-password")

        assert wrong.status_code == unknown.status_code == 401
        # Identical body, or the response enumerates valid accounts.
        assert wrong.json()["message"] == unknown.json()["message"]
        assert wrong.json()["error"] == unknown.json()["error"]

    def test_401_carries_www_authenticate(self, client: TestClient, admin: User) -> None:
        response = _login(client, admin.email, "wrong")
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_inactive_account_cannot_log_in(self, client: TestClient, admin: User) -> None:
        with SessionLocal() as session:
            session.get(User, admin.id).is_active = False  # type: ignore[union-attr]
            session.commit()
        assert _login(client, admin.email, TEST_PASSWORD).status_code == 401

    def test_last_login_at_is_recorded(self, client: TestClient, admin: User) -> None:
        assert admin.last_login_at is None
        _login(client, admin.email, TEST_PASSWORD)
        with SessionLocal() as session:
            assert session.get(User, admin.id).last_login_at is not None  # type: ignore[union-attr]


class TestAuditDurability:
    """The reason AuditService owns its own session."""

    def test_successful_login_is_audited(self, client: TestClient, admin: User, audit_rows) -> None:
        _login(client, admin.email, TEST_PASSWORD)
        actions = [row.action for row in audit_rows(admin.id)]
        assert AuditAction.LOGIN_SUCCEEDED.value in actions

    def test_failed_login_survives_the_request_rollback(
        self, client: TestClient, admin: User, audit_rows
    ) -> None:
        # The request raises AuthenticationError. `get_db` rolls back. If the
        # audit row shared that transaction it would vanish -- and a brute-force
        # attempt would leave no trace at all.
        _login(client, admin.email, "not-the-password")

        rows = [r for r in audit_rows(admin.id) if r.action == AuditAction.LOGIN_FAILED.value]
        assert len(rows) == 1
        assert rows[0].detail == {"reason": "bad_password"}

    def test_audit_detail_never_contains_the_submitted_password(
        self, client: TestClient, admin: User, audit_rows
    ) -> None:
        _login(client, admin.email, "hunter2-hunter2-hunter2")
        for row in audit_rows(admin.id):
            assert "hunter2" not in str(row.detail)

    def test_unknown_email_is_audited_without_a_user_id(self, client: TestClient) -> None:
        _login(client, "ghost@example.com", "irrelevant")
        with SessionLocal() as session:
            stmt = (
                select(AuditLog)
                .where(AuditLog.user_id.is_(None))
                .where(AuditLog.action == AuditAction.LOGIN_FAILED.value)
                .order_by(AuditLog.id.desc())
                .limit(1)
            )
            row = session.execute(stmt).scalar_one()
            assert row.detail == {"reason": "unknown_email"}
            # The email itself is absent: audit detail carries reasons, not inputs.
            assert "ghost" not in str(row.detail)
            row_id = row.id

        # `audit_logs` is append-only at the database level -- the app's runtime
        # role has no DELETE grant on it. Cleanup needs the owner-role session,
        # same as every other audit-row teardown since the app-role split.
        with OwnerSessionLocal() as owner_session:
            owner_session.execute(AuditLog.__table__.delete().where(AuditLog.id == row_id))
            owner_session.commit()


class TestProtectedRoutes:
    def test_me_requires_a_token(self, client: TestClient) -> None:
        assert client.get("/api/v1/auth/me").status_code == 401

    def test_me_returns_the_authenticated_user(self, client: TestClient, admin: User) -> None:
        token = _login(client, admin.email, TEST_PASSWORD).json()["access_token"]
        response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.json()["email"] == admin.email
        assert response.json()["role"] == "admin"
        # The hash must not be serialised. UserRead has no such field; assert it.
        assert "password_hash" not in response.json()

    def test_refresh_token_is_not_accepted_as_a_bearer_credential(
        self, client: TestClient, admin: User
    ) -> None:
        refresh = _login(client, admin.email, TEST_PASSWORD).json()["refresh_token"]
        response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {refresh}"})
        assert response.status_code == 401

    def test_garbage_token_is_rejected(self, client: TestClient) -> None:
        response = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
        assert response.status_code == 401


class TestRefreshAndRevocation:
    def test_refresh_returns_a_new_pair(self, client: TestClient, admin: User) -> None:
        refresh = _login(client, admin.email, TEST_PASSWORD).json()["refresh_token"]
        time.sleep(1)  # `iat`/`exp` have one-second resolution
        response = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
        assert response.status_code == 200
        assert response.json()["access_token"]

    def test_bumping_token_version_invalidates_issued_tokens(
        self, client: TestClient, admin: User
    ) -> None:
        tokens = _login(client, admin.email, TEST_PASSWORD).json()
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 200

        with SessionLocal() as session:
            session.get(User, admin.id).token_version += 1  # type: ignore[union-attr]
            session.commit()

        assert client.get("/api/v1/auth/me", headers=headers).status_code == 401
        assert (
            client.post(
                "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
            ).status_code
            == 401
        )
