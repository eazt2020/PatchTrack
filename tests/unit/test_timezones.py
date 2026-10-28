"""The deployment-wide timezone.

One setting decides calendar boundaries for every service: period bounds,
daily snapshots, schedule defaults and report timestamps. Instants stay in
UTC; only the local calendar interpretation changes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from compliance.app import evaluator
from compliance.app.engine import period_bounds
from patchtrack_common import timezones


def test_common_zones_are_valid_iana_names():
    for name in timezones.COMMON:
        assert timezones.validate_timezone(name) == name


def test_utc_aliases_are_canonicalised():
    for alias in ("UTC", "utc", "Etc/UTC", "GMT", "Z"):
        assert timezones.validate_timezone(alias) == "UTC"


@pytest.mark.parametrize("bad", ["", "   ", "Mars/Olympus", "Asia/Nowhere"])
def test_unknown_zone_is_rejected(bad):
    with pytest.raises(ValueError):
        timezones.validate_timezone(bad)


def test_system_timezone_reads_the_setting(monkeypatch):
    monkeypatch.setattr(timezones, "fetch_one",
                        lambda *a, **k: {"value": {"name": "Asia/Kuala_Lumpur"}})
    assert timezones.system_timezone() == "Asia/Kuala_Lumpur"
    assert timezones.system_zone() == ZoneInfo("Asia/Kuala_Lumpur")


def test_system_timezone_falls_back_to_utc(monkeypatch):
    monkeypatch.setattr(timezones, "fetch_one", lambda *a, **k: None)
    assert timezones.system_timezone() == "UTC"
    monkeypatch.setattr(timezones, "fetch_one", lambda *a, **k: {"value": "nonsense"})
    assert timezones.system_timezone() == "UTC"


def test_default_period_uses_the_local_month():
    # 2026-10-31 18:00 UTC is already 2026-11-01 02:00 in Kuala Lumpur.
    now = datetime(2026, 10, 31, 18, 0, tzinfo=UTC)
    start, end = period_bounds(now=now, tz="Asia/Kuala_Lumpur")
    assert start == datetime(2026, 11, 1, tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))
    assert end == datetime(2026, 12, 1, tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))
    # The same instant is still October in UTC.
    utc_start, _ = period_bounds(now=now)
    assert utc_start.month == 10


def test_named_periods_are_anchored_in_the_zone():
    start, end = period_bounds("2026-10", tz="Asia/Kuala_Lumpur")
    assert start.isoformat() == "2026-10-01T00:00:00+08:00"
    assert end.isoformat() == "2026-11-01T00:00:00+08:00"
    q_start, q_end = period_bounds(quarter="2026-Q2", tz="Australia/Sydney")
    assert q_start.isoformat() == "2026-04-01T00:00:00+11:00"
    assert q_end.isoformat() == "2026-07-01T00:00:00+10:00"


def test_daily_snapshot_uses_the_local_day(monkeypatch):
    captured = {}

    def fake_create_snapshot(**kwargs):
        captured.update(kwargs)
        return {"snapshot_id": "snap"}

    monkeypatch.setattr(evaluator, "create_snapshot", fake_create_snapshot)
    monkeypatch.setattr(timezones, "system_zone",
                        lambda: ZoneInfo("Asia/Kuala_Lumpur"))
    evaluator.daily_snapshot(datetime(2026, 10, 31, 18, 0, tzinfo=UTC))
    assert captured["period_start"].date().isoformat() == "2026-11-01"
    assert captured["period_start"].hour == 0
    assert captured["period_start"].utcoffset().total_seconds() == 8 * 3600
