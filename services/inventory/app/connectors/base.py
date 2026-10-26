"""Connector contracts.

Every patch-management connector returns a normalised :class:`SourceRecord`
and raises on failure. A connector MUST NOT convert an error into an empty
result: an empty result means "the source reports zero outstanding patches",
which is materially different from "the source could not be reached".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

SEVERITIES = ("critical", "important", "moderate", "low", "unknown")


@dataclass(frozen=True)
class NormalisedFinding:
    advisory_id: str
    title: str
    severity: str
    category: str = ""
    released_at: datetime | None = None
    source_reference: str = ""

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            object.__setattr__(self, "severity", "unknown")


@dataclass(frozen=True)
class PatchAssessment:
    """Outstanding patch information for one host from one source system."""

    source_system: str
    source_ref: str
    hostname: str
    fqdn: str | None
    ip_address: str | None
    os_name: str
    os_version: str
    assessed_at: datetime
    status: str
    findings: tuple[NormalisedFinding, ...] = field(default_factory=tuple)
    source_reference: str = ""

    def severity_counts(self) -> dict[str, int]:
        counts = {sev: 0 for sev in SEVERITIES}
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return counts


@dataclass(frozen=True)
class AssetRecord:
    """Normalised inventory record from ManagedEngine AssetExplorer."""

    source_id: str
    hostname: str
    fqdn: str | None = None
    ip_address: str | None = None
    os_name: str = ""
    os_version: str = ""
    asset_type: str = "server"
    asset_status: str = ""
    environment: str = "UNKNOWN"
    business_group: str = "UNKNOWN"
    application_group: str = "UNKNOWN"
    asset_owner: str = ""
    department: str = ""
    location: str = ""
    lifecycle_status: str = "active"
    classification: str = ""
    source_created_at: datetime | None = None
    source_modified_at: datetime | None = None
    raw: dict | None = None

    @property
    def os_family(self) -> str:
        name = f"{self.os_name} {self.os_version}".lower()
        if "windows" in name:
            return "windows"
        if "solaris" in name or "sunos" in name:
            return "solaris"
        if any(k in name for k in ("rhel", "red hat", "redhat", "centos",
                                   "rocky", "oracle linux", "almalinux",
                                   "fedora", "linux")):
            return "rhel"
        return "other"


class InventorySource(Protocol):
    key: str

    def fetch_assets(self) -> list[AssetRecord]: ...


class PatchSource(Protocol):
    key: str

    def fetch_assessments(self) -> list[PatchAssessment]: ...
