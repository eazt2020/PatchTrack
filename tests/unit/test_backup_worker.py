"""Static safety checks for the backup worker and its configuration.

The worker is a shell script run inside the PostgreSQL image, so the useful
unit-level guarantees are about what it refuses to do: dump with the wrong
exclusions, write outside the volume, or publish a file before its digest and
database row exist.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from patchtrack_common.config import Settings, get_settings
from reporting.app.backups import archive_path

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "deploy" / "backup" / "backup-worker.sh"


@pytest.fixture(scope="module")
def script() -> str:
    return WORKER.read_text()


def test_the_worker_script_is_valid_bash():
    result = subprocess.run(
        ["bash", "-n", str(WORKER)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_the_worker_fails_fast_and_keeps_archives_private(script):
    assert "set -Eeuo pipefail" in script
    assert "umask 0077" in script
    assert 'chmod 0640 "$final_path"' in script


def test_the_dump_excludes_live_sessions_the_queue_and_the_heartbeat(script):
    for table in (
        "identity.refresh_tokens",
        "identity.login_attempts",
        "shared.backup_runs",
        "shared.backup_worker",
    ):
        assert f"--exclude-table-data={table}" in script
    assert "--format=custom" in script
    assert "--no-owner" in script
    assert "--no-privileges" in script


def test_a_database_url_file_is_supported_for_secret_mounted_deployments(script):
    assert "PATCHTRACK_DATABASE_URL_FILE" in script
    assert 'case "$url" in' in script
    assert "postgresql+psycopg://" in script


def test_the_archive_is_published_only_after_every_digest_exists(script):
    # The digest and rename precede the database update; a failure afterwards
    # removes the file so no archive exists without a row that claims it.
    assert 'sha256sum database.dump reports.tar.gz > SHA256SUMS' in script
    assert 'tar -czf "$work/archive.tar.gz"' in script
    assert 'mv -- "$work/archive.tar.gz" "$final_path"' in script
    assert 'rm -f -- "$final_path"' in script


def test_retention_cannot_delete_outside_the_backups_volume(script):
    # Only a basename is removed; a filename containing a path separator is
    # skipped even if a restored database contains one.
    assert 'base="${fname##*/}"' in script
    assert '"$base" == "$fname"' in script
    assert 'rm -f -- "$BACKUP_DIR/$fname"' in script


def test_report_files_being_written_are_not_archived(script):
    assert "--exclude='*.tmp'" in script


def test_backup_settings_are_validated_and_defaulted():
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://user@/db",
        jwt_secret="x" * 32,
    )
    assert settings.backup_dir == Path("/var/lib/patchtrack/backups")
    assert settings.backup_interval == 86400
    assert settings.backup_retention == 7
    assert settings.version == ""

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            database_url="postgresql+psycopg://user@/db",
            jwt_secret="x" * 32,
            backup_interval=30,
        )
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            database_url="postgresql+psycopg://user@/db",
            jwt_secret="x" * 32,
            backup_retention=0,
        )
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            database_url="postgresql+psycopg://user@/db",
            jwt_secret="x" * 32,
            backup_retention=91,
        )


def test_archive_path_accepts_a_plain_name_and_rejects_an_escape(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PATCHTRACK_BACKUP_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        assert archive_path("patchtrack-backup-1.tar.gz") == (
            tmp_path / "patchtrack-backup-1.tar.gz"
        )
        assert archive_path("../archive.tar.gz") is None
        assert archive_path("/etc/passwd") is None
        assert archive_path("sub/archive.tar.gz") is None
        assert archive_path(None) is None
        assert archive_path("") is None
    finally:
        get_settings.cache_clear()


def test_the_backup_job_is_registered_in_the_scheduler():
    from patchtrack_common.scheduler import JOB_DEFINITIONS

    backup = {job.name: job for job in JOB_DEFINITIONS}["reporting.backup"]
    assert backup.interval_setting == "backup_interval"
