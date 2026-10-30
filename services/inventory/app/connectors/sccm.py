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


def _device_rows(response: httpx.Response) -> list:
    """Return the OData ``value`` array, or fail.

    An empty list means "the view reports no devices". A body without a
    ``value`` array (an error envelope, a proxy page, ``{}``) is a broken
    contract and must not be mistaken for a patched estate.
    """
    try:
        body = response.json()
    except ValueError as exc:
        raise SccmError("SCCM returned malformed JSON") from exc
    if not isinstance(body, dict):
        raise SccmError("SCCM response is not a JSON object")
    devices = body.get("value")
    if not isinstance(devices, list):
        raise SccmError("SCCM response does not contain a 'value' array")
    return devices


DEFAULT_API_PATH = "/SMS_REST/DeviceCompliance"


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
        api_path: str = DEFAULT_API_PATH,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        self.api_path = "/" + api_path.lstrip("/")
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

    def probe(self) -> str:
        """One-device query: endpoint, TLS and token, without collection."""
        try:
            response = self._client.get(
                f"{self.base_url}{self.api_path}",
                params={"$top": 1, "$skip": 0},
            )
        except httpx.HTTPError as exc:
            raise SccmError(f"SCCM request failed: {exc}") from exc
        if response.status_code in (401, 403):
            raise SccmError("SCCM authentication failed")
        if response.status_code >= 400:
            raise SccmError(
                f"SCCM returned HTTP {response.status_code} for compliance query"
            )
        devices = _device_rows(response)
        return (
            "SCCM answered with HTTP 200; "
            f"{len(devices)} device record(s) in the first page"
        )

    def fetch_assessments(self) -> list[PatchAssessment]:
        url = f"{self.base_url}{self.api_path}"
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
            devices = _device_rows(response)
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
        if not isinstance(device, dict):
            log.warning("SCCM returned a non-object device record: %s", device)
            return None
        name = device.get("Name") or device.get("ResourceName")
        resource_id = device.get("ResourceID") or device.get("ResourceId")
        if not name or resource_id is None:
            log.warning("SCCM device record missing identity: %s", device)
            return None
        updates = device.get("MissingUpdates") or device.get("RequiredUpdates") or []
        findings = []
        for update in updates:
            if not isinstance(update, dict):
                continue
            advisory_id = (
                update.get("ArticleID") or update.get("BulletinID")
                or update.get("ID") or update.get("CI_ID")
            )
            if advisory_id is None:
                log.warning("SCCM update record missing an identifier: %s", update)
                continue
            findings.append(
                NormalisedFinding(
                    advisory_id=str(advisory_id),
                    title=str(update.get("Title") or update.get("Name") or ""),
                    severity=SEVERITY_MAP.get(
                        str(update.get("Severity") or "unspecified").lower(), "unknown"
                    ),
                    category=str(update.get("Classification") or "update"),
                    released_at=_parse_time(
                        update.get("DateReleased") or update.get("DatePosted")
                    ),
                    source_reference=str(update.get("CI_ID") or ""),
                )
            )
        return PatchAssessment(
            source_system="sccm",
            source_ref=str(resource_id),
            hostname=str(name).split(".")[0],
            fqdn=str(name),
            ip_address=_first_ip(device.get("IPAddresses") or device.get("IPAddress")),
            os_name=device.get("OperatingSystemNameAndVersion") or "Windows Server",
            os_version=str(device.get("OSVersion") or ""),
            assessed_at=_parse_time(
                device.get("LastDDR") or device.get("LastAssessmentTime")
            ),
            status="complete" if device.get("AssessmentComplete", True) else "partial",
            findings=tuple(findings),
            source_reference=f"sccm:device/{resource_id}",
        )


def _first_ip(value: object) -> str | None:
    """SCCM returns IPAddresses as an array; the contract is one address."""
    if isinstance(value, (list, tuple)):
        value = next((item for item in value if item), None)
    if not value:
        return None
    return str(value)


def _parse_time(value: str | None) -> datetime | None:
    """Parse a SCCM timestamp, or return None when the source has none.

    Substituting the current time would make an undated assessment look
    freshly collected, so a missing or malformed value stays unknown.
    """
    if not value:
        return None
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
        log.warning("SCCM returned an unparseable timestamp: %r", value)
        return None
