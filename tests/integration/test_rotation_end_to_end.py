"""Forced rotation through the real gateway, over HTTP.

The unit test pins the gateway's guard and the integration test pins identity's
endpoint; this one runs both together — gateway in-process, identity on a real
socket, a real database — so the cookie, the CSRF header and the token claim
all interact as they will in production.
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import text

from identity.app.main import _hasher, app as identity_app
from patchtrack_common.config import get_settings
from patchtrack_common.security import create_access_token

USERNAME = "rotation.e2e"
OLD = "the-original-passphrase"
NEW = "the-replacement-passphrase"


@pytest.fixture
def identity_server(migrated_database):
    """Run the identity service on an ephemeral loopback port."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = uvicorn.Server(uvicorn.Config(
        identity_app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            pytest.fail("identity service did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def gateway(identity_server, monkeypatch):
    from gateway.app.main import app as gateway_app

    monkeypatch.setattr(get_settings(), "identity_url", identity_server)
    return TestClient(gateway_app)


@pytest.fixture
def account(engine, clean_tables):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM identity.users WHERE username = :u"),
                     {"u": USERNAME})
        user_id = conn.execute(
            text(
                """
                INSERT INTO identity.users
                    (username, display_name, password_hash, role,
                     auth_source, must_change_password)
                VALUES (:u, 'Rotation E2E', :h, 'viewer', 'local', true)
                RETURNING id
                """
            ),
            {"u": USERNAME, "h": _hasher.hash(OLD)},
        ).scalar_one()
    yield user_id
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM identity.users WHERE id = :id"),
                     {"id": user_id})


def login(client: TestClient, password: str) -> httpx.Response:
    return client.post("/api/session/login",
                       json={"username": USERNAME, "password": password})


def csrf_headers(client: TestClient) -> dict[str, str]:
    return {"x-patchtrack-csrf": client.cookies.get("patchtrack_csrf") or ""}


def test_rotation_is_enforced_and_satisfiable_through_the_gateway(gateway, account):
    response = login(gateway, OLD)
    assert response.status_code == 200, response.text

    # 1. The session reports the debt.
    assert gateway.get("/api/me").json()["must_change_password"] is True

    # 2. Every other proxied route is refused.
    blocked = gateway.get("/api/inventory/assets")
    assert blocked.status_code == 403
    assert "password change required" in blocked.json()["detail"].lower()

    # 3. A wrong current password is refused with a message the dialog can show.
    wrong = gateway.post("/api/session/change-password",
                         json={"current_password": "not-the-password",
                               "new_password": NEW},
                         headers=csrf_headers(gateway))
    assert wrong.status_code == 401
    assert "incorrect" in wrong.json()["detail"].lower()

    # 4. The correct one succeeds and mints a fresh session.
    changed = gateway.post("/api/session/change-password",
                           json={"current_password": OLD, "new_password": NEW},
                           headers=csrf_headers(gateway))
    assert changed.status_code == 200, changed.text
    assert changed.json()["status"] == "password_changed"

    # 5. The new session owes nothing, and routes reach their upstream (the
    #    inventory service is not running here, so 503 — anything but 403
    #    proves the guard is gone).
    assert gateway.get("/api/me").json()["must_change_password"] is False
    assert gateway.get("/api/inventory/assets").status_code != 403

    # 6. The old credential is dead.
    assert login(TestClient(gateway.app), OLD).status_code == 401


def test_a_forced_session_cannot_reach_identity_administration(gateway, account):
    assert login(gateway, OLD).status_code == 200
    assert gateway.get("/api/identity/users").status_code == 403


def test_an_ordinary_session_carries_no_rotation_claim(gateway, account):
    token, _ = create_access_token(
        user_id=str(account), username=USERNAME, role="viewer")
    client = TestClient(gateway.app)
    client.cookies.set("patchtrack_session", token)
    assert client.get("/api/me").json()["must_change_password"] is False
