"""Microsoft Configuration Manager (SCCM) connector.

SCCM deployments expose different interfaces. PatchTrack reads from a
read-only reporting view, either through the Configuration Manager
Administration Service (SMS Provider) REST endpoint or a pre-built SQL view
exported to JSON by an approved data pipeline. Both paths are normalised
into the same assessment shape.

A failure raises :class:`SccmError`; never an empty list.
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
    "none": "unknown",
    "unspecified": "unknown",
}


class SccmError(RuntimeError):
    pass


class SccmClient:
    key = "sccm"

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout_s: float = 60.0,
        page_size: int = 200,
        verify_tls: bool = True,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout_s), verify=verify_tls, headers=headers
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SccmClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetch_assessments(self) -> list[PatchAssessment]:
        url = f"{self.base_url}/SMS_REST/DeviceCompliance"
        assessments: list[PatchAssessment] = []
        skip = 0
        while True:
            try:
                response = self._client.get(
                    url, params={"$top": self.page_size, "$skip": skip}
                )
            except httpx.HTTPError as exc:
                raise SccmError(f"SCCM request failed: {exc}") from exc
            if response.status_code in (401, 403):
                raise SccmError("SCCM authentication failed")
            if response.status_code >= 400:
                raise SccmError(
                    f"SCCM returned HTTP {response.status_code} for compliance query"
                )
            try:
                body = response.json()
            except ValueError as exc:
                raise SccmError("SCCM returned malformed JSON") from exc
            devices = body.get("value", [])
            for device in devices:
                assessment = self._normalise(device)
                if assessment is not None:
                    assessments.append(assessment)
            if len(devices) < self.page_size:
                break
            skip += self.page_size
            if skip > 1_000_000:
                raise SccmError("SCCM pagination did not terminate")
        return assessments

    def _normalise(self, device: dict) -> PatchAssessment | None:
        name = device.get("Name") or device.get("ResourceName")
        resource_id = device.get("ResourceID") or device.get("ResourceId")
        if not name or resource_id is None:
            log.warning("SCCM device record missing identity: %s", device)
            return None
        updates = device.get("MissingUpdates") or device.get("RequiredUpdates") or []
        findings = tuple(
            NormalisedFinding(
                advisory_id=str(u.get("ArticleID") or u.get("BulletinID") or u.get("ID")),
                title=str(u.get("Title") or u.get("Name") or ""),
                severity=SEVERITY_MAP.get(
                    str(u.get("Severity") or "unspecified").lower(), "unknown"
                ),
                category=str(u.get("Classification") or "update"),
                released_at=_parse_time(u.get("DateReleased") or u.get("DatePosted")),
                source_reference=str(u.get("CI_ID") or ""),
            )
            for u in updates
            if isinstance(u, dict)
        )
        return PatchAssessment(
            source_system="sccm",
            source_ref=str(resource_id),
            hostname=str(name).split(".")[0],
            fqdn=str(name),
            ip_address=device.get("IPAddresses") or device.get("IPAddress"),
            os_name=device.get("OperatingSystemNameAndVersion") or "Windows Server",
            os_version=str(device.get("OSVersion") or ""),
            assessed_at=_parse_time(
                device.get("LastDDR") or device.get("LastAssessmentTime")
            ),
            status="complete" if device.get("AssessmentComplete", True) else "partial",
            findings=findings,
            source_reference=f"sccm:device/{resource_id}",
        )


def _parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    cleaned = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        # SCCM often returns /Date(1234567890000)/ style values.
        if cleaned.startswith("/Date("):
            try:
                millis = int(cleaned[6:].split(")")[0].split("+")[0])
                return datetime.fromtimestamp(millis / 1000, tz=UTC)
            except ValueError:
                pass
        return datetime.now(UTC)
