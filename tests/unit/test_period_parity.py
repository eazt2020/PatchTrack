"""Period resolution must agree between compliance and reporting.

The two services carry independent copies of ``period_bounds`` so they remain
separately deployable. A divergence would let the dashboard, the API and the
PDF disagree about which period a number belongs to, which the business
requirements forbid. This test is the guard against that drift.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from compliance.app import engine as compliance_engine
from reporting.app import periods as reporting_periods

NOW = datetime(2026, 10, 26, 12, 34, 56, tzinfo=UTC)

VECTORS = [
    {},
    {"reporting_month": "2026-01"},
    {"reporting_month": "2026-10"},
    {"reporting_month": "2026-12"},
    {"reporting_month": "2024-02"},
    {"quarter": "2026-Q1"},
    {"quarter": "2026-Q2"},
    {"quarter": "2026-Q3"},
    {"quarter": "2026-Q4"},
    {"quarter": "2026 q4"},
    {"quarter": "2027- Q1"},
]


@pytest.mark.parametrize("vector", VECTORS)
def test_both_implementations_agree(vector):
    assert compliance_engine.period_bounds(**vector, now=NOW) == \
        reporting_periods.period_bounds(**vector, now=NOW)


@pytest.mark.parametrize("vector", [
    {"reporting_month": "2026-13"},
    {"reporting_month": "October"},
    {"quarter": "2026-Q5"},
    {"quarter": "2026-QX"},
])
def test_both_implementations_reject_the_same_input(vector):
    with pytest.raises(ValueError):
        compliance_engine.period_bounds(**vector, now=NOW)
    with pytest.raises(ValueError):
        reporting_periods.period_bounds(**vector, now=NOW)


@pytest.mark.parametrize("vector", VECTORS)
def test_periods_are_contiguous_and_non_empty(vector):
    start, end = compliance_engine.period_bounds(**vector, now=NOW)
    assert end > start
    assert (end - start).days in (28, 29, 30, 31, 90, 91, 92)


def test_default_period_is_the_month_containing_now():
    start, end = compliance_engine.period_bounds(now=NOW)
    assert (start.year, start.month, start.day) == (2026, 10, 1)
    assert (end.year, end.month, end.day) == (2026, 11, 1)
