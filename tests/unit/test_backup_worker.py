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


def test_restore_requires_an_explicit_confirmation_word():
    from reporting.app.backups import RESTORE_CONFIRMATION

    assert RESTORE_CONFIRMATION == "RESTORE"


def test_migration_versions_are_compared_by_their_number():
    from reporting.app.backups import schema_number

    assert schema_number("021_restore_runs") == 21
    assert schema_number("9_initial") == 9
    assert schema_number(21) == 21
    assert schema_number("") is None
    assert schema_number("unknown") is None


def test_restore_validates_the_archive_before_touching_the_database(script):
    # The digest, the archive's own checksums and the release/schema checks all
    # run before pg_restore; a mismatch exits without renaming anything.
    assert 'sha256sum database.dump reports.tar.gz > SHA256SUMS' in script
    assert 'sha256sum -c SHA256SUMS' in script
    assert 'app_version' in script
    assert 'schema_number' in script
    assert script.index("sha256sum -c SHA256SUMS") < script.index("pg_restore")


def test_restore_keeps_the_previous_database_and_never_reuses_a_name(script):
    assert 'patchtrack_pre_restore_' in script
    assert 'patchtrack_restore_' in script
    # The old database is renamed aside, never dropped; only the scratch
    # database a failed restore leaves behind may be dropped.
    assert 'pg_terminate_backend' in script
    assert 'RENAME TO' in script


def test_the_worker_compares_migration_names_by_their_number(script):
    # "021_restore_runs" must not be rejected as unreadable, and must not sort
    # as a string against "9_initial".
    assert "schema_number()" in script
    assert "0*\\([0-9][0-9]*\\)" in script


def test_an_uploaded_archive_manifest_is_read_without_extracting_it(tmp_path):
    import io
    import json
    import tarfile

    from reporting.app.backups import inspect_archive

    good = tmp_path / "good.tar.gz"
    manifest = {
        "app_version": "3.0.6", "schema_version": "022_restore_uploads",
    }
    with tarfile.open(good, "w:gz") as archive:
        payload = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    assert inspect_archive(good) == manifest

    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(b"not a tar")
    assert inspect_archive(bad) is None


def test_the_worker_reads_uploads_from_their_own_volume(script):
    assert 'UPLOAD_DIR=' in script
    assert 'path="$UPLOAD_DIR/$filename"' in script
    # Uploaded files are removed after the one attempt; archives PatchTrack
    # produced stay for download and retention.
    assert 'rm -f -- "$UPLOAD_DIR/$filename"' in script


def test_the_worker_only_runs_one_restore_at_a_time(script):
    assert "restore_runs_one_active_idx" in (
        ROOT / "deploy" / "postgres" / "migrations" / "021_restore_runs.sql"
    ).read_text()
    assert "FOR UPDATE SKIP LOCKED" in script

