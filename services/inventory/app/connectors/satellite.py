"""Red Hat Satellite patch-compliance connector.

PatchTrack reads host errata through Satellite's documented REST API. Only
errata the host has *not* applied are returned as findings. A request failure
raises :class:`SatelliteError` so the caller can mark the integration as
degraded instead of treating the host as compliant.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

from .base import NormalisedFinding, PatchAssessment

log = logging.getLogger(__name__)

SEVERITY_MAP = {
    "critical": "critical",
    "important": "important",
    "moderate": "moderate",
    "low": "low",
    "security": "important",
    "bugfix": "moderate",
    "enhancement": "low",
}


class SatelliteError(RuntimeError):
    pass


class SatelliteClient:
    key = "satellite"

    def __init__(
        self,
        base_url: str,
        *,
        username: str,
        password: str,
        timeout_s: float = 60.0,
        page_size: int = 100,
        verify_tls: bool = True,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/api/v2"
        self.page_size = page_size
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout_s),
            verify=verify_tls,
            auth=(username, password),
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SatelliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            response = self._client.get(f"{self.base_url}{path}", params=params)
        except httpx.HTTPError as exc:
            raise SatelliteError(f"Satellite request failed: {exc}") from exc
        if response.status_code in (401, 403):
            raise SatelliteError("Satellite authentication failed")
        if response.status_code >= 400:
            raise SatelliteError(
                f"Satellite returned HTTP {response.status_code} for {path}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise SatelliteError("Satellite returned malformed JSON") from exc

    def fetch_assessments(self) -> list[PatchAssessment]:
        hosts: list[dict] = []
        page = 1
        while True:
            body = self._get(
                "/hosts",
                {"page": page, "per_page": self.page_size,
                 "search": "subscription_status = valid"},
            )
            results = body.get("results", [])
            hosts.extend(results)
            total = int(body.get("total", len(hosts)))
            if len(hosts) >= total or not results:
                break
            page += 1

        assessments: list[PatchAssessment] = []
        for host in hosts:
            host_id = host.get("id")
            hostname = host.get("name")
            if not host_id or not hostname:
                continue
            errata = self._host_errata(int(host_id))
            findings = tuple(self._to_finding(e) for e in errata)
            assessments.append(
                PatchAssessment(
                    source_system="satellite",
                    source_ref=str(host.get("id")),
                    hostname=str(hostname).split(".")[0],
                    fqdn=str(hostname),
                    ip_address=host.get("ip"),
                    os_name=host.get("operatingsystem_name", "RHEL"),
                    os_version=host.get("content_facet_attributes", {}).get(
                        "content_view_version", ""
                    ),
                    assessed_at=_parse_time(host.get("updated_at")),
                    status="complete",
                    findings=findings,
                    source_reference=f"satellite:host/{host_id}",
                )
            )
        return assessments

    def _host_errata(self, host_id: int) -> list[dict]:
        errata: list[dict] = []
        page = 1
        while True:
            body = self._get(
                f"/hosts/{host_id}/errata",
                {"page": page, "per_page": self.page_size,
                 "search": "errata_type = security or errata_type = bugfix or errata_type = enhancement"},
            )
            results = body.get("results", [])
            errata.extend(results)
            total = int(body.get("total", len(errata)))
            if len(errata) >= total or not results:
                break
            page += 1
        return errata

    @staticmethod
    def _to_finding(errata: dict) -> NormalisedFinding:
        severity = str(errata.get("severity") or errata.get("errata_type") or "unknown").lower()
        return NormalisedFinding(
            advisory_id=str(errata.get("errata_id") or errata.get("id") or "unknown"),
            title=str(errata.get("title") or errata.get("summary") or ""),
            severity=SEVERITY_MAP.get(severity, "unknown"),
            category=str(errata.get("type") or errata.get("errata_type") or ""),
            released_at=_parse_time(errata.get("issued") or errata.get("updated_at")),
            source_reference=str(errata.get("href") or ""),
        )


def _parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return datetime.now(UTC)
