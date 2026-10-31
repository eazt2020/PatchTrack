"""The gateway must not swallow headers a client needs to act on.

Identity answers a throttled sign-in with 429 and Retry-After. The proxy used
to whitelist only content headers, so the browser received a bare 429 with no
instruction and an operator could not tell how long to wait.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

GATEWAY_DIR = Path(__file__).resolve().parents[2] / "services" / "gateway"


def _gateway_module():
    for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
        del sys.modules[name]
    sys.path.insert(0, str(GATEWAY_DIR))
    try:
        return importlib.import_module("app.main")
    finally:
        sys.path.remove(str(GATEWAY_DIR))


def test_a_throttled_upstream_keeps_its_retry_after_header():
    gateway = _gateway_module()
    upstream = httpx.Response(
        429,
        headers={
            "Retry-After": "900",
            "Content-Type": "application/json",
            "Set-Cookie": "patchtrack_session=leak",
            "X-Internal": "not-for-the-browser",
        },
        json={"detail": "Too many sign-in attempts"},
    )

    forwarded = gateway._forwarded_response_headers(upstream)

    assert forwarded["retry-after"] == "900"
    assert forwarded["content-type"] == "application/json"
    assert "set-cookie" not in {key.lower() for key in forwarded}
    assert "x-internal" not in forwarded


def test_sign_in_forwards_retry_after_from_identity(monkeypatch):
    gateway = _gateway_module()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return httpx.Response(
                429,
                headers={"Retry-After": "900"},
                json={"detail": "Too many sign-in attempts. Try again later."},
            )

    monkeypatch.setattr(gateway.httpx, "AsyncClient", FakeClient)
    request = Request({
        "type": "http", "method": "POST", "path": "/api/session/login",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345), "scheme": "http",
        "query_string": b"", "server": ("testserver", 80),
    })

    with pytest.raises(HTTPException) as raised:
        asyncio.run(gateway.login(
            gateway.LoginBody(username="admin", password="wrong"),
            Response(), request,
        ))

    assert raised.value.status_code == 429
    assert raised.value.headers == {"Retry-After": "900"}
