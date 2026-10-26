"""Schedule next-run calculation.

Schedules run in a configured timezone and persist across restarts; the next
run must be deterministic given a clock, and must always be in the future.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from reporting.app.scheduler import compute_next_run

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
