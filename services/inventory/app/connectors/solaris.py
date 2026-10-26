"""Solaris compliance ingestion.

Direct SSH collection is explicitly not assumed. Solaris assessments arrive
as JSON files deposited by the organisation's approved read-only collection
job into a watched inbox directory. This module validates and parses them;
the inventory service decides when to ingest.

Each file is one host assessment:

    {
      "hostname": "solaris-db01",
      "fqdn": "solaris-db01.example.com",
      "ip_address": "10.20.0.11",
      "os_name": "Oracle Solaris",
      "os_version": "11.4",
      "assessed_at": "2026-10-20T02:00:00Z",
      "patches": [
        {"patch_id": "356890-01", "title": "...", "severity": "important",
         "released_at": "2026-09-02"}
      ]
    }
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from .base import NormalisedFinding, PatchAssessment

log = logging.getLogger(__name__)


class SolarisIngestError(RuntimeError):
    pass


SEVERITY_MAP = {
    "critical": "critical",
    "security": "critical",
    "important": "important",
    "recommended": "moderate",
    "moderate": "moderate",
    "low": "low",
    "optional": "low",
}


def parse_solaris_document(payload: dict) -> PatchAssessment:
    hostname = str(payload.get("hostname") or "").strip()
    if not hostname:
        raise SolarisIngestError("Solaris document missing hostname")
    assessed_raw = payload.get("assessed_at")
    try:
        assessed_at = (
            datetime.fromisoformat(str(assessed_raw).replace("Z", "+00:00"))
            if assessed_raw
            else datetime.now(UTC)
        )
    except ValueError as exc:
        raise SolarisIngestError("Invalid assessed_at timestamp") from exc
    patches = payload.get("patches") or []
    if not isinstance(patches, list):
        raise SolarisIngestError("patches must be a list")
    findings = []
    for patch in patches:
        if not isinstance(patch, dict) or not patch.get("patch_id"):
            raise SolarisIngestError("patch entry missing patch_id")
        severity = SEVERITY_MAP.get(
            str(patch.get("severity") or "moderate").lower(), "unknown"
        )
        findings.append(
            NormalisedFinding(
                advisory_id=str(patch["patch_id"]),
                title=str(patch.get("title") or patch.get("synopsis") or ""),
                severity=severity,
                category=str(patch.get("category") or "patch"),
                source_reference=str(patch.get("url") or ""),
            )
        )
    return PatchAssessment(
        source_system="solaris",
        source_ref=hostname,
        hostname=hostname.split(".")[0],
        fqdn=payload.get("fqdn") or hostname,
        ip_address=payload.get("ip_address"),
        os_name=str(payload.get("os_name") or "Oracle Solaris"),
        os_version=str(payload.get("os_version") or ""),
        assessed_at=assessed_at,
        status="complete",
        findings=tuple(findings),
        source_reference=f"solaris:{hostname}",
    )


def load_inbox(inbox: Path) -> list[tuple[Path, PatchAssessment]]:
    """Read every ``*.json`` file in the inbox. Malformed files raise."""
    if not inbox.exists():
        return []
    results: list[tuple[Path, PatchAssessment]] = []
    for path in sorted(inbox.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SolarisIngestError(f"{path.name}: unreadable ({exc})") from exc
        if not isinstance(payload, dict):
            raise SolarisIngestError(f"{path.name}: document must be an object")
        results.append((path, parse_solaris_document(payload)))
    return results
