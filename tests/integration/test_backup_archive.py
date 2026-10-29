"""In-app data archives: queue, download controls and a real round trip.

The worker is a shell script in the PostgreSQL container, so these tests run it
against the test database with the host's pg_dump/pg_restore. That proves the
archive layout, the exclusions, and restore into a freshly created database,
not just that a row changed status.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, make_url, text

from patchtrack_common.security import Principal
from reporting.app import backups as backup_module
from reporting.app import main as reporting_main
from reporting.app.main import (
    BackupRetentionUpdate,
    create_backup,
    download_backup,
    get_backup_settings,
    get_backups,
    set_backup_settings,
)

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "deploy" / "backup" / "backup-worker.sh"
ADMIN = Principal(
    user_id="00000000-0000-0000-0000-000000000010",
    username="administrator",
    role="administrator",
)


class _Request:
    client = SimpleNamespace(host="127.0.0.1")
    headers: dict[str, str] = {}


@pytest.fixture(autouse=True)
def reset_backup_state(engine):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM shared.backup_runs"))
        conn.execute(text("DELETE FROM shared.backup_worker"))
        conn.execute(
            text("DELETE FROM shared.app_settings WHERE key = 'backup.retention'")
        )
    yield


@pytest.fixture
def settings_stub(monkeypatch, tmp_path):
    stub = SimpleNamespace(
        backup_retention=7, backup_dir=tmp_path, version="3.1.0-test",
    )
    monkeypatch.setattr(backup_module, "get_settings", lambda: stub)
    monkeypatch.setattr(reporting_main, "get_settings", lambda: stub)
    return stub


def first_row(engine, sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).first()


def all_rows(engine, sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).all()


def add_backup(engine, **values) -> str:
    backup_id = str(uuid4())
    defaults = {
        "id": backup_id,
        "status": "completed",
        "trigger_kind": "manual",
        "requested_by": "tester",
        "finished_at": datetime.now(UTC),
        "filename": "",
        "sha256": "",
        "size_bytes": None,
        "report_files": None,
        "app_version": "3.1.0-test",
        "schema_version": "020_data_backups",
        "message": "",
    }
    defaults.update(values)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO shared.backup_runs "
                "(id, status, trigger_kind, requested_by, finished_at, "
                " filename, sha256, size_bytes, report_files, app_version, "
                " schema_version, message) "
                "VALUES (:id, :status, :trigger_kind, :requested_by, "
                "        :finished_at, :filename, :sha256, :size_bytes, "
                "        :report_files, :app_version, :schema_version, :message)"
            ),
            defaults,
        )
    return backup_id


def test_settings_default_save_and_worker_heartbeat(engine, settings_stub):
    initial = get_backup_settings(ADMIN)
    assert initial["retention"] == 7
    assert initial["min_retention"] == 1
    assert initial["max_retention"] == 90
    assert initial["worker"]["available"] is False
    assert initial["app_version"] == "3.1.0-test"

    saved = set_backup_settings(
        BackupRetentionUpdate(count=3), _Request(), ADMIN,
    )
    assert saved["retention"] == 3
    assert saved["applies_after_next_backup"] is True

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO shared.backup_worker "
                "(singleton, last_seen_at, worker_version) "
                "VALUES (true, now(), '3.1.0-test')"
            )
        )
    online = get_backup_settings(ADMIN)
    assert online["worker"]["available"] is True
    assert online["worker"]["version"] == "3.1.0-test"

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE shared.backup_worker SET last_seen_at = :old "
                "WHERE singleton"
            ),
            {"old": datetime.now(UTC) - timedelta(minutes=5)},
        )
    assert get_backup_settings(ADMIN)["worker"]["available"] is False

    stored = first_row(
        engine,
        "SELECT value, updated_by FROM shared.app_settings "
        "WHERE key = 'backup.retention'",
    )
    assert stored.value == {"count": 3}
    assert stored.updated_by == "administrator"
    audited = first_row(
        engine,
        "SELECT context FROM shared.audit_events "
        "WHERE event_type = 'settings.updated' "
        "  AND target_id = 'backup.retention' ORDER BY id DESC",
    )
    assert audited.context == {"retention": 3}


def test_only_one_backup_can_be_queued_or_running(engine):
    queued = create_backup(_Request(), ADMIN)
    assert queued["status"] == "queued"

    with pytest.raises(HTTPException) as excinfo:
        create_backup(_Request(), ADMIN)
    assert excinfo.value.status_code == 409
    assert "already queued" in excinfo.value.detail

    listed = get_backups(ADMIN, 100)
    assert listed["stats"]["active"] == 1
    assert [str(row["id"]) for row in listed["items"]] == [queued["id"]]
    audited = first_row(
        engine,
        "SELECT target_id FROM shared.audit_events "
        "WHERE event_type = 'backup.requested' ORDER BY id DESC",
    )
    assert str(audited.target_id) == queued["id"]


def test_download_is_limited_to_a_completed_archive_inside_the_volume(
    engine, tmp_path, settings_stub,
):
    archive = tmp_path / "patchtrack-backup-test.tar.gz"
    archive.write_bytes(b"archive")
    digest = hashlib.sha256(b"archive").hexdigest()
    completed_id = add_backup(
        engine, filename=archive.name, sha256=digest, size_bytes=7,
    )
    assert get_backups(ADMIN, 100)["items"][0]["download_available"] is True

    response = download_backup(UUID(completed_id), _Request(), ADMIN)
    assert Path(response.path) == archive
    assert response.media_type == "application/gzip"
    assert response.headers["x-patchtrack-sha256"] == digest
    audited = first_row(
        engine,
        "SELECT target_id FROM shared.audit_events "
        "WHERE event_type = 'backup.downloaded' ORDER BY id DESC",
    )
    assert str(audited.target_id) == completed_id

    # A stored name with a directory component must never be opened.
    outside = str(tmp_path.parent / "outside.tar.gz")
    evil_id = add_backup(engine, filename=outside, sha256=digest)
    listed = {row["id"]: row for row in get_backups(ADMIN, 100)["items"]}
    assert listed[UUID(evil_id)]["download_available"] is False
    with pytest.raises(HTTPException) as excinfo:
        download_backup(UUID(evil_id), _Request(), ADMIN)
    assert excinfo.value.status_code == 404

    queued_id = add_backup(engine, status="queued", filename="", sha256="")
    with pytest.raises(HTTPException) as excinfo:
        download_backup(UUID(queued_id), _Request(), ADMIN)
    assert excinfo.value.status_code == 409


# ------------------------------------------------------------------ worker
def _server_version_num(url: str) -> int | None:
    try:
        output = subprocess.run(
            ["psql", url, "-Atc", "SHOW server_version_num"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return int(output)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _dump_major() -> int | None:
    try:
        output = subprocess.run(
            ["pg_dump", "--version"], capture_output=True, text=True, check=True,
        ).stdout
        return int(re.search(r"(\d+)", output).group(1))
    except (OSError, subprocess.SubprocessError, AttributeError, ValueError):
        return None


@pytest.fixture
def worker_ready(migrated_database):
    for tool in ("pg_dump", "pg_restore", "psql", "sha256sum", "tar"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} is not installed; the worker round trip needs it")
    server = _server_version_num(libpq_url(migrated_database))
    dump = _dump_major()
    if server is None or dump is None or server // 10000 != dump:
        pytest.skip("pg_dump major does not match the test server")
    return migrated_database


def run_worker(database_url: str, root: Path, *, retention: int = 1) -> None:
    env = os.environ.copy()
    env.update({
        "PATCHTRACK_DATABASE_URL": database_url,
        "PATCHTRACK_BACKUP_DIR": str(root / "backups"),
        "PATCHTRACK_REPORT_DIR": str(root / "reports"),
        "PATCHTRACK_BACKUP_ONCE": "1",
        "PATCHTRACK_BACKUP_RETENTION": str(retention),
        "PATCHTRACK_VERSION": "3.1.0-test",
    })
    result = subprocess.run(
        ["bash", str(WORKER)], env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def libpq_url(database_url: str, database: str | None = None) -> str:
    url = make_url(database_url)
    if database is not None:
        url = url.set(database=database)
    return url.render_as_string(hide_password=False).replace(
        "postgresql+psycopg", "postgresql"
    )


def psql(url: str, sql: str) -> str:
    return subprocess.run(
        ["psql", url, "-Atc", sql], capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_worker_builds_a_verifiable_archive_that_restores(
    engine, tmp_path, worker_ready,
):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "report-1.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    (reports / "report-2.pdf.tmp").write_bytes(b"half-written")
    add_backup(engine, status="queued", finished_at=None, filename="", sha256="")

    run_worker(worker_ready, tmp_path)

    row = first_row(
        engine,
        "SELECT * FROM shared.backup_runs ORDER BY requested_at DESC",
    )
    assert row.status == "completed"
    assert row.report_files == 1
    assert row.app_version == "3.1.0-test"
    assert row.schema_version == "020_data_backups"
    archive = tmp_path / "backups" / row.filename
    assert archive.is_file()
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == row.sha256

    with tarfile.open(archive) as bundle:
        names = set(bundle.getnames())
        assert names == {
            "manifest.json", "database.dump", "reports.tar.gz",
            "SHA256SUMS", "RESTORE.txt",
        }
        manifest = json.loads(bundle.extractfile("manifest.json").read())
        assert manifest["format"] == "patchtrack-data-archive"
        assert manifest["app_version"] == "3.1.0-test"
        assert manifest["schema_version"] == "020_data_backups"
        assert manifest["reports"]["file_count"] == 1

        work = tmp_path / "extract"
        work.mkdir()
        bundle.extractall(work, filter="data")
    verified = subprocess.run(
        ["sha256sum", "-c", "SHA256SUMS"], cwd=work,
        capture_output=True, text=True, check=True,
    )
    assert "database.dump: OK" in verified.stdout
    assert "reports.tar.gz: OK" in verified.stdout
    with tarfile.open(work / "reports.tar.gz") as report_bundle:
        report_names = set(report_bundle.getnames())
    assert "./report-1.pdf" in report_names
    assert "./report-2.pdf.tmp" not in report_names

    # Restore into a brand-new database and compare what survived.
    name = f"backup_restore_{uuid4().hex[:8]}"
    admin = create_engine(
        libpq_url(worker_ready, "postgres").replace("postgresql://", "postgresql+psycopg://", 1),
        isolation_level="AUTOCOMMIT",
    )
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        subprocess.run(
            ["pg_restore", "-d", libpq_url(worker_ready, name),
             "--no-owner", "--no-privileges", str(work / "database.dump")],
            capture_output=True, text=True, check=True,
        )
        restore_url = libpq_url(worker_ready, name)
        assert psql(restore_url, "SELECT max(version) FROM shared.schema_migrations") == (
            "020_data_backups"
        )
        assert psql(restore_url, "SELECT count(*) FROM shared.backup_runs") == "0"
        assert psql(restore_url, "SELECT count(*) FROM identity.refresh_tokens") == "0"
        assert psql(
            restore_url, "SELECT count(*) FROM shared.schema_migrations"
        ) != "0"
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_worker_applies_the_retention_count(engine, tmp_path, worker_ready):
    (tmp_path / "reports").mkdir()
    add_backup(engine, status="queued", finished_at=None, filename="", sha256="")
    run_worker(worker_ready, tmp_path, retention=1)
    first = first_row(
        engine,
        "SELECT filename FROM shared.backup_runs WHERE status='completed'",
    ).filename
    first_path = tmp_path / "backups" / first
    assert first_path.is_file()

    add_backup(engine, status="queued", finished_at=None, filename="", sha256="")
    run_worker(worker_ready, tmp_path, retention=1)

    completed = all_rows(
        engine,
        "SELECT id, filename FROM shared.backup_runs "
        "WHERE status='completed' ORDER BY finished_at DESC",
    )
    assert len(completed) == 1
    assert completed[0].filename != first
    assert not first_path.exists()
    assert (tmp_path / "backups" / completed[0].filename).is_file()
