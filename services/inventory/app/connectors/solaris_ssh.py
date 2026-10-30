"""Direct Solaris patch collection over SSH.

Two collection models share one transport:

* **Solaris 11.4+** — the collector reads the IPS state on the host
  (``entire`` and ``pkg list -u``) and emits pending findings.
* **Solaris 10** — the host can only enumerate installed patches. The
  collector emits ``installed_patches``; PatchTrack diffs that against the
  baseline assigned to the target. With no baseline, no assessment is written:
  an unbaselined host is Unknown, never "no missing patches".

The SSH command is a fixed, read-only collector. PatchTrack never opens a
shell, never forwards ports and never runs a command supplied by the target.
Host keys are pinned per target; a missing or mismatched key is an error.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Callable, Protocol

import paramiko

from .base import SEVERITIES, NormalisedFinding, PatchAssessment
from .solaris import SEVERITY_MAP, SolarisIngestError, parse_solaris_document

log = logging.getLogger(__name__)

DEFAULT_COLLECTOR_COMMAND = "/opt/patchtrack/collect-solaris --format=json"
DEFAULT_WORKERS = 4
MAX_WORKERS = 8


class SolarisSshError(RuntimeError):
    pass


@dataclass(frozen=True)
class SolarisTarget:
    """A configured server or non-global zone."""

    id: str
    name: str
    hostname: str
    username: str
    secret_ref: str
    os_major: int
    port: int = 22
    host_key: str = ""
    zone: str = "global"
    collector_command: str = ""
    connect_timeout_s: int = 15
    read_timeout_s: int = 120
    baseline: dict[str, Any] | None = None


@dataclass(frozen=True)
class CollectionResult:
    assessments: list[PatchAssessment]
    errors: list[dict[str, str]]


class SshExecutor(Protocol):
    def run(self, target: SolarisTarget, command: str, secret: str) -> str: ...


def fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def _normalise_fingerprint(value: str) -> str:
    text = (value or "").strip()
    if text.lower().startswith("sha256:"):
        text = text[7:]
    return text.rstrip("=")


class _PinnedHostKey(paramiko.MissingHostKeyPolicy):
    """Accept exactly the configured key, and nothing else."""

    def __init__(self, expected: str) -> None:
        self.expected = _normalise_fingerprint(expected)
        self.actual = ""

    def missing_host_key(
        self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey
    ) -> None:
        self.actual = fingerprint(key)
        if not self.expected:
            raise SolarisSshError(
                f"host key for {hostname} is not pinned; record {self.actual}"
            )
        if _normalise_fingerprint(self.actual) != self.expected:
            raise SolarisSshError(
                f"host key mismatch for {hostname}: expected SHA256:"
                f"{self.expected}, got {self.actual}"
            )


def _load_private_key(secret: str) -> paramiko.PKey:
    errors: list[str] = []
    for key_type in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return key_type.from_private_key(io.StringIO(secret))
        except (paramiko.SSHException, ValueError) as exc:
            errors.append(str(exc))
    raise SolarisSshError(
        "credential is not an unencrypted OpenSSH private key "
        f"({'; '.join(errors[:2])})"
    )


class ParamikoExecutor:
    """OpenSSH transport with pinned host keys and key-only authentication."""

    def run(self, target: SolarisTarget, command: str, secret: str) -> str:
        key = _load_private_key(secret)
        client = paramiko.SSHClient()
        policy = _PinnedHostKey(target.host_key)
        client.set_missing_host_key_policy(policy)
        try:
            client.connect(
                target.hostname,
                port=target.port,
                username=target.username,
                pkey=key,
                timeout=target.connect_timeout_s,
                banner_timeout=target.connect_timeout_s,
                auth_timeout=target.connect_timeout_s,
                look_for_keys=False,
                allow_agent=False,
            )
            _, stdout, stderr = client.exec_command(
                command, timeout=target.read_timeout_s
            )
            output = stdout.read().decode("utf-8", "replace")
            error = stderr.read().decode("utf-8", "replace").strip()
            status = stdout.channel.recv_exit_status()
            if status != 0:
                raise SolarisSshError(
                    f"collector exited {status} on {target.name}: "
                    f"{(error or output).strip()[:300]}"
                )
            return output
        except SolarisSshError:
            raise
        except paramiko.AuthenticationException as exc:
            raise SolarisSshError(
                f"SSH authentication failed for {target.name}"
            ) from exc
        except (paramiko.SSHException, OSError, TimeoutError) as exc:
            raise SolarisSshError(
                f"SSH connection to {target.name} failed: {exc}"
            ) from exc
        finally:
            client.close()


class SolarisSshClient:
    key = "solaris"

    def __init__(
        self,
        targets: list[SolarisTarget],
        *,
        collector_command: str = DEFAULT_COLLECTOR_COMMAND,
        workers: int = DEFAULT_WORKERS,
        executor: SshExecutor | None = None,
        secret_resolver: Callable[[str], str] | None = None,
    ) -> None:
        self.targets = targets
        self.collector_command = collector_command
        self.workers = max(1, min(int(workers), MAX_WORKERS))
        self._executor = executor or ParamikoExecutor()
        self._resolve = secret_resolver or _default_resolve

    def probe(self, target: SolarisTarget, secret: str) -> str:
        payload = self._run_one(target, secret)
        if target.os_major == 10:
            installed = _installed_patches(payload)
            detail = f"{len(installed)} installed patch(es)"
            if not target.baseline:
                raise SolarisSshError(
                    f"{target.name}: collector works, but no Solaris 10 patch "
                    "baseline is assigned; compliance would be Unknown"
                )
            findings, _ = _solaris10_findings(target, installed)
            detail += f", {len(findings)} missing against baseline"
            return f"SSH and collector OK; {detail}"
        assessment = self._solaris11_assessment(target, payload)
        return (
            "SSH and collector OK; "
            f"{len(assessment.findings)} pending finding(s); "
            f"source {assessment.os_name} {assessment.os_version}"
        )

    def collect(self) -> CollectionResult:
        assessments: list[PatchAssessment] = []
        errors: list[dict[str, str]] = []
        if not self.targets:
            return CollectionResult([], errors)

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {
                pool.submit(self._collect_target, target): target
                for target in self.targets
            }
            results: dict[str, PatchAssessment | None] = {}
            for future in as_completed(futures):
                target = futures[future]
                try:
                    results[target.id] = future.result()
                except (SolarisSshError, SolarisIngestError) as exc:
                    errors.append({"target": target.name, "error": str(exc)})
                    log.warning("Solaris target %s failed: %s", target.name, exc)
                except Exception as exc:  # noqa: BLE001 - one target must not stop the rest
                    errors.append({
                        "target": target.name,
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    log.exception("Solaris target %s failed unexpectedly", target.name)

        for target in self.targets:
            assessment = results.get(target.id)
            if assessment is not None:
                assessments.append(assessment)
        return CollectionResult(assessments, errors)

    def _collect_target(self, target: SolarisTarget) -> PatchAssessment:
        secret = self._resolve(target.secret_ref)
        payload = self._run_one(target, secret)
        if target.os_major == 10:
            if not target.baseline:
                raise SolarisSshError(
                    f"{target.name}: no Solaris 10 patch baseline assigned"
                )
            installed = _installed_patches(payload)
            assessment, _ = _solaris10_findings(target, installed)
            return assessment
        return self._solaris11_assessment(target, payload)

    def _run_one(self, target: SolarisTarget, secret: str) -> dict[str, Any]:
        command = target.collector_command or self.collector_command
        raw = self._executor.run(target, command, secret)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SolarisSshError(
                f"collector on {target.name} returned malformed JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise SolarisSshError(
                f"collector on {target.name} returned a non-object document"
            )
        return payload

    def _solaris11_assessment(
        self, target: SolarisTarget, payload: dict[str, Any]
    ) -> PatchAssessment:
        document = dict(payload)
        document.setdefault("hostname", target.hostname)
        document.setdefault("assessed_at", datetime.now(UTC).isoformat())
        assessment = parse_solaris_document(document)
        return replace(
            assessment,
            source_ref=target.name,
            source_reference=f"solaris:{target.name}",
        )


def _solaris10_findings(
    target: SolarisTarget, installed: dict[str, int]
) -> tuple[PatchAssessment, list[dict[str, Any]]]:
    """Diff installed patches against the assigned baseline.

    A patch is satisfied when its installed revision is at least the required
    revision. Entries obsoleted by a satisfied baseline patch are not reported.
    """
    if not target.baseline:
        raise SolarisSshError(
            f"{target.name}: no Solaris 10 patch baseline assigned"
        )
    entries = target.baseline.get("entries")
    try:
        parsed = validate_baseline_entries(entries)
    except ValueError as exc:
        raise SolarisSshError(
            f"{target.name}: invalid baseline "
            f"{target.baseline.get('name', '')}: {exc}"
        ) from exc

    satisfied: set[str] = set()
    obsoleted: set[str] = set()
    for entry in parsed:
        patch_id = entry["patch_id"]
        revision = entry["min_revision"]
        if installed.get(patch_id, -1) >= revision:
            satisfied.add(patch_id)
            for old in entry.get("obsoletes") or []:
                obsoleted.add(_base_patch_id(old))

    findings: list[NormalisedFinding] = []
    missing: list[dict[str, Any]] = []
    for entry in parsed:
        if entry["patch_id"] in satisfied or entry["patch_id"] in obsoleted:
            continue
        missing.append(entry)
        severity = SEVERITY_MAP.get(
            str(entry.get("severity") or "unknown").lower(), "unknown"
        )
        findings.append(
            NormalisedFinding(
                advisory_id=entry["patch_id"],
                title=str(
                    entry.get("title")
                    or f"Patch {entry['patch_id']} revision "
                       f"{entry['min_revision']} or later required"
                ),
                severity=severity,
                category=str(entry.get("category") or "patch"),
                source_reference=str(entry.get("url") or ""),
            )
        )

    assessment = PatchAssessment(
        source_system="solaris",
        source_ref=target.name,
        hostname=target.hostname.split(".")[0],
        fqdn=target.hostname,
        ip_address=None,
        os_name="Oracle Solaris",
        os_version="10",
        assessed_at=datetime.now(UTC),
        status="complete",
        findings=tuple(findings),
        source_reference=f"solaris:{target.name}",
    )
    return assessment, missing


def _installed_patches(payload: dict[str, Any]) -> dict[str, int]:
    raw = payload.get("installed_patches")
    if raw is None:
        raise SolarisSshError(
            "Solaris 10 collector did not return an installed_patches list"
        )
    if not isinstance(raw, list):
        raise SolarisSshError("installed_patches must be a list")
    installed: dict[str, int] = {}
    for item in raw:
        if isinstance(item, dict):
            patch_id = item.get("patch_id")
            revision = item.get("revision")
        elif isinstance(item, str):
            patch_id, _, revision = item.partition("-")
        else:
            continue
        if not patch_id:
            continue
        base = _base_patch_id(str(patch_id))
        installed[base] = max(installed.get(base, -1), _revision(revision))
    return installed


def _base_patch_id(value: str) -> str:
    return value.strip().split("-")[0]


def validate_baseline_entries(entries: object) -> list[dict[str, Any]]:
    """Validate and normalise a baseline's patch list.

    ``patch_id`` may carry the required revision (``147147-26``); when
    ``min_revision`` is absent that revision is used.
    """
    if not isinstance(entries, list):
        raise ValueError("entries must be a list")
    if len(entries) > 50_000:
        raise ValueError("a baseline may contain at most 50,000 patches")
    normalised: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"entry {index} must be an object")
        raw_id = str(entry.get("patch_id") or "").strip()
        if not raw_id:
            raise ValueError(f"entry {index} is missing patch_id")
        patch_id = _base_patch_id(raw_id)
        if not patch_id:
            raise ValueError(f"entry {index} has an invalid patch_id")
        if patch_id in seen:
            raise ValueError(f"patch {patch_id} is listed more than once")
        seen.add(patch_id)
        if entry.get("min_revision") is None:
            revision = _revision(raw_id)
        else:
            try:
                revision = int(entry["min_revision"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"entry {index} has an invalid min_revision"
                ) from exc
        if revision < 0:
            raise ValueError(f"entry {index} has a negative min_revision")
        severity = str(entry.get("severity") or "unknown").lower()
        if severity not in SEVERITIES:
            severity = "unknown"
        obsoletes = entry.get("obsoletes") or []
        if not isinstance(obsoletes, list):
            raise ValueError(f"entry {index} has a non-list obsoletes")
        normalised.append({
            "patch_id": patch_id,
            "min_revision": revision,
            "title": str(entry.get("title") or ""),
            "severity": severity,
            "category": str(entry.get("category") or "patch"),
            "url": str(entry.get("url") or ""),
            "obsoletes": [_base_patch_id(str(v)) for v in obsoletes if v],
        })
    return normalised


def _revision(value: object) -> int:
    try:
        return int(str(value).split("-")[-1])
    except (TypeError, ValueError):
        return 0


def _default_resolve(reference: str) -> str:
    from patchtrack_common.secrets import resolve

    return resolve(reference)
