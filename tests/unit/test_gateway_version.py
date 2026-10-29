"""The console must report the release of the running image.

The Platform panel used to print a hardcoded "PatchTrack 1.0.0", so every
deployment showed the wrong release. The gateway now serves the value from
PATCHTRACK_VERSION, which the installer writes into .env.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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
def client(monkeypatch):
    from patchtrack_common.config import get_settings

    monkeypatch.setenv("PATCHTRACK_VERSION", "9.9.9-test")
    get_settings.cache_clear()
    try:
        with TestClient(_gateway()) as test_client:
            yield test_client
    finally:
        get_settings.cache_clear()


def test_gateway_reports_the_release_from_the_environment(client):
    response = client.get("/api/version")

    assert response.status_code == 200
    assert response.json() == {"version": "9.9.9-test"}


def test_api_metadata_reports_the_same_release(client):
    # The gateway disables its own OpenAPI route, so assert on the app
    # metadata the backend services expose at /openapi.json.
    assert client.app.version == "9.9.9-test"
