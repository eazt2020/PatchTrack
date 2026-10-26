"""Forced password rotation, end to end against a real database.

`must_change_password` is set when an administrator creates an account or
resets its password. That flag is only a security control if the user can
satisfy it — and if satisfying it actually clears the flag, revokes the
sessions opened with the old credential, and refuses a wrong current password.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from identity.app.main import _hasher, app
from patchtrack_common.config import get_settings
from patchtrack_common.security import create_access_token

USERNAME = "rotation.user"
OLD = "old-correct-horse-battery"
NEW = "new-correct-horse-battery"


@pytest.fixture
def user(engine, clean_tables):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM identity.users WHERE username = :u"), {"u": USERNAME})
        row = conn.execute(
            text(
                """
                INSERT INTO identity.users
                    (username, display_name, password_hash, role,
                     auth_source, must_change_password)
                VALUES (:u, 'Rotation User', :h, 'viewer', 'local', true)
                RETURNING id
                """
            ),
            {"u": USERNAME, "h": _hasher.hash(OLD)},
        ).first()
    yield row[0]
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM identity.users WHERE id = :id"), {"id": row[0]})


def client_for(user_id) -> TestClient:
    token, _ = create_access_token(
        user_id=str(user_id), username=USERNAME, role="viewer",
        must_change_password=True)
    client = TestClient(app)
    client.headers["authorization"] = f"Bearer {token}"
    # TestClient's peer host is the literal "testclient", which the audit
    # trail's inet column would reject; real callers always arrive with an
    # address (the gateway supplies one for proxied requests).
    client.headers["x-forwarded-for"] = "127.0.0.1"
    return client


def refresh_token_count(engine, user_id) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT count(*) FROM identity.refresh_tokens "
                "WHERE user_id = :id AND revoked_at IS NULL"
            ),
            {"id": user_id},
        ).scalar_one()


def test_the_fixture_account_starts_with_rotation_pending(engine, user):
    with engine.connect() as conn:
        flag = conn.execute(
            text("SELECT must_change_password FROM identity.users WHERE id = :id"),
            {"id": user},
        ).scalar_one()
    assert flag is True


def test_changing_the_password_clears_the_flag_and_rotates_the_hash(engine, user):
    response = client_for(user).post(
        "/auth/change-password",
        json={"current_password": OLD, "new_password": NEW},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "password_changed"}
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT must_change_password, password_hash FROM identity.users "
                 "WHERE id = :id"),
            {"id": user},
        ).first()
    assert row.must_change_password is False
    assert _hasher.verify(row.password_hash, NEW)


def test_a_wrong_current_password_is_refused_and_changes_nothing(engine, user):
    response = client_for(user).post(
        "/auth/change-password",
        json={"current_password": "not-the-password", "new_password": NEW},
    )
    assert response.status_code == 401
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT must_change_password, password_hash FROM identity.users "
                 "WHERE id = :id"),
            {"id": user},
        ).first()
    assert row.must_change_password is True
    assert _hasher.verify(row.password_hash, OLD)


def test_the_new_password_must_differ_from_the_current_one(engine, user):
    response = client_for(user).post(
        "/auth/change-password",
        json={"current_password": OLD, "new_password": OLD},
    )
    assert response.status_code == 422


def test_the_new_password_must_meet_the_minimum_length(engine, user):
    response = client_for(user).post(
        "/auth/change-password",
        json={"current_password": OLD, "new_password": "short"},
    )
    assert response.status_code == 422


def test_other_sessions_are_revoked(engine, user):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO identity.refresh_tokens (user_id, token_hash, expires_at) "
                "VALUES (:id, :h, now() + interval '1 hour')"
            ),
            {"id": user, "h": uuid.uuid4().hex},
        )
    assert refresh_token_count(engine, user) == 1

    client_for(user).post(
        "/auth/change-password",
        json={"current_password": OLD, "new_password": NEW},
    )
    # The credential every outstanding session was opened with no longer
    # exists, so none of them may be refreshable.
    assert refresh_token_count(engine, user) == 0


def test_a_service_caller_without_a_user_id_is_refused_cleanly(
        engine, user, monkeypatch):
    # The scheduler authenticates with the shared service token and no user
    # id. It must get a clear refusal, not a 500 from PostgreSQL rejecting ""
    # as a UUID.
    monkeypatch.setattr(get_settings(), "service_token", "test-service-token")
    client = TestClient(app)
    client.headers.update({
        "x-patchtrack-internal": "test-service-token",
        "x-patchtrack-user": "scheduler",
        "x-patchtrack-role": "administrator",
        "x-patchtrack-user-id": "",
        "x-forwarded-for": "127.0.0.1",
    })
    response = client.post(
        "/auth/change-password",
        json={"current_password": OLD, "new_password": NEW},
    )
    assert response.status_code == 403


def test_unauthenticated_callers_cannot_change_a_password(engine, user):
    response = TestClient(app).post(
        "/auth/change-password",
        json={"current_password": OLD, "new_password": NEW},
    )
    assert response.status_code == 401
