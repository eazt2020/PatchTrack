"""Server-side session revocation and sign-in enumeration resistance.

An access token is self-contained, so the only way to end a session before it
expires is for the services to check something the account owns. These tests
exercise that check and the uniform answer given to every failed sign-in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from identity.app.main import _hasher, app
from patchtrack_common.db import fetch_one
from patchtrack_common.security import create_access_token

PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _database(migrated_database):
    return migrated_database


def _add_user(engine, username: str, role: str = "viewer", *,
              active: bool = True, locked: bool = False) -> str:
    user_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO identity.users
                    (id, username, display_name, password_hash, role,
                     auth_source, is_active, must_change_password, locked_until)
                VALUES (:id, :u, :d, :h, :r, 'local', :active, false, :locked)
                """
            ),
            {
                "id": user_id,
                "u": username,
                "d": username,
                "h": _hasher.hash(PASSWORD),
                "r": role,
                "active": active,
                "locked": (
                    datetime.now(UTC) + timedelta(minutes=15) if locked else None
                ),
            },
        )
    return str(user_id)


def _client(user_id: str, role: str = "viewer", *, version: int = 0) -> TestClient:
    token, _ = create_access_token(
        user_id=user_id, username="someone", role=role, token_version=version
    )
    client = TestClient(app)
    client.headers.update({
        "authorization": f"Bearer {token}",
        "x-forwarded-for": "127.0.0.1",
    })
    return client


def test_a_token_is_refused_once_its_version_is_raised(engine):
    user = _add_user(engine, f"revoke-{uuid4().hex[:8]}")
    client = _client(user)
    assert client.get("/auth/me").status_code == 200

    # What every revocation path does: move the counter the token carries.
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE identity.users SET token_version = token_version + 1 "
                 "WHERE id = :id"),
            {"id": user},
        )
    response = client.get("/auth/me")
    assert response.status_code == 401
    assert "revoked" in response.json()["detail"].lower()


def test_signing_out_revokes_the_access_token(engine):
    user = _add_user(engine, f"logout-{uuid4().hex[:8]}")
    client = _client(user)
    assert client.post("/auth/revoke-session").status_code == 200
    # The cookie may be gone, but a copied token must not keep working.
    assert client.get("/auth/me").status_code == 401


def test_changing_a_role_revokes_existing_sessions(engine):
    admin = _add_user(engine, f"admin-{uuid4().hex[:8]}", "administrator")
    victim = _add_user(engine, f"victim-{uuid4().hex[:8]}", "viewer")
    victim_client = _client(victim)
    assert victim_client.get("/auth/me").status_code == 200

    response = _client(admin, "administrator").patch(
        f"/users/{victim}", json={"role": "operations"}
    )
    assert response.status_code == 200
    assert victim_client.get("/auth/me").status_code == 401


def test_deactivating_an_account_revokes_its_session(engine):
    admin = _add_user(engine, f"admin-{uuid4().hex[:8]}", "administrator")
    victim = _add_user(engine, f"victim-{uuid4().hex[:8]}", "viewer")
    victim_client = _client(victim)

    response = _client(admin, "administrator").patch(
        f"/users/{victim}", json={"is_active": False}
    )
    assert response.status_code == 200
    assert victim_client.get("/auth/me").status_code == 401


def test_a_password_reset_revokes_existing_sessions(engine):
    admin = _add_user(engine, f"admin-{uuid4().hex[:8]}", "administrator")
    victim = _add_user(engine, f"victim-{uuid4().hex[:8]}", "viewer")
    victim_client = _client(victim)

    response = _client(admin, "administrator").post(
        f"/users/{victim}/password", json={"password": "a-new-password-value"}
    )
    assert response.status_code == 200
    assert victim_client.get("/auth/me").status_code == 401


@pytest.mark.parametrize("kind", ["unknown", "inactive", "locked"])
def test_every_failed_sign_in_looks_the_same(engine, kind):
    username = f"enum-{uuid4().hex[:8]}"
    if kind != "unknown":
        _add_user(engine, username, active=(kind != "inactive"),
                  locked=(kind == "locked"))

    response = TestClient(app).post(
        "/auth/login",
        json={"username": username, "password": "wrong"},
        headers={"x-forwarded-for": "127.0.0.1"},
    )
    # No 404/423/400 distinction: status and body must match an unknown user.
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid username or password"


def test_repeated_failures_from_one_address_are_throttled(engine, monkeypatch):
    username = f"throttle-{uuid4().hex[:8]}"
    # TestClient's peer is the literal "testclient", which is not a trusted
    # proxy; pin the address the service would derive in a real deployment.
    import identity.app.main as identity_main

    monkeypatch.setattr(
        identity_main, "_client_ip", lambda request: "198.51.100.7"
    )
    with engine.begin() as conn:
        for _ in range(10):
            conn.execute(
                text(
                    "INSERT INTO identity.login_attempts "
                    "(username, succeeded, source_ip) "
                    "VALUES (:u, false, CAST('198.51.100.7' AS inet))"
                ),
                {"u": username},
            )
    response = TestClient(app).post(
        "/auth/login",
        json={"username": username, "password": "wrong"},
        headers={"x-forwarded-for": "198.51.100.7"},
    )
    assert response.status_code == 429
    assert response.headers.get("retry-after")
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM identity.login_attempts WHERE username = :u"),
            {"u": username},
        )


def test_an_unknown_inactive_or_locked_account_never_gets_a_token(engine):
    username = f"nolock-{uuid4().hex[:8]}"
    _add_user(engine, username, active=False)
    response = TestClient(app).post(
        "/auth/login",
        json={"username": username, "password": PASSWORD},
        headers={"x-forwarded-for": "127.0.0.1"},
    )
    assert response.status_code == 401
    assert fetch_one(
        "SELECT count(*) AS n FROM identity.login_attempts "
        "WHERE username = :u AND succeeded",
        u=username,
    )["n"] == 0
