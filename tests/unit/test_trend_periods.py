"""The dashboard's month/quarter/year basis must land on real calendar periods.

A year is not 365 days and a quarter is not three equal months for the purpose
of grouping recorded snapshots: the cut-off has to be the first day of the
first period inside the window, or the chart silently drops the oldest point
(a 12-month window that starts mid-month) or keeps a phantom one.
"""

from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

COMPLIANCE_DIR = Path(__file__).resolve().parents[2] / "services" / "compliance"


@pytest.fixture(scope="module")
def trends():
    for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
        del sys.modules[name]
    sys.path.insert(0, str(COMPLIANCE_DIR))
    try:
        return importlib.import_module("app.main")
    finally:
        sys.path.remove(str(COMPLIANCE_DIR))


NOW = datetime(2026, 10, 26, 15, 30, tzinfo=UTC)


@pytest.mark.parametrize(("unit", "expected"), [
    ("month", datetime(2026, 10, 1, tzinfo=UTC)),
    ("quarter", datetime(2026, 10, 1, tzinfo=UTC)),
    ("year", datetime(2026, 1, 1, tzinfo=UTC)),
])
def test_truncate_finds_the_period_start(trends, unit: str, expected: datetime):
    assert trends._truncate(NOW, unit) == expected


def test_truncate_quarter_handles_each_month_of_the_quarter(trends):
    for month in (7, 8, 9):
        assert trends._truncate(NOW.replace(month=month), "quarter").month == 7


@pytest.mark.parametrize(("unit", "steps", "expected"), [
    ("month", 0, datetime(2026, 10, 1, tzinfo=UTC)),
    ("month", 11, datetime(2025, 11, 1, tzinfo=UTC)),
    ("quarter", 7, datetime(2025, 1, 1, tzinfo=UTC)),
    ("year", 4, datetime(2022, 1, 1, tzinfo=UTC)),
])
def test_shift_back_walks_calendar_periods(trends, unit: str, steps: int,
                                          expected: datetime):
    assert trends._shift_back(trends._truncate(NOW, unit), unit, steps) == expected


def test_shift_back_crosses_a_year_boundary(trends):
    start = trends._truncate(datetime(2026, 1, 15, tzinfo=UTC), "month")
    assert trends._shift_back(start, "month", 1) == datetime(2025, 12, 1, tzinfo=UTC)


@pytest.mark.parametrize(("unit", "when", "expected"), [
    ("month", datetime(2026, 10, 1), "Oct 26"),
    ("quarter", datetime(2026, 10, 1), "Q4 2026"),
    ("year", datetime(2026, 1, 1), "2026"),
])
def test_labels_read_as_calendar_periods(trends, unit: str, when: datetime,
                                         expected: str):
    assert trends._trend_label(unit, when) == expected


def test_every_basis_has_a_window(trends):
    # The endpoint validates the name through Literal; the table is the single
    # source of truth for the unit and the default point count.
    assert set(trends.TREND_PERIODS) == {"monthly", "quarterly", "yearly"}
    for unit, points, noun in trends.TREND_PERIODS.values():
        assert unit in {"month", "quarter", "year"}
        assert points >= 2
        assert noun.endswith("s")
