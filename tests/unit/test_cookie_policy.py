"""Session cookies must be TLS-only unless a development override says otherwise.

A cookie marked ``Secure`` is refused by the browser over plain HTTP, so the
default policy (``auto``) correctly makes an unencrypted non-loopback
deployment unusable instead of quietly sending the session token in clear.
The ``never`` override exists so a development instance can be reached by IP
from another host; it must be impossible to combine with production.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from patchtrack_common.config import Settings, get_settings

GATEWAY_DIR = Path(__file__).resolve().parents[2] / "services" / "gateway"


def _gateway_module():
    for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
        del sys.modules[name]
    sys.path.insert(0, str(GATEWAY_DIR))
    try:
        return importlib.import_module("app.main")
    finally:
        sys.path.remove(str(GATEWAY_DIR))


@pytest.fixture()
def gateway():
    return _gateway_module()


def _request(host: str) -> Request:
    return Request({"type": "http", "headers": [(b"host", host.encode())]})


def _with_policy(gateway, policy: str):
    settings = get_settings().model_copy(update={"session_cookie_secure": policy})
    gateway.get_settings = lambda: settings
    return gateway._cookie_secure


@pytest.mark.parametrize("host", ["127.0.0.1:8005", "localhost:8005", "[::1]:8005"])
def test_auto_leaves_loopback_cookies_usable(gateway, host: str):
    assert _with_policy(gateway, "auto")(_request(host)) is False


@pytest.mark.parametrize("host", ["192.168.99.10:8005", "patchtrack.example.com"])
def test_auto_requires_tls_for_any_other_host(gateway, host: str):
    assert _with_policy(gateway, "auto")(_request(host)) is True


def test_never_permits_plain_http_for_a_development_instance(gateway):
    assert _with_policy(gateway, "never")(_request("192.168.99.10:8005")) is False


def test_always_marks_even_loopback_cookies_secure(gateway):
    assert _with_policy(gateway, "always")(_request("127.0.0.1:8005")) is True


def _settings(**overrides) -> Settings:
    values = {
        "database_url": "postgresql+psycopg://example/db",
        "jwt_secret": "t" * 40,
    }
    values.update(overrides)
    return Settings(**values)


def test_production_refuses_the_insecure_override():
    with pytest.raises(ValidationError, match="development-only"):
        _settings(environment="production", session_cookie_secure="never")


def test_production_accepts_the_default_policy():
    assert _settings(environment="production").session_cookie_secure == "auto"


def test_an_unknown_policy_is_rejected():
    with pytest.raises(ValidationError):
        _settings(session_cookie_secure="sometimes")
