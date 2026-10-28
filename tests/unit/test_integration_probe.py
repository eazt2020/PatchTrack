"""Each connector's probe must prove the configuration without collecting.

These use httpx MockTransport rather than a live source system. The probe is
the "Test configuration" button: it must succeed on a reachable endpoint with
valid credentials, fail with a useful message on 401, and never be mistaken
for a successful empty collection.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from inventory.app.connectors.assetexplorer import (
    AssetExplorerClient,
    AssetExplorerError,
)
from inventory.app.connectors.satellite import SatelliteClient, SatelliteError
from inventory.app.connectors.sccm import SccmClient, SccmError
from inventory.app.connectors.solaris import SolarisIngestError, probe_inbox


def http_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_assetexplorer_probe_reports_the_first_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/assets/servers"
        assert request.headers["Authorization"] == "Bearer test-secret"
        return httpx.Response(200, json={"data": [{"asset_id": "AE-1"}]})

    with AssetExplorerClient(
        "https://ae.example.com", secret="test-secret",
        # An injected client owns its own headers; the connector only builds
        # them when it creates the client itself.
        client=httpx.Client(
            headers={"Authorization": "Bearer test-secret"},
            transport=httpx.MockTransport(handler),
        ),
    ) as client:
        detail = client.probe()

    assert "1 record" in detail


@pytest.mark.parametrize("status", [401, 403])
def test_assetexplorer_probe_reports_authentication_failure(status: int) -> None:
    with AssetExplorerClient(
        "https://ae.example.com", secret="wrong",
        client=http_client(lambda request: httpx.Response(status)),
    ) as client:
        with pytest.raises(AssetExplorerError, match="authentication failed"):
            client.probe()


def test_assetexplorer_probe_rejects_a_non_list_payload() -> None:
    with AssetExplorerClient(
        "https://ae.example.com",
        client=http_client(
            lambda request: httpx.Response(200, json={"data": "not-a-list"})
        ),
    ) as client:
        with pytest.raises(AssetExplorerError, match="not a list"):
            client.probe()


def test_satellite_probe_uses_one_host_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/hosts"
        assert request.url.params["per_page"] == "1"
        return httpx.Response(200, json={"total": 42, "results": []})

    with SatelliteClient(
        "https://sat.example.com", username="admin", password="pw",
        client=http_client(handler),
    ) as client:
        detail = client.probe()

    assert "42 host" in detail


def test_satellite_probe_reports_authentication_failure() -> None:
    with SatelliteClient(
        "https://sat.example.com", username="admin", password="pw",
        client=http_client(lambda request: httpx.Response(401)),
    ) as client:
        with pytest.raises(SatelliteError, match="authentication failed"):
            client.probe()


def test_sccm_probe_uses_one_device_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/SMS_REST/DeviceCompliance"
        assert request.url.params["$top"] == "1"
        return httpx.Response(200, json={"value": [{"ResourceID": 1}]})

    with SccmClient(
        "https://sccm.example.com", token="t",
        client=http_client(handler),
    ) as client:
        detail = client.probe()

    assert "1 device" in detail


def test_sccm_probe_reports_authentication_failure() -> None:
    with SccmClient(
        "https://sccm.example.com", token="t",
        client=http_client(lambda request: httpx.Response(403)),
    ) as client:
        with pytest.raises(SccmError, match="authentication failed"):
            client.probe()


def write_document(inbox: Path, name: str, payload: dict) -> None:
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / name).write_text(json.dumps(payload), encoding="utf-8")


def test_solaris_probe_reports_a_readable_inbox(tmp_path: Path) -> None:
    write_document(tmp_path, "one.json", {"hostname": "solaris-01", "patches": []})
    detail = probe_inbox(tmp_path)
    assert "1 document" in detail


def test_solaris_probe_fails_when_the_inbox_is_missing(tmp_path: Path) -> None:
    with pytest.raises(SolarisIngestError, match="does not exist"):
        probe_inbox(tmp_path / "missing")


def test_solaris_probe_fails_on_a_malformed_document(tmp_path: Path) -> None:
    write_document(tmp_path, "bad.json", {"patches": []})
    with pytest.raises(SolarisIngestError, match="hostname"):
        probe_inbox(tmp_path)
