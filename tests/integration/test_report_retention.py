"""Report file retention is an administrator setting, not a deployment constant.

Changing the window in Settings applies immediately: each completed report's
expiry is recomputed from its completion date, the purge runs in the same
request, and the report row survives as a tombstone carrying the digest.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from patchtrack_common.db import fetch_one
from patchtrack_common.security import Principal
from reporting.app.main import (
    ReportRetentionUpdate,
    get_report_retention,
    set_report_retention,
)
from reporting.app.service import (
    REPORT_RETENTION_DEFAULT,
    REPORT_RETENTION_MAX,
    REPORT_RETENTION_MIN,
)

ADMIN = Principal(
    user_id="00000000-0000-0000-0000-000000000002",
    username="admin",
    role="administrator",
    display_name="Administrator",
)


class _Request:
    client = SimpleNamespace(host="127.0.0.1")
    headers: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _database(migrated_database):
    return migrated_database


@pytest.fixture
def default_retention(engine):
    with engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM shared.app_settings WHERE key = 'report_retention_days'"
        ))
        conn.execute(text("DELETE FROM reporting.reports"))
    yield


def add_report(engine, tmp_path, *, completed_days_ago: int,
               status: str = "completed") -> tuple[str, object]:
    report_id = str(uuid4())
    path = tmp_path / f"{report_id}.pdf"
    path.write_bytes(b"%PDF-1.4\ntest\n%%EOF\n")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO reporting.reports "
                "(id, report_type, period_start, period_end, status, "
                " requested_by, completed_at, filename, file_path) "
                "VALUES (:id, 'monthly_management', '2026-09-01', "
                "        '2026-09-30', :status, 'tester', :done, :name, :path)"
            ),
            {
                "id": report_id,
                "status": status,
                "done": datetime.now(UTC) - timedelta(days=completed_days_ago)
                if status == "completed" else None,
                "name": path.name,
                "path": str(path),
            },
        )
    return report_id, path


def test_the_default_window_is_used_until_an_administrator_changes_it(
    default_retention,
) -> None:
    value = get_report_retention(ADMIN)
    assert value["days"] == REPORT_RETENTION_DEFAULT == 2555
    assert value["min_days"] == REPORT_RETENTION_MIN == 30
    assert value["max_days"] == REPORT_RETENTION_MAX == 36500
    assert value["updated_at"] is None


def test_saving_the_window_recomputes_expiry_and_purges_at_once(
    engine, tmp_path, default_retention,
) -> None:
    old_id, old_path = add_report(engine, tmp_path, completed_days_ago=400)
    kept_id, kept_path = add_report(engine, tmp_path, completed_days_ago=10)

    result = set_report_retention(
        ReportRetentionUpdate(days=365), _Request(), ADMIN
    )

    assert result == {"days": 365, "purged_reports": 1}
    assert not old_path.exists()
    assert kept_path.exists()

    old = fetch_one(
        "SELECT status, file_path, error_summary, expires_at "
        "FROM reporting.reports WHERE id = :id", id=old_id,
    )
    assert old["status"] == "completed"
    assert old["file_path"] == ""
    assert old["error_summary"] == "Purged by retention policy"
    assert old["expires_at"] is not None

    kept = fetch_one(
        "SELECT expires_at FROM reporting.reports WHERE id = :id", id=kept_id,
    )
    assert kept["expires_at"] > datetime.now(UTC)

    stored = fetch_one(
        "SELECT value, updated_by FROM shared.app_settings "
        "WHERE key = 'report_retention_days'"
    )
    assert stored["value"] == {"days": 365}
    assert stored["updated_by"] == "admin"

    changed = fetch_one(
        "SELECT context FROM shared.audit_events "
        "WHERE event_type = 'settings.updated' "
        "  AND target_id = 'report_retention_days' ORDER BY id DESC"
    )
    assert changed["context"] == {"days": 365, "purged_reports": 1}


def test_a_longer_window_brings_no_file_back_but_extends_the_expiry(
    engine, tmp_path, default_retention,
) -> None:
    report_id, path = add_report(engine, tmp_path, completed_days_ago=400)

    set_report_retention(ReportRetentionUpdate(days=365), _Request(), ADMIN)
    assert not path.exists()

    set_report_retention(ReportRetentionUpdate(days=2555), _Request(), ADMIN)
    row = fetch_one(
        "SELECT file_path, expires_at FROM reporting.reports WHERE id = :id",
        id=report_id,
    )
    assert row["file_path"] == ""
    assert row["expires_at"] > datetime.now(UTC)


@pytest.mark.parametrize("days", [0, 29, 36501])
def test_a_window_outside_the_safe_range_is_refused(days: int) -> None:
    with pytest.raises(ValidationError):
        ReportRetentionUpdate(days=days)
