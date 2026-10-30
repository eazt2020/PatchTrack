"""Schedule next-run calculation.

Schedules run in a configured timezone and persist across restarts; the next
run must be deterministic given a clock, and must always be in the future.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from reporting.app.scheduler import compute_next_run, period_for_schedule

# 2026-10-26 12:00 in Sydney (AEDT, UTC+11) == 01:00 UTC.
NOW = datetime(2026, 10, 26, 1, 0, tzinfo=UTC)


def test_monthly_run_later_today_stays_today():
    result = compute_next_run("monthly", 26, 18, "Australia/Sydney", now=NOW)
    # 18:00 Sydney on the 26th == 07:00 UTC on the 26th.
    assert result == datetime(2026, 10, 26, 7, 0, tzinfo=UTC)


def test_run_time_already_passed_moves_to_next_month():
    """09:00 Sydney on the 26th has passed; the next one is a month later.

    09:00 AEDT on 2026-11-26 == 22:00 UTC on 2026-11-25.
    """
    result = compute_next_run("monthly", 26, 9, "Australia/Sydney", now=NOW)
    assert result == datetime(2026, 11, 25, 22, 0, tzinfo=UTC)


def test_next_run_is_always_in_the_future():
    for hour in range(0, 24, 3):
        assert compute_next_run("monthly", 26, hour, "UTC", now=NOW) > NOW


def test_quarterly_cadence_advances_three_months():
    """Today's 09:00 UTC has not passed at 01:00 UTC, so it is the next run."""
    assert compute_next_run("quarterly", 26, 9, "UTC", now=NOW) == \
        datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
    # Once today's slot has passed, the next one is a quarter later.
    later = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)
    assert compute_next_run("quarterly", 26, 9, "UTC", now=later) == \
        datetime(2027, 1, 26, 9, 0, tzinfo=UTC)


def test_quarterly_cadence_from_q4_rolls_into_next_year():
    result = compute_next_run(
        "quarterly", 26, 9, "UTC",
        now=datetime(2026, 11, 27, tzinfo=UTC))
    assert result == datetime(2027, 2, 26, 9, 0, tzinfo=UTC)


def test_day_of_month_is_clamped_to_28():
    """A 31st schedule must still run in February."""
    result = compute_next_run(
        "monthly", 31, 9, "UTC", now=datetime(2027, 1, 31, 12, 0, tzinfo=UTC))
    assert result == datetime(2027, 2, 28, 9, 0, tzinfo=UTC)


def test_timezone_changes_the_utc_instant():
    utc_run = compute_next_run("monthly", 26, 9, "UTC", now=NOW)
    sydney_run = compute_next_run("monthly", 26, 9, "Australia/Sydney", now=NOW)
    assert utc_run != sydney_run


def test_unknown_timezone_falls_back_to_utc():
    result = compute_next_run("monthly", 26, 18, "Not/AZone", now=NOW)
    assert result == datetime(2026, 10, 26, 18, 0, tzinfo=UTC)


def test_result_is_timezone_aware_utc():
    result = compute_next_run("monthly", 26, 18, "Europe/London", now=NOW)
    assert result.tzinfo is not None
    assert result.utcoffset().total_seconds() == 0


def test_dst_transition_uses_the_local_wall_clock():
    """Europe/London moves to BST in late March; 09:00 local stays 09:00 local."""
    before = compute_next_run(
        "monthly", 30, 9, "Europe/London",
        now=datetime(2027, 3, 1, tzinfo=UTC))
    # 2027-03-30 09:00 BST == 08:00 UTC.
    assert before == datetime(2027, 3, 30, 8, 0, tzinfo=UTC)


@pytest.mark.parametrize("cadence", ["monthly", "quarterly"])
def test_cadence_always_produces_a_later_time(cadence):
    result = compute_next_run(cadence, 1, 0, "UTC", now=NOW)
    assert result > NOW


# --------------------------------------------------------------- report periods
def _quarterly(now: datetime, tz: str = "UTC"):
    return period_for_schedule(
        {"cadence": "quarterly", "timezone": tz}, now)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # A quarterly report run in the first month of a quarter covers the
        # quarter that just ended, not a rolling two-month window.
        (datetime(2026, 1, 26, 9, tzinfo=UTC), (date(2025, 10, 1), date(2026, 1, 1))),
        (datetime(2026, 4, 26, 9, tzinfo=UTC), (date(2026, 1, 1), date(2026, 4, 1))),
        (datetime(2026, 7, 26, 9, tzinfo=UTC), (date(2026, 4, 1), date(2026, 7, 1))),
        (datetime(2026, 10, 26, 9, tzinfo=UTC), (date(2026, 7, 1), date(2026, 10, 1))),
    ],
)
def test_quarterly_period_starts_at_the_previous_quarter(now, expected):
    assert _quarterly(now) == expected


def test_monthly_period_is_the_previous_calendar_month():
    result = period_for_schedule(
        {"cadence": "monthly", "timezone": "UTC"},
        datetime(2026, 10, 26, 9, tzinfo=UTC),
    )
    assert result == (date(2026, 9, 1), date(2026, 10, 1))


def test_monthly_period_crosses_the_year_boundary():
    result = period_for_schedule(
        {"cadence": "monthly", "timezone": "UTC"},
        datetime(2027, 1, 26, 9, tzinfo=UTC),
    )
    assert result == (date(2026, 12, 1), date(2027, 1, 1))


def test_quarterly_period_crosses_the_year_boundary():
    result = _quarterly(datetime(2027, 1, 26, 9, tzinfo=UTC))
    assert result == (date(2026, 10, 1), date(2027, 1, 1))


def test_period_uses_the_schedule_local_date_not_the_utc_date():
    """Midnight in Kuala Lumpur is the previous afternoon in UTC.

    The scheduler still looks up the snapshot for 1 October, not 30 September.
    """
    # 2026-10-01 00:30 +08 == 2026-09-30 16:30 UTC.
    run = datetime(2026, 9, 30, 16, 30, tzinfo=UTC)
    assert _quarterly(run, "Asia/Kuala_Lumpur") == (
        date(2026, 7, 1), date(2026, 10, 1))


def test_period_uses_local_date_across_a_dst_transition():
    """1 April 00:30 Sydney is still 31 March in UTC."""
    run = datetime(2026, 3, 31, 13, 30, tzinfo=UTC)
    assert _quarterly(run, "Australia/Sydney") == (
        date(2026, 1, 1), date(2026, 4, 1))


def test_period_falls_back_to_utc_for_an_unknown_timezone():
    run = datetime(2026, 10, 1, 0, 30, tzinfo=UTC)
    assert _quarterly(run, "Not/AZone") == (
        date(2026, 7, 1), date(2026, 10, 1))


# ---------------------------------------------------------------- claim failure
class _Orig:
    def __init__(self, sqlstate: str | None = None, message: str = "") -> None:
        self.sqlstate = sqlstate
        self._message = message

    def __str__(self) -> str:
        return self._message


def test_duplicate_claim_is_a_skip(monkeypatch):
    from sqlalchemy.exc import IntegrityError

    from reporting.app import scheduler

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *args, **kwargs):
            raise IntegrityError("INSERT", {}, _Orig("23505", "duplicate key"))

    monkeypatch.setattr(scheduler, "connection", lambda: Connection())
    assert scheduler.claim_execution("s1", date(2026, 9, 1)) is False


def test_an_infrastructure_failure_is_not_swallowed(monkeypatch):
    from sqlalchemy.exc import OperationalError

    from reporting.app import scheduler

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *args, **kwargs):
            raise OperationalError("INSERT", {}, _Orig(None, "connection refused"))

    monkeypatch.setattr(scheduler, "connection", lambda: Connection())
    with pytest.raises(OperationalError):
        scheduler.claim_execution("s1", date(2026, 9, 1))


def test_a_non_unique_integrity_error_is_not_swallowed(monkeypatch):
    from sqlalchemy.exc import IntegrityError

    from reporting.app import scheduler

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *args, **kwargs):
            raise IntegrityError("INSERT", {}, _Orig("23503", "foreign key"))

    monkeypatch.setattr(scheduler, "connection", lambda: Connection())
    with pytest.raises(IntegrityError):
        scheduler.claim_execution("s1", date(2026, 9, 1))
