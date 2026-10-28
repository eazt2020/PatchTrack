"""ManagedEngine AssetExplorer inventory connector.

AssetExplorer is the authoritative inventory source. This connector speaks
its read-only REST API through an HTTP client and normalises only fields the
deployment actually returns. Field mapping is configuration-driven because
AssetExplorer customisations differ per installation.

The exact AssetExplorer version and API shape must be confirmed against the
customer environment before this integration is declared operational; until
then the connector is exercised against a recorded fixture in tests.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from .base import AssetRecord

log = logging.getLogger(__name__)

# Default mapping for the documented AssetExplorer server asset endpoint.
# Administrators can override via the integration's `extra.field_map` JSON.
DEFAULT_FIELD_MAP: dict[str, str] = {
    "source_id": "asset_id",
    "hostname": "host_name",
    "fqdn": "fqdn",
    "ip_address": "ip_address",
    "os_name": "os_name",
    "os_version": "os_version",
    "asset_type": "asset_type",
    "asset_status": "asset_status",
    "environment": "environment",
    "business_group": "business_group",
    "application_group": "application_group",
    "asset_owner": "asset_owner",
    "department": "department",
    "location": "location",
    "lifecycle_status": "lifecycle_status",
    "classification": "asset_classification",
    "source_created_at": "created_time",
    "source_modified_at": "last_modified_time",
}

LIFECYCLE_MAP = {
    "active": "active",
    "in use": "active",
    "in-use": "active",
    "retired": "retired",
    "decommissioned": "decommissioned",
    "disposed": "decommissioned",
    "staging": "active",
}


class AssetExplorerError(RuntimeError):
    pass


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class AssetExplorerClient:
    key = "assetexplorer"

    def __init__(
        self,
        base_url: str,
        *,
        auth_method: str = "bearer",
        secret: str = "",
        timeout_s: float = 30.0,
        page_size: int = 200,
        verify_tls: bool = True,
        field_map: dict[str, str] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        self.page_size = page_size
        self.field_map = {**DEFAULT_FIELD_MAP, **(field_map or {})}
        headers = {"Accept": "application/json"}
        if auth_method == "bearer" and secret:
            headers["Authorization"] = f"Bearer {secret}"
        elif auth_method == "apikey" and secret:
            headers["X-API-Key"] = secret
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout_s),
            verify=verify_tls,
            headers=headers,
            follow_redirects=False,
        )
        self._auth_method = auth_method

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> AssetExplorerClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request_page(self, page: int) -> dict[str, Any]:
        url = f"{self.base_url}/api/v3/assets/servers"
        try:
            response = self._client.get(
                url, params={"page": page, "size": self.page_size}
            )
        except httpx.HTTPError as exc:
            raise AssetExplorerError(f"AssetExplorer request failed: {exc}") from exc
        if response.status_code in (401, 403):
            raise AssetExplorerError(
                f"AssetExplorer authentication failed (HTTP {response.status_code})"
            )
        if response.status_code >= 500:
            raise AssetExplorerError(
                f"AssetExplorer server error (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            raise AssetExplorerError(
                f"AssetExplorer rejected request (HTTP {response.status_code})"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise AssetExplorerError("AssetExplorer returned malformed JSON") from exc
        if not isinstance(body, dict):
            raise AssetExplorerError("AssetExplorer returned an unexpected payload")
        return body

    def probe(self) -> str:
        """Check reachability, TLS and credentials without collecting.

        One page is enough: it proves the endpoint answers, the credential is
        accepted and the payload is the documented shape. Nothing is written.
        """
        body = self._request_page(1)
        items = body.get("data") or body.get("assets") or []
        if not isinstance(items, list):
            raise AssetExplorerError("AssetExplorer data field is not a list")
        return (
            f"AssetExplorer answered with HTTP 200; "
            f"{len(items)} record(s) in the first page"
        )

    def fetch_assets(self) -> list[AssetRecord]:
        records: list[AssetRecord] = []
        page = 1
        seen_ids: set[str] = set()
        while True:
            body = self._request_page(page)
            items = body.get("data") or body.get("assets") or []
            if not isinstance(items, list):
                raise AssetExplorerError("AssetExplorer data field is not a list")
            for item in items:
                record = self.normalise(item)
                if record is None:
                    continue
                if record.source_id in seen_ids:
                    log.warning("duplicate AssetExplorer id %s skipped", record.source_id)
                    continue
                seen_ids.add(record.source_id)
                records.append(record)
            page_info = body.get("page_info") or {}
            total_pages = page_info.get("total_pages")
            if total_pages is None:
                if len(items) < self.page_size:
                    break
            elif page >= int(total_pages):
                break
            page += 1
            if page > 10_000:
                raise AssetExplorerError("AssetExplorer pagination did not terminate")
        return records

    def normalise(self, item: dict[str, Any]) -> AssetRecord | None:
        def pick(field: str) -> Any:
            return item.get(self.field_map.get(field, field))

        source_id = pick("source_id")
        hostname = pick("hostname")
        if not source_id or not hostname:
            log.warning("AssetExplorer record missing id or hostname: %s", item)
            return None
        lifecycle = str(pick("lifecycle_status") or "active").strip().lower()
        ip = pick("ip_address")
        return AssetRecord(
            source_id=str(source_id),
            hostname=str(hostname).strip(),
            fqdn=(str(pick("fqdn")).strip() or None) if pick("fqdn") else None,
            ip_address=str(ip).strip() if ip else None,
            os_name=str(pick("os_name") or ""),
            os_version=str(pick("os_version") or ""),
            asset_type=str(pick("asset_type") or "server"),
            asset_status=str(pick("asset_status") or ""),
            environment=str(pick("environment") or "UNKNOWN"),
            business_group=str(pick("business_group") or "UNKNOWN"),
            application_group=str(pick("application_group") or "UNKNOWN"),
            asset_owner=str(pick("asset_owner") or ""),
            department=str(pick("department") or ""),
            location=str(pick("location") or ""),
            lifecycle_status=LIFECYCLE_MAP.get(lifecycle, "unknown"),
            classification=str(pick("classification") or ""),
            source_created_at=_parse_ts(pick("source_created_at")),
            source_modified_at=_parse_ts(pick("source_modified_at")),
            raw=item,
        )
