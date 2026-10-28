"""Release archives must never carry runtime state or credentials."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build-release.sh"


def _archive(tmp_path: Path, *names: str) -> Path:
    staging = tmp_path / "staging"
    for name in names:
        path = staging / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    archive = tmp_path / "release.tar.gz"
    subprocess.run(["tar", "-czf", str(archive), "-C", str(staging), *names],
                   check=True)
    return archive


def _verify(archive: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), "--verify", str(archive)],
                          capture_output=True, text=True)


def test_clean_archive_is_accepted(tmp_path):
    archive = _archive(tmp_path, "README.md", ".env.example", "services/app.py")
    result = _verify(archive)
    assert result.returncode == 0, result.stderr


def test_environment_file_is_rejected(tmp_path):
    archive = _archive(tmp_path, ".env")
    result = _verify(archive)
    assert result.returncode != 0
    assert ".env" in result.stderr


def test_private_key_is_rejected(tmp_path):
    archive = _archive(tmp_path, "deploy/tls/server.key")
    result = _verify(archive)
    assert result.returncode != 0
    assert "server.key" in result.stderr


def test_database_and_runtime_state_are_rejected(tmp_path):
    archive = _archive(tmp_path, "pgdata/base/1", ".runtime/state.json")
    result = _verify(archive)
    assert result.returncode != 0


def test_no_secret_bearing_path_is_tracked():
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True, check=True).stdout.decode().split("\0")
    offenders = [name for name in tracked if name and (
        name == ".env" or name.startswith(".runtime/")
        or name.endswith((".pem", ".key", ".p12", ".pfx")))]
    assert offenders == []
