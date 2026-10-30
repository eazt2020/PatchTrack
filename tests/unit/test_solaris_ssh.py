"""Direct Solaris SSH collection, without a live Solaris host.

The executor is replaced so these tests cover the contract PatchTrack relies
on: pinned keys, one failing target not stopping the others, and the Solaris 10
baseline diff. They deliberately do not pretend to prove a real host's command
output; the collector contract is documented and validated in the field.
"""

from __future__ import annotations

import json

import paramiko

from inventory.app.connectors.solaris_ssh import (
    SolarisSshClient,
    SolarisSshError,
    SolarisTarget,
    _PinnedHostKey,
    fingerprint,
)


class FakeExecutor:
    def __init__(self, payloads: dict[str, dict]):
        self.payloads = payloads
        self.commands: list[tuple[str, str]] = []

    def run(self, target: SolarisTarget, command: str, secret: str) -> str:
        self.commands.append((target.name, command))
        payload = self.payloads[target.name]
        if isinstance(payload, Exception):
            raise payload
        return json.dumps(payload)


def target(name: str, os_major: int, **kwargs) -> SolarisTarget:
    return SolarisTarget(
        id=name,
        name=name,
        hostname=f"{name}.example.com",
        username="patchtrack",
        secret_ref=f"file:/etc/patchtrack/secrets/{name}.key",
        os_major=os_major,
        host_key="SHA256:abc",
        **kwargs,
    )


def client(payloads: dict[str, dict], targets: list[SolarisTarget], **kwargs):
    return SolarisSshClient(
        targets,
        executor=FakeExecutor(payloads),
        secret_resolver=lambda reference: "key",
        **kwargs,
    )


def test_collects_every_target_and_reports_one_failure() -> None:
    payloads = {
        "sol11a": {"hostname": "sol11a", "os_version": "11.4", "patches": []},
        "sol11b": SolarisSshError("connection refused"),
    }
    result = client(payloads, [target("sol11a", 11), target("sol11b", 11)]).collect()

    assert [a.source_ref for a in result.assessments] == ["sol11a"]
    assert result.errors == [{"target": "sol11b", "error": "connection refused"}]


def test_solaris_11_uses_the_target_as_the_stable_source_ref() -> None:
    payload = {
        "hostname": "renamed-host",
        "os_version": "11.4",
        "patches": [{"patch_id": "SOLARIS-11.4-SRU-48", "title": "SRU 48"}],
    }
    result = client({"sol11": payload}, [target("sol11", 11)]).collect()

    (assessment,) = result.assessments
    assert assessment.source_ref == "sol11"
    assert assessment.source_reference == "solaris:sol11"
    assert assessment.hostname == "renamed-host"
    # Oracle does not publish a severity in IPS metadata; do not invent one.
    assert assessment.findings[0].severity == "unknown"


def test_solaris_10_diffs_required_revisions_against_installed_patches() -> None:
    baseline = {
        "name": "solaris10-baseline",
        "entries": [
            {"patch_id": "147147", "min_revision": 26, "title": "Kernel"},
            {"patch_id": "148888", "min_revision": 12, "title": "SSH"},
        ],
    }
    payload = {
        "hostname": "sol10",
        "installed_patches": [
            {"patch_id": "147147", "revision": "25"},
            {"patch_id": "148888", "revision": "14"},
        ],
    }
    result = client(
        {"sol10": payload}, [target("sol10", 10, baseline=baseline)]
    ).collect()

    (assessment,) = result.assessments
    assert [f.advisory_id for f in assessment.findings] == ["147147"]
    assert assessment.findings[0].title == "Kernel"
    assert assessment.os_version == "10"


def test_solaris_10_does_not_report_a_patch_obsoleted_by_a_satisfied_entry() -> None:
    baseline = {
        "name": "baseline",
        "entries": [
            {"patch_id": "150000", "min_revision": 2, "obsoletes": ["149999"]},
            {"patch_id": "149999", "min_revision": 1},
        ],
    }
    payload = {"installed_patches": [{"patch_id": "150000", "revision": "2"}]}
    result = client(
        {"sol10": payload}, [target("sol10", 10, baseline=baseline)]
    ).collect()

    (assessment,) = result.assessments
    assert assessment.findings == ()


def test_solaris_10_without_a_baseline_writes_no_assessment() -> None:
    payload = {"installed_patches": [{"patch_id": "147147", "revision": "26"}]}
    result = client({"sol10": payload}, [target("sol10", 10)]).collect()

    assert result.assessments == []
    assert "no Solaris 10 patch baseline" in result.errors[0]["error"]


def test_solaris_10_rejects_a_known_patch_id_with_a_lower_revision() -> None:
    baseline = {
        "name": "baseline",
        "entries": [{"patch_id": "147147-26"}],
    }
    payload = {"installed_patches": ["147147-25"]}
    result = client(
        {"sol10": payload}, [target("sol10", 10, baseline=baseline)]
    ).collect()

    (assessment,) = result.assessments
    assert [f.advisory_id for f in assessment.findings] == ["147147"]


def test_a_pinned_key_must_match_exactly() -> None:
    key = paramiko.RSAKey.generate(1024)
    expected = fingerprint(key)
    policy = _PinnedHostKey(expected)

    policy.missing_host_key(None, "host", key)  # matching key is accepted

    mismatch = _PinnedHostKey("SHA256:not-the-key")
    try:
        mismatch.missing_host_key(None, "host", key)
    except SolarisSshError as exc:
        assert "mismatch" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a mismatched host key was accepted")


def test_an_unpinned_target_is_refused_with_the_actual_fingerprint() -> None:
    key = paramiko.RSAKey.generate(1024)
    policy = _PinnedHostKey("")
    try:
        policy.missing_host_key(None, "host", key)
    except SolarisSshError as exc:
        assert "not pinned" in str(exc)
        assert fingerprint(key) in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an unpinned host key was accepted")
