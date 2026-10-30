"""ManagedEngine AssetExplorer inventory connector.

AssetExplorer is the authoritative inventory source. This connector speaks
its read-only REST API through an HTTP client and normalises only fields the
deployment actually returns. Field mapping is configuration-driven because
AssetExplorer customisations differ per installation.

It targets the documented on-premise V3 Assets API (AssetExplorer build 7700
or later):

* ``GET /api/v3/asset_servers`` — the per-product-type server resource;
* ``input_data`` as a URL-encoded JSON query parameter, with
  ``list_info.start_index`` / ``list_info.row_count`` for paging (the older
  ``page``/``size`` query parameters do not exist on this API);
* ``Authorization: <integration key>`` for the on-premise key configured under
  *Admin > Developer Space > Integration Key*;
* a ``response_status`` / ``list_info.has_more_rows`` envelope with the rows
  under ``asset_servers``.

Older builds expose ``/api/v3/assets``, whose rows are under ``assets``. Both
paths are accepted, and the path is configurable through
``extra.api_path`` for installations with a non-default URL. The exact field
names still differ per installation; override them with ``extra.field_map``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import httpx

from .base import AssetRecord

log = logging.getLogger(__name__)

DEFAULT_API_PATH = "/api/v3/asset_servers"
MAX_ROW_COUNT = 100  # documented maximum for one AssetExplorer page
ACCEPT_HEADER = "application/vnd.manageengine.sdp.v3+json"
ROW_KEYS = ("asset_servers", "asset_assets", "assets", "data")

# Default mapping for the documented AssetExplorer server asset endpoint.
# Administrators can override via the integration's `extra.field_map` JSON.
# ``id`` and ``name`` are the V3 resource's own keys; the older aliases are
# kept for builds that still return them.
DEFAULT_FIELD_MAP: dict[str, str] = {
    "source_id": "id",
    "hostname": "name",
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
        auth_method: str = "integration_key",
        secret: str = "",
        timeout_s: float = 30.0,
        read_timeout_s: float | None = None,
        page_size: int = 200,
        verify_tls: bool = True,
        field_map: dict[str, str] | None = None,
        api_path: str = DEFAULT_API_PATH,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_path = "/" + api_path.strip("/")
        self.secret = secret
        # AssetExplorer refuses a row_count above 100.
        self.page_size = max(1, min(int(page_size), MAX_ROW_COUNT))
        self.field_map = {**DEFAULT_FIELD_MAP, **(field_map or {})}
        headers = {"Accept": ACCEPT_HEADER}
        if auth_method == "integration_key" and secret:
            # On-premise AssetExplorer expects the integration key itself,
            # not a "Bearer" token, in the AUTHORIZATION header.
            headers["AUTHORIZATION"] = secret
        elif auth_method == "bearer" and secret:
            headers["Authorization"] = f"Bearer {secret}"
        elif auth_method == "apikey" and secret:
            headers["X-API-Key"] = secret
        timeout = httpx.Timeout(
            connect=timeout_s,
            read=read_timeout_s or timeout_s,
            write=timeout_s,
            pool=timeout_s,
        )
        self._client = client or httpx.Client(
            timeout=timeout,
            verify=verify_tls,
            headers=headers,
            follow_redirects=False,
            transport=transport,
        )
        self._auth_method = auth_method

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> AssetExplorerClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request_page(self, start_index: int, row_count: int) -> dict[str, Any]:
        url = f"{self.base_url}{self.api_path}"
        input_data = json.dumps(
            {
                "list_info": {
                    "start_index": start_index,
                    "row_count": row_count,
                }
            },
            separators=(",", ":"),
        )
        try:
            response = self._client.get(url, params={"input_data": input_data})
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
        status = body.get("response_status")
        if isinstance(status, dict):
            state = str(status.get("status", "")).lower()
            if state and state != "success":
                message = "; ".join(
                    str(m.get("message", ""))
                    for m in status.get("messages", [])
                    if isinstance(m, dict)
                )
                raise AssetExplorerError(
                    "AssetExplorer reported failure"
                    + (f": {message}" if message else "")
                )
        return body

    @staticmethod
    def _rows(body: dict[str, Any]) -> list[Any]:
        for key in ROW_KEYS:
            value = body.get(key)
            if isinstance(value, list):
                return value
        if "data" in body and not isinstance(body["data"], list):
            raise AssetExplorerError("AssetExplorer data field is not a list")
        lists = [v for k, v in body.items() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
        raise AssetExplorerError("AssetExplorer payload has no asset list")

    def probe(self) -> str:
        """Check reachability, TLS and credentials without collecting.

        One page is enough: it proves the endpoint answers, the credential is
        accepted and the payload is the documented shape. Nothing is written.
        """
        body = self._request_page(1, 1)
        items = self._rows(body)
        return (
            f"AssetExplorer answered with HTTP 200; "
            f"{len(items)} record(s) in the first page"
        )

    def fetch_assets(self) -> list[AssetRecord]:
        records: list[AssetRecord] = []
        start_index = 1
        seen_ids: set[str] = set()
        while True:
            body = self._request_page(start_index, self.page_size)
            items = self._rows(body)
            for item in items:
                if not isinstance(item, dict):
                    continue
                record = self.normalise(item)
                if record is None:
                    continue
                if record.source_id in seen_ids:
                    log.warning("duplicate AssetExplorer id %s skipped", record.source_id)
                    continue
                seen_ids.add(record.source_id)
                records.append(record)
            info = body.get("list_info")
            if isinstance(info, dict) and "has_more_rows" in info:
                more = info["has_more_rows"]
                if isinstance(more, str):
                    more = more.strip().lower() == "true"
                if not more:
                    break
                start_index += self.page_size
            elif len(items) < self.page_size:
                break
            else:
                start_index += self.page_size
            if start_index > self.page_size * 10_000:
                raise AssetExplorerError("AssetExplorer pagination did not terminate")
        return records

    def normalise(self, item: dict[str, Any]) -> AssetRecord | None:
        def pick(field: str) -> Any:
            return item.get(self.field_map.get(field, field))

        source_id = pick("source_id") or item.get("asset_id")
        hostname = pick("hostname") or item.get("host_name")
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
