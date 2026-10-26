"""The gateway must refuse a session that still owes a password change.

Forced rotation is only meaningful if the backend enforces it: a client that
ignores the UI prompt must not be able to reach the API with the old session.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from patchtrack_common.security import create_access_token

GATEWAY_COOKIE = "patchtrack_session"
GATEWAY_DIR = Path(__file__).resolve().parents[2] / "services" / "gateway"


def _gateway():
    """Import the gateway app in isolation.

    Every service package is called ``app``, so a sibling service module
    already in ``sys.modules`` would be imported instead.
    """
    for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
        del sys.modules[name]
    sys.path.insert(0, str(GATEWAY_DIR))
    try:
        return importlib.import_module("app.main").app
    finally:
        sys.path.remove(str(GATEWAY_DIR))


@pytest.fixture
def client() -> TestClient:
    return TestClient(_gateway())


def _session(role: str = "administrator", must_change: bool = False) -> str:
    token, _ = create_access_token(
        user_id="u1", username="someone", role=role, must_change_password=must_change)
    return token


def test_me_reports_the_pending_rotation(client: TestClient):
    client.cookies.set(GATEWAY_COOKIE, _session(must_change=True))
    body = client.get("/api/me").json()
    assert body["must_change_password"] is True


def test_me_reports_no_rotation_for_a_normal_session(client: TestClient):
    client.cookies.set(GATEWAY_COOKIE, _session())
    assert client.get("/api/me").json()["must_change_password"] is False


@pytest.mark.parametrize("path", ["/api/inventory/assets", "/api/compliance/summary",
                                  "/api/reporting/reports"])
def test_pending_rotation_blocks_backend_routes(client: TestClient, path: str):
    client.cookies.set(GATEWAY_COOKIE, _session(must_change=True))
    response = client.get(path)
    assert response.status_code == 403
    assert "password change required" in response.json()["detail"].lower()


def test_pending_rotation_blocks_identity_administration(client: TestClient):
    client.cookies.set(GATEWAY_COOKIE, _session(must_change=True))
    assert client.get("/api/identity/users").status_code == 403


def test_sign_out_requires_the_csrf_token(client: TestClient):
    # The SPA's sign-out must go through the API helper, which copies the CSRF
    # cookie into the header; a bare POST is refused and would leave the
    # session cookie in the browser.
    client.cookies.set(GATEWAY_COOKIE, _session())
    client.cookies.set("patchtrack_csrf", "csrf-value")
    assert client.post("/api/session/logout").status_code == 403
    allowed = client.post("/api/session/logout",
                          headers={"x-patchtrack-csrf": "csrf-value"})
    assert allowed.status_code == 200
    assert allowed.json() == {"status": "logged_out"}


def test_a_normal_session_is_not_blocked_by_the_rotation_guard(client: TestClient):
    # The upstream services are not running here, so the request fails — what
    # matters is that it is not refused by the rotation guard.
    client.cookies.set(GATEWAY_COOKIE, _session())
    response = client.get("/api/inventory/assets")
    assert response.status_code != 403
