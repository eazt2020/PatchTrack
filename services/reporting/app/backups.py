"""Data-archive queue, retention setting and worker status.

The reporting service never runs pg_dump itself. It writes a row to
``shared.backup_runs``; the backup worker container owns the dump, the report
tar and the retention sweep. That keeps the application image free of database
client binaries and guarantees the dump comes from the same PostgreSQL major
version as the server.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

from patchtrack_common.config import get_settings
from patchtrack_common.db import connection, fetch_all, fetch_one

BACKUP_RETENTION_KEY = "backup.retention"
BACKUP_RETENTION_MIN = 1
BACKUP_RETENTION_MAX = 90
BACKUP_ACTIVE_STATUSES = ("queued", "running")
WORKER_STALE_SECONDS = 60


def backup_retention() -> tuple[int, dict[str, Any] | None]:
    """The configured archive count and the setting row, if one exists."""
    default = int(get_settings().backup_retention)
    row = fetch_one(
        "SELECT value, updated_at, updated_by FROM shared.app_settings "
        "WHERE key = :key",
        key=BACKUP_RETENTION_KEY,
    )
    if row is None:
        return default, None
    value = row.get("value")
    count = default
    if isinstance(value, dict):
        try:
            count = int(value.get("count"))
        except (TypeError, ValueError):
            count = default
    return max(BACKUP_RETENTION_MIN, min(BACKUP_RETENTION_MAX, count)), row


def save_backup_retention(count: int, updated_by: str) -> None:
    with connection() as conn:
        conn.execute(
            text(
                "INSERT INTO shared.app_settings "
                "(key, value, updated_at, updated_by) "
                "VALUES (:key, CAST(:v AS jsonb), now(), :by) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
                "    updated_at = now(), updated_by = EXCLUDED.updated_by"
            ),
            {
                "key": BACKUP_RETENTION_KEY,
                "v": json.dumps({"count": count}),
                "by": updated_by,
            },
        )


def archive_path(filename: str | None) -> Path | None:
    """The archive's path, or None if the stored name escapes the volume.

    The worker generates the names, but a restore from an old or altered
    database must not be able to make the service read an arbitrary file.
    """
    if not filename or Path(filename).name != filename:
        return None
    root = Path(get_settings().backup_dir).resolve()
    path = (root / filename).resolve()
    return path if path.parent == root else None


def _view(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    filename = row.get("filename") or ""
    path = archive_path(filename)
    row["download_available"] = (
        row.get("status") == "completed"
        and path is not None
        and path.is_file()
    )
    return row


def list_backups(limit: int = 100) -> list[dict[str, Any]]:
    rows = fetch_all(
        "SELECT id, status, trigger_kind, requested_by, requested_at, "
        "       started_at, finished_at, filename, size_bytes, sha256, "
        "       app_version, schema_version, report_files, message "
        "FROM shared.backup_runs ORDER BY requested_at DESC LIMIT :limit",
        limit=limit,
    )
    return [_view(row) for row in rows]


def active_backup() -> dict[str, Any] | None:
    row = fetch_one(
        "SELECT id, status, trigger_kind, requested_by, requested_at "
        "FROM shared.backup_runs "
        "WHERE status IN ('queued', 'running') ORDER BY requested_at LIMIT 1"
    )
    return row


def enqueue_backup(
    trigger_kind: str, requested_by: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Queue one archive, or report the one already active.

    The partial unique index is the race-proof part: two requests arriving at
    once cannot both insert. The loser reads the winner so the caller can say
    which backup is in the way rather than pretending it started a second one.
    """
    row = fetch_one(
        "INSERT INTO shared.backup_runs (trigger_kind, requested_by) "
        "VALUES (:trigger_kind, :requested_by) "
        "ON CONFLICT DO NOTHING "
        "RETURNING id, status, trigger_kind, requested_by, requested_at",
        trigger_kind=trigger_kind,
        requested_by=requested_by,
    )
    if row is not None:
        return row, None
    return None, active_backup()


def worker_status() -> dict[str, Any]:
    row = fetch_one(
        "SELECT last_seen_at, worker_version FROM shared.backup_worker "
        "WHERE singleton"
    )
    if row is None:
        return {"available": False, "last_seen_at": None, "version": ""}
    seen = row.get("last_seen_at")
    available = False
    if isinstance(seen, datetime):
        reference = seen if seen.tzinfo else seen.replace(tzinfo=UTC)
        available = (datetime.now(UTC) - reference).total_seconds() <= (
            WORKER_STALE_SECONDS
        )
    return {
        "available": available,
        "last_seen_at": seen,
        "version": row.get("worker_version") or "",
    }


def backup_stats() -> dict[str, int]:
    row = fetch_one(
        "SELECT count(*) FILTER (WHERE status = 'completed') AS completed, "
        "       count(*) FILTER (WHERE status = 'failed') AS failed, "
        "       count(*) FILTER (WHERE status IN ('queued', 'running')) AS active "
        "FROM shared.backup_runs"
    ) or {}
    return {key: int(row.get(key) or 0)
            for key in ("completed", "failed", "active")}


def backup_settings_view() -> dict[str, Any]:
    retention, row = backup_retention()
    stats = backup_stats()
    worker = worker_status()
    # A manually assembled Compose deployment may not carry
    # PATCHTRACK_VERSION in .env. The worker receives it from Compose, so its
    # heartbeat is the next-best source; "unknown" is honest if neither has it.
    return {
        "retention": retention,
        "min_retention": BACKUP_RETENTION_MIN,
        "max_retention": BACKUP_RETENTION_MAX,
        "updated_at": row["updated_at"] if row else None,
        "updated_by": row["updated_by"] if row else None,
        "app_version": get_settings().version or worker.get("version") or "unknown",
        "worker": worker,
        "worker_stale_seconds": WORKER_STALE_SECONDS,
        **stats,
        "pending_purge": max(0, stats["completed"] - retention),
    }


def queue_scheduled_backup() -> dict[str, Any]:
    """Scheduler entry point. A backup already in progress is not an error."""
    queued, active = enqueue_backup("schedule", "scheduler")
    if queued is not None:
        return {
            "status": "success",
            "queued": True,
            "backup_id": str(queued["id"]),
        }
    return {
        "status": "success",
        "queued": False,
        "backup_id": str(active["id"]) if active else None,
        "detail": "A backup is already queued or running.",
    }
