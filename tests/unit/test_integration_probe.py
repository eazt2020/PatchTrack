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
        assert request.url.path == "/api/v3/asset_servers"
        assert request.url.params["input_data"] == (
            '{"list_info":{"start_index":1,"row_count":1}}'
        )
        return httpx.Response(200, json={
            "response_status": {"status_code": 2000, "status": "success"},
            "list_info": {"has_more_rows": False, "start_index": 1, "row_count": 1},
            "asset_servers": [{"id": "4101", "name": "srv-dc-01"}],
        })

    with AssetExplorerClient(
        "https://ae.example.com", secret="test-secret",
        client=http_client(handler),
    ) as client:
        detail = client.probe()

    assert "1 record" in detail


def test_assetexplorer_sends_the_on_premise_integration_key() -> None:
    client = AssetExplorerClient(
        "https://ae.example.com",
        auth_method="integration_key",
        secret="integration-key-value",
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    )
    try:
        assert client._client.headers["AUTHORIZATION"] == "integration-key-value"
        assert client._client.headers["Accept"].endswith(
            "application/vnd.manageengine.sdp.v3+json"
        )
    finally:
        client.close()


def test_assetexplorer_pages_with_input_data_and_has_more_rows() -> None:
    seen_starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.url.params["input_data"])
        start = data["list_info"]["start_index"]
        seen_starts.append(start)
        rows = {
            1: [{"id": "1", "name": "srv-1"}, {"id": "2", "name": "srv-2"}],
            3: [{"id": "3", "name": "srv-3"}],
        }[start]
        return httpx.Response(200, json={
            "response_status": {"status_code": 2000, "status": "success"},
            "list_info": {
                "has_more_rows": start == 1,
                "start_index": start,
                "row_count": 2,
            },
            "asset_servers": rows,
        })

    with AssetExplorerClient(
        "https://ae.example.com", secret="k", page_size=2,
        client=http_client(handler),
    ) as client:
        records = client.fetch_assets()

    assert seen_starts == [1, 3]
    assert [r.source_id for r in records] == ["1", "2", "3"]


def test_assetexplorer_raises_when_response_status_is_not_success() -> None:
    with AssetExplorerClient(
        "https://ae.example.com", secret="k",
        client=http_client(lambda request: httpx.Response(200, json={
            "response_status": {
                "status_code": 4010, "status": "failed",
                "messages": [{"message": "Invalid API key"}],
            }
        })),
    ) as client:
        with pytest.raises(AssetExplorerError, match="Invalid API key"):
            client.probe()


def test_assetexplorer_api_path_is_configurable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/assets"
        return httpx.Response(200, json={"assets": [{"id": "1", "name": "a"}]})

    with AssetExplorerClient(
        "https://ae.example.com", secret="k", api_path="/api/v3/assets",
        client=http_client(handler),
    ) as client:
        assert client.probe().startswith("AssetExplorer answered")


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


@pytest.mark.parametrize(
    "payload",
    [{}, {"error": {"code": "InternalError"}}, {"value": "not-a-list"}, []],
)
def test_sccm_rejects_a_response_without_a_value_array(payload) -> None:
    # A 200 with no device array is a broken view, not a patched estate.
    with SccmClient(
        "https://sccm.example.com", token="t",
        client=http_client(lambda request: httpx.Response(200, json=payload)),
    ) as client:
        with pytest.raises(SccmError, match="value|not a JSON object"):
            client.probe()
        with pytest.raises(SccmError, match="value|not a JSON object"):
            client.fetch_assessments()


def test_sccm_api_path_is_configurable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/reporting/patch-compliance"
        return httpx.Response(200, json={"value": []})

    with SccmClient(
        "https://sccm.example.com", token="t", api_path="reporting/patch-compliance",
        client=http_client(handler),
    ) as client:
        assert "0 device" in client.probe()


def test_sccm_takes_the_first_address_from_an_ip_array() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": [{
            "ResourceID": 16777220,
            "Name": "app01.contoso.com",
            "IPAddresses": ["10.20.0.11", "fe80::1"],
            "MissingUpdates": [{"CI_ID": 35001, "Title": "Update"}],
        }]})

    with SccmClient(
        "https://sccm.example.com", token="t", client=http_client(handler),
    ) as client:
        (assessment,) = client.fetch_assessments()

    assert assessment.ip_address == "10.20.0.11"
    assert assessment.findings[0].advisory_id == "35001"


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
