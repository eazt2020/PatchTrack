"""Deleting archived reports is an administrator-only, deliberate act.

The PDF and the metadata row go together; a report still being generated is
refused rather than removed underneath the worker, and delivery history is
cascade-deleted with the report it belongs to.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from patchtrack_common.config import get_settings
from patchtrack_common.db import fetch_one
from patchtrack_common.security import Principal
from reporting.app.main import (
    ReportDeleteRequest,
    bulk_delete_reports,
    delete_report,
)

ADMIN = Principal(
    user_id="00000000-0000-0000-0000-000000000002",
    username="admin",
    role="administrator",
    display_name="Administrator",
)
MANAGER = Principal(
    user_id="00000000-0000-0000-0000-000000000003",
    username="manager",
    role="report_manager",
)


class _Request:
    client = SimpleNamespace(host="127.0.0.1")
    headers: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _database(migrated_database):
    return migrated_database


@pytest.fixture
def report_dir(tmp_path, monkeypatch):
    """Point the archive root at the test directory for the path guard."""
    monkeypatch.setenv("PATCHTRACK_REPORT_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def add_report(engine, root, *, status: str = "completed") -> tuple[str, object]:
    report_id = str(uuid4())
    path = root / f"{report_id}.pdf"
    path.write_bytes(b"%PDF-1.4\ntest\n%%EOF\n")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO reporting.reports "
                "(id, report_type, period_start, period_end, status, "
                " requested_by, filename, file_path) "
                "VALUES (:id, 'monthly_management', '2026-09-01', "
                "        '2026-09-30', :status, 'reporter', :name, :path)"
            ),
            {"id": report_id, "status": status,
             "name": path.name, "path": str(path)},
        )
        conn.execute(
            text(
                "INSERT INTO reporting.delivery_attempts "
                "(report_id, status, detail) VALUES (:id, 'sent', 'test')"
            ),
            {"id": report_id},
        )
    return report_id, path


def test_administrator_deletes_the_row_the_file_and_delivery_history(
    engine, report_dir,
) -> None:
    first_id, first_path = add_report(engine, report_dir)
    second_id, second_path = add_report(engine, report_dir)
    keep_id, keep_path = add_report(engine, report_dir)

    result = bulk_delete_reports(
        ReportDeleteRequest(ids=[first_id, second_id]), _Request(), ADMIN)

    assert result["count"] == 2
    assert sorted(result["deleted"]) == sorted([first_id, second_id])
    assert not first_path.exists()
    assert not second_path.exists()
    assert keep_path.exists()
    for deleted_id in (first_id, second_id):
        assert fetch_one(
            "SELECT 1 FROM reporting.reports WHERE id = :id", id=deleted_id,
        ) is None
        assert fetch_one(
            "SELECT 1 FROM reporting.delivery_attempts WHERE report_id = :id",
            id=deleted_id,
        ) is None
    assert fetch_one(
        "SELECT 1 FROM reporting.reports WHERE id = :id", id=keep_id,
    ) is not None
    event = fetch_one(
        "SELECT outcome FROM shared.audit_events "
        "WHERE event_type = 'report.deleted' AND target_id = :id",
        id=first_id,
    )
    assert event["outcome"] == "success"
    assert fetch_one(
        "SELECT outcome FROM shared.audit_events "
        "WHERE event_type = 'report.bulk_deleted' ORDER BY occurred_at DESC",
    )["outcome"] == "success"


def test_a_report_still_being_generated_is_refused(engine, report_dir) -> None:
    report_id, path = add_report(engine, report_dir, status="generating")

    result = bulk_delete_reports(
        ReportDeleteRequest(ids=[report_id]), _Request(), ADMIN)

    assert result["count"] == 0
    assert result["refused"][0]["id"] == report_id
    assert path.exists()
    assert fetch_one(
        "SELECT 1 FROM reporting.reports WHERE id = :id", id=report_id,
    ) is not None


def test_unknown_ids_are_reported_not_fatal(engine, report_dir) -> None:
    report_id, _ = add_report(engine, report_dir)
    absent = str(uuid4())

    result = bulk_delete_reports(
        ReportDeleteRequest(ids=[report_id, absent]), _Request(), ADMIN)

    assert result["count"] == 1
    assert result["not_found"] == [absent]


def test_single_delete_raises_not_found(engine, report_dir) -> None:
    with pytest.raises(HTTPException) as exc:
        delete_report(uuid4(), _Request(), ADMIN)
    assert exc.value.status_code == 404


def test_report_manager_does_not_hold_reports_delete() -> None:
    assert not MANAGER.can("reports:delete")
    assert ADMIN.can("reports:delete")
