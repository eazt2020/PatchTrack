"""Report schedule evaluation.

Schedules persist in PostgreSQL so they survive container recreation.
``job_executions`` has a unique (schedule_id, period_start) key, making a
second scheduler instance a no-op instead of a duplicate report.
"""

from __future__ import annotations

import calendar
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from patchtrack_common.db import connection, fetch_all


def compute_next_run(
    cadence: str, day_of_month: int, run_hour: int, timezone: str,
    *, now: datetime | None = None,
) -> datetime:
    """Return the next scheduled execution in UTC."""
    now = now or datetime.now(UTC)
    try:
        tz = ZoneInfo(timezone)
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
    """Claim a (schedule, period) pair. False means someone else has it."""
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
    except Exception:  # noqa: BLE001 - unique violation means already claimed
        return False


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


def period_for_schedule(schedule: dict, now: datetime) -> tuple[datetime, datetime]:
    """The period a scheduled report covers: the period that just ended."""
    tz = ZoneInfo(schedule.get("timezone") or "UTC")
    local = now.astimezone(tz)
    month_start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous_end = month_start
    if schedule["cadence"] == "quarterly":
        quarter_month = 3 * ((month_start.month - 1) // 3)
        previous_start = month_start.replace(
            month=quarter_month, year=month_start.year
        )
        if previous_start >= month_start:
            previous_start = previous_start.replace(year=previous_start.year - 1)
    else:
        previous_start = (month_start - timedelta(days=1)).replace(day=1)
    return previous_start.astimezone(UTC), previous_end.astimezone(UTC)
