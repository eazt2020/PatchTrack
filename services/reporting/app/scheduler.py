"""Report schedule evaluation.

Schedules persist in PostgreSQL so they survive container recreation.
``job_executions`` has a unique (schedule_id, period_start) key, making a
second scheduler instance a no-op instead of a duplicate report.
"""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from patchtrack_common import timezones as timezone_settings
from patchtrack_common.db import connection, fetch_all


def compute_next_run(
    cadence: str, day_of_month: int, run_hour: int, timezone: str,
    *, now: datetime | None = None,
) -> datetime:
    """Return the next scheduled execution in UTC."""
    now = now or datetime.now(UTC)
    try:
        tz = ZoneInfo(timezone or timezone_settings.system_timezone())
    except Exception:  # noqa: BLE001 - unknown tz falls back to UTC
        tz = ZoneInfo("UTC")
    local = now.astimezone(tz)

    def for_month(year: int, month: int) -> datetime:
        # Clamp to the last day of the target month so a "31st" schedule runs on
        # 28 February, while a "26th" schedule keeps the 26th in every month.
        last_day = calendar.monthrange(year, month)[1]
        return local.replace(
            year=year, month=month, day=min(day_of_month, last_day),
            hour=run_hour, minute=0, second=0, microsecond=0,
        )

    candidate = for_month(local.year, local.month)
    if candidate <= local:
        month = local.month + (3 if cadence == "quarterly" else 1)
        year = local.year
        while month > 12:
            month -= 12
            year += 1
        candidate = for_month(year, month)
    return candidate.astimezone(UTC)


def due_schedules(*, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(UTC)
    return fetch_all(
        "SELECT * FROM reporting.schedules WHERE enabled "
        "AND (next_run_at IS NULL OR next_run_at <= :now) ORDER BY id",
        now=now,
    )


def claim_execution(schedule_id: str, period_start) -> bool:
    """Claim a (schedule, period) pair. False means someone else has it.

    Only the expected uniqueness violation is treated as "already claimed". A
    connection, permission or timeout failure must reach the caller, otherwise
    an infrastructure outage looks like a normal skipped run.
    """
    try:
        with connection() as conn:
            conn.execute(
                text(
                    "INSERT INTO reporting.job_executions "
                    "(schedule_id, period_start) VALUES (:s, :p)"
                ),
                {"s": schedule_id, "p": period_start},
            )
        return True
    except IntegrityError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None)
        if sqlstate == "23505" or "duplicate key" in str(exc.orig).lower():
            return False
        raise


def finish_execution(schedule_id: str, period_start, status: str, message: str,
                     next_run: datetime) -> None:
    with connection() as conn:
        conn.execute(
            text(
                "UPDATE reporting.job_executions SET finished_at = now(), "
                "status = :s, message = :m WHERE schedule_id = :sid "
                "AND period_start = :p"
            ),
            {"s": status, "m": message[:1000], "sid": schedule_id, "p": period_start},
        )
        conn.execute(
            text(
                "UPDATE reporting.schedules SET last_run_at = now(), "
                "last_run_status = :s, next_run_at = :n, updated_at = now() "
                "WHERE id = :id"
            ),
            {"s": status, "n": next_run, "id": schedule_id},
        )


def _shift_months(first_of_month: date, months: int) -> date:
    """Move a first-of-month date by whole months, carrying the year."""
    index = (first_of_month.year * 12 + first_of_month.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def period_for_schedule(schedule: dict, now: datetime) -> tuple[date, date]:
    """The calendar period a scheduled report covers: the one that just ended.

    The result is ``(period_start, period_end)`` as **local calendar dates**,
    not UTC instants. A schedule's time zone decides *when* it runs; it must
    not change *which* report period is selected. Converting the boundary to
    UTC would move a midnight boundary in Asia/Kuala_Lumpur to the previous
    day and make the snapshot lookup miss the period entirely.

    ``period_end`` is exclusive, matching the snapshot convention: a monthly
    run in October covers 1 September to 1 October.
    """
    try:
        tz = ZoneInfo(schedule.get("timezone")
                      or timezone_settings.system_timezone())
    except Exception:  # noqa: BLE001 - unknown tz falls back to UTC
        tz = ZoneInfo("UTC")
    month_start = now.astimezone(tz).date().replace(day=1)
    backlog = 3 if schedule["cadence"] == "quarterly" else 1
    return _shift_months(month_start, -backlog), month_start
