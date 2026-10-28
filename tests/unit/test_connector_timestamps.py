"""A connector must never invent an assessment timestamp.

A source that omits or corrupts the timestamp is reporting an *unknown*
assessment time. Substituting the current time would make stale data look
fresh, which is exactly the integrity failure the reports exist to prevent.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from inventory.app.connectors import sccm, satellite


@pytest.mark.parametrize("parse", [satellite._parse_time, sccm._parse_time])
@pytest.mark.parametrize("value", [None, "", "not-a-date"])
def test_missing_or_malformed_timestamp_is_unknown(parse, value):
    assert parse(value) is None


@pytest.mark.parametrize(
    ("parse", "value"),
    [
        (satellite._parse_time, "2026-09-30T16:30:00Z"),
        (satellite._parse_time, "2026-09-30T16:30:00+00:00"),
        (sccm._parse_time, "2026-09-30T16:30:00Z"),
        (sccm._parse_time, "/Date(1790785800000)/"),
    ],
)
def test_valid_timestamp_is_parsed(parse, value):
    parsed = parse(value)
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.astimezone(UTC).year == 2026


def test_naive_timestamp_is_treated_as_utc():
    assert satellite._parse_time("2026-09-30T16:30:00") == datetime(
        2026, 9, 30, 16, 30, tzinfo=UTC)


def test_satellite_worker_count_is_bounded():
    def client(workers):
        return satellite.SatelliteClient(
            "https://satellite.example.com", username="u", password="p",
            workers=workers, client=object())

    assert client(0).workers == 1
    assert client(4).workers == 4
    assert client(999).workers == 8


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self):
        self.hosts = [
            {"id": 1, "name": "a.example.com", "updated_at": "2026-09-30T10:00:00Z"},
            {"id": 2, "name": "b.example.com"},  # no timestamp -> unknown
        ]

    def get(self, url, params=None):
        if "/errata" in url:
            host = url.split("/hosts/")[1].split("/")[0]
            return _Response({"results": [{
                "errata_id": f"RHSA-{host}", "title": "t",
                "severity": "critical", "issued": "2026-09-01T00:00:00Z",
            }], "total": 1})
        return _Response({"results": self.hosts, "total": 2})


def test_concurrent_errata_preserves_host_order_and_unknown_timestamps():
    connector = satellite.SatelliteClient(
        "https://satellite.example.com", username="u", password="p",
        workers=4, client=_FakeClient())
    assessments = connector.fetch_assessments()
    assert [a.hostname for a in assessments] == ["a", "b"]
    assert assessments[0].assessed_at is not None
    assert assessments[1].assessed_at is None
    assert assessments[1].findings[0].advisory_id == "RHSA-2"
