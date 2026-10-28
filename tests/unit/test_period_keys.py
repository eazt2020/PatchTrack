"""The executive dashboard's month, quarter and year selection.

A period key is what the browser and the API exchange, so the parsing must be
exact: a key that is nearly right must be refused, not rounded to a nearby
period. These tests pin the accepted forms, the inclusive end dates (including
leap years and year-end), and the labels a reader sees.
"""

from __future__ import annotations

from datetime import date

import pytest

from compliance.app.engine import (
    parse_period_key,
    period_key,
    period_label,
)


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("2026-10", ("month", date(2026, 10, 1), date(2026, 10, 31))),
        ("2026-02", ("month", date(2026, 2, 1), date(2026, 2, 28))),
        ("2024-02", ("month", date(2024, 2, 1), date(2024, 2, 29))),
        ("2026-12", ("month", date(2026, 12, 1), date(2026, 12, 31))),
        ("2026-Q1", ("quarter", date(2026, 1, 1), date(2026, 3, 31))),
        ("2026-Q2", ("quarter", date(2026, 4, 1), date(2026, 6, 30))),
        ("2026-Q3", ("quarter", date(2026, 7, 1), date(2026, 9, 30))),
        ("2026-Q4", ("quarter", date(2026, 10, 1), date(2026, 12, 31))),
        ("2026", ("year", date(2026, 1, 1), date(2026, 12, 31))),
        ("2024", ("year", date(2024, 1, 1), date(2024, 12, 31))),
        (" 2026-10 ", ("month", date(2026, 10, 1), date(2026, 10, 31))),
    ],
)
def test_parse_period_key(key, expected):
    assert parse_period_key(key) == expected


@pytest.mark.parametrize(
    "key",
    [
        "",
        "2026-13",          # no thirteenth month
        "2026-00",
        "2026-Q0",          # quarter outside 1-4
        "2026-Q5",
        "2026-q4",          # the canonical form is upper case
        "2026-1",           # a month must be two digits
        "26-10",
        "2026-10-01",       # a date is not a period
        "October 2026",
        "0000",
        "abcd",
    ],
)
def test_parse_period_key_rejects_anything_else(key):
    with pytest.raises(ValueError):
        parse_period_key(key)


@pytest.mark.parametrize(
    ("kind", "start", "key", "label"),
    [
        ("month", date(2026, 10, 1), "2026-10", "October 2026"),
        ("quarter", date(2026, 10, 1), "2026-Q4", "Q4 2026"),
        ("quarter", date(2026, 1, 1), "2026-Q1", "Q1 2026"),
        ("year", date(2026, 1, 1), "2026", "2026"),
    ],
)
def test_key_and_label_round_trip(kind, start, key, label):
    assert period_key(kind, start) == key
    assert period_label(kind, start) == label
    parsed_kind, parsed_start, parsed_end = parse_period_key(key)
    assert parsed_kind == kind
    assert parsed_start == start
    assert parsed_end >= start


def test_key_rejects_unknown_kind():
    with pytest.raises(ValueError):
        period_key("week", date(2026, 10, 1))
    with pytest.raises(ValueError):
        period_label("week", date(2026, 10, 1))
