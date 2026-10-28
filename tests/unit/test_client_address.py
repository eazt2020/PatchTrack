"""The gateway must not believe X-Forwarded-For from a browser.

Uvicorn applies `--forwarded-allow-ips` and rewrites `request.client` for
trusted peers; application code reading the raw header instead would let any
client choose the address recorded in the audit trail — and, before the fix,
put arbitrary text into an `inet` column and fail the request outright.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from starlette.requests import Request

GATEWAY_DIR = Path(__file__).resolve().parents[2] / "services" / "gateway"


@pytest.fixture(scope="module")
def gateway_module():
    for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
        del sys.modules[name]
    sys.path.insert(0, str(GATEWAY_DIR))
    try:
        yield importlib.import_module("app.main")
    finally:
        sys.path.remove(str(GATEWAY_DIR))


def make_request(peer: str, forwarded: str) -> Request:
    return Request({
        "type": "http", "method": "POST", "path": "/api/session/login",
        "headers": [(b"x-forwarded-for", forwarded.encode())] if forwarded else [],
        "client": (peer, 12345), "scheme": "http", "server": ("patchtrack", 8005),
    })


def test_a_forwarded_header_does_not_override_the_peer(gateway_module):
    request = make_request("10.1.2.3", "203.0.113.9")
    assert gateway_module._client_ip(request) == "10.1.2.3"


def test_arbitrary_header_text_never_reaches_the_audit_trail(gateway_module):
    assert gateway_module._client_ip(make_request("10.1.2.3", "not-an-ip")) == "10.1.2.3"


def test_a_non_address_peer_becomes_none(gateway_module):
    # Starlette's TestClient peer is the literal "testclient"; recording it
    # would either fail the insert or record something untrue.
    assert gateway_module._client_ip(make_request("testclient", "")) is None


def test_no_client_at_all_is_none(gateway_module):
    request = Request({
        "type": "http", "method": "GET", "path": "/healthz",
        "headers": [], "client": None, "scheme": "http", "server": ("x", 80),
    })
    assert gateway_module._client_ip(request) is None
