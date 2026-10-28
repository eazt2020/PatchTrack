"""The snapshot lookup must compare a calendar date, never a UTC instant.

``compliance.snapshots.period_start`` is a DATE. Comparing it to a UTC
datetime casts the date to local midnight, which in Asia/Kuala_Lumpur is the
previous afternoon in UTC — so a valid October request matched nothing and the
API answered 409 "No compliance snapshot exists for that period yet".
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import BackgroundTasks, HTTPException

from patchtrack_common.security import Principal
from reporting.app import main
from reporting.app.main import ReportRequest

ADMIN = Principal(
    user_id="00000000-0000-0000-0000-000000000002",
    username="admin",
    role="administrator",
    display_name="Administrator",
)


class _Request:
    class client:
        host = "127.0.0.1"
    headers: dict[str, str] = {}


def test_period_snapshot_lookup_uses_a_date(monkeypatch):
    captured: dict = {}

    def fake_fetch_one(sql: str, **params):
        captured.update(params)
        return None

    monkeypatch.setattr(main, "fetch_one", fake_fetch_one)
    with pytest.raises(HTTPException) as raised:
        main.request_report(
            ReportRequest(report_type="monthly_management",
                          reporting_month="2026-10"),
            BackgroundTasks(), _Request(), ADMIN,
        )

    assert raised.value.status_code == 409
    assert captured["ps"] == date(2026, 10, 1)
    assert type(captured["ps"]) is date
