"""The executive dashboard's month, quarter and year views.

A historical view must be built from the snapshot recorded inside the period,
not from a re-evaluation of today's estate. These tests pin the three
consequences: the tiles and panels come from the frozen snapshot, an empty
period is a 404 rather than a blank success, and a dimension filter is refused
because the snapshot holds group totals rather than server rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from compliance.app.evaluator import create_snapshot
from compliance.app.main import snapshot_periods, summary
from inventory.app.connectors.base import (
    AssetRecord,
    NormalisedFinding,
    PatchAssessment,
)
from inventory.app.sync import ingest_assessments, upsert_asset
from patchtrack_common.security import Principal

VIEWER = Principal(
    user_id="00000000-0000-0000-0000-000000000003",
    username="viewer",
    role="viewer",
    display_name="Viewer",
)

PERIOD_START = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
ASSESSED_AT = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
SNAPSHOT_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def _record(source_id: str, hostname: str, **overrides) -> AssetRecord:
    base = {
        "source_id": source_id,
        "hostname": hostname,
        "fqdn": f"{hostname}.example.com",
        "ip_address": "10.0.0.1",
        "os_name": "Red Hat Enterprise Linux",
        "os_version": "9.4",
        "asset_type": "server",
        "asset_status": "In Use",
        "environment": "Production",
        "business_group": "Payments",
        "application_group": "Core",
        "asset_owner": "Platform",
        "department": "IT",
        "location": "DC1",
        "lifecycle_status": "active",
        "classification": "internal",
        "source_created_at": None,
        "source_modified_at": None,
        "raw": {},
    }
    base.update(overrides)
    return AssetRecord(**base)


def _asset(engine, **overrides) -> str:
    values = {"source_id": "AE-1", "hostname": "srv-001", **overrides}
    with engine.begin() as conn:
        run_id = str(conn.execute(
            text("INSERT INTO inventory.sync_runs (integration_key, trigger) "
                 "VALUES ('assetexplorer', 'manual') RETURNING id")
        ).first()[0])
        asset_id, _ = upsert_asset(conn, _record(**values), run_id)
    return asset_id


def _assess(engine, hostname: str, findings: tuple[NormalisedFinding, ...]) -> None:
    with engine.begin() as conn:
        run_id = str(conn.execute(
            text("INSERT INTO inventory.sync_runs (integration_key, trigger) "
                 "VALUES ('satellite', 'manual') RETURNING id")
        ).first()[0])
    ingest_assessments([
        PatchAssessment(
            source_system="satellite", source_ref=f"SAT-{hostname}",
            hostname=hostname, fqdn=f"{hostname}.example.com",
            ip_address="10.0.0.1", os_name="Red Hat Enterprise Linux",
            os_version="9.4", assessed_at=ASSESSED_AT, status="complete",
            findings=findings,
        )
    ], run_id=run_id, source_key="satellite")


def _september_snapshot(engine) -> dict:
    """One compliant RHEL, one non-compliant RHEL, one unassessed Windows."""
    _asset(engine)
    _asset(engine, source_id="AE-2", hostname="srv-002", ip_address="10.0.0.2")
    _asset(
        engine, source_id="AE-3", hostname="srv-003", ip_address="10.0.0.3",
        os_name="Microsoft Windows Server", os_version="2022",
        environment="Staging", business_group="Others",
    )
    _assess(engine, "srv-001", ())
    _assess(engine, "srv-002", (
        NormalisedFinding(advisory_id="RHSA-2026:1", title="Kernel update",
                          severity="critical"),
    ))
    return create_snapshot(
        scope="daily", period_start=PERIOD_START, period_end=PERIOD_START,
        generated_by="test", now=SNAPSHOT_NOW,
    )


def test_month_view_is_built_from_the_snapshot(engine, clean_tables):
    snapshot = _september_snapshot(engine)

    result = summary(VIEWER, period="2026-09")

    assert result["mode"] == "period"
    assert result["period"]["key"] == "2026-09"
    assert result["period"]["label"] == "September 2026"
    assert result["period"]["snapshot_id"] == snapshot["snapshot_id"]
    assert result["period"]["samples"] == 1
    assert result["period"]["start"] == "2026-09-01"
    assert result["period"]["end"] == "2026-09-30"
    # The snapshot's own counts, not a fresh evaluation: these are recorded
    # values and must match exactly.
    assert result["kpis"]["compliant"] == 1
    assert result["kpis"]["non_compliant"] == 1
    assert result["kpis"]["unknown"] == 1
    assert result["kpis"]["in_scope"] == 3
    assert result["kpis"]["compliance_pct"] == pytest.approx(33.33, abs=0.01)
    assert result["freshness"] is None, (
        "today's collection age is not evidence about a past period"
    )


def test_snapshot_records_the_os_family_panel(engine, clean_tables):
    _september_snapshot(engine)

    result = summary(VIEWER, period="2026-09")

    os_counts = {row["group"]: row["total_servers"] for row in result["by_os"]}
    assert os_counts == {"rhel": 2, "windows": 1}
    assert "os_family" in result["dimensions"]
    assert {row["group"]: row["total_servers"]
            for row in result["by_environment"]} == {
        "Production": 2, "Staging": 1,
    }


def test_quarter_and_year_views_cover_the_same_snapshot(engine, clean_tables):
    snapshot = _september_snapshot(engine)

    for key, label in (("2026-Q3", "Q3 2026"), ("2026", "2026")):
        result = summary(VIEWER, period=key)
        assert result["period"]["snapshot_id"] == snapshot["snapshot_id"]
        assert result["period"]["label"] == label
        assert result["kpis"]["in_scope"] == 3


def test_periods_are_listed_newest_first_with_sample_counts(engine, clean_tables):
    _september_snapshot(engine)

    result = snapshot_periods(VIEWER)

    by_kind = {}
    for period in result["periods"]:
        by_kind.setdefault(period["kind"], []).append(period)
    assert [p["key"] for p in by_kind["month"]] == ["2026-09"]
    assert [p["key"] for p in by_kind["quarter"]] == ["2026-Q3"]
    assert [p["key"] for p in by_kind["year"]] == ["2026"]
    assert all(p["samples"] == 1 for p in result["periods"])
    assert by_kind["month"][0]["label"] == "September 2026"


def test_empty_period_is_404_not_an_empty_dashboard(engine, clean_tables):
    _september_snapshot(engine)

    with pytest.raises(HTTPException) as caught:
        summary(VIEWER, period="2026-10")
    assert caught.value.status_code == 404
    assert "October 2026" in caught.value.detail


@pytest.mark.parametrize("key", ["2026-13", "2026-Q5", "last month", ""])
def test_invalid_period_is_400(engine, clean_tables, key):
    _september_snapshot(engine)

    with pytest.raises(HTTPException) as caught:
        summary(VIEWER, period=key)
    assert caught.value.status_code == 400


def test_period_cannot_be_combined_with_dimension_filters(engine, clean_tables):
    _september_snapshot(engine)

    with pytest.raises(HTTPException) as caught:
        summary(VIEWER, period="2026-09", environment="Production")
    assert caught.value.status_code == 400
    assert "cannot be combined" in caught.value.detail


def test_older_snapshot_without_os_family_returns_an_empty_panel(
    engine, clean_tables
):
    # Snapshots captured before os_family was recorded must not have the panel
    # filled in from today's inventory. An empty list plus the dimensions list
    # lets the dashboard say "not recorded" instead of inventing a figure.
    snapshot = _september_snapshot(engine)
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM compliance.snapshot_groups "
                 "WHERE snapshot_id = :id AND dimension = 'os_family'"),
            {"id": snapshot["snapshot_id"]},
        )

    result = summary(VIEWER, period="2026-09")

    assert result["by_os"] == []
    assert "os_family" not in result["dimensions"]
    assert result["kpis"]["in_scope"] == 3, (
        "the headline is still the recorded snapshot, not a live recalculation"
    )


def test_period_view_does_not_touch_the_live_state(engine, clean_tables):
    # Reading history must not re-evaluate or persist anything: a past report
    # read twice must be identical, and reading it must not rewrite today's
    # compliance. This is checked by comparing the stored snapshot row before
    # and after a read.
    snapshot = _september_snapshot(engine)
    with engine.connect() as conn:
        before = conn.execute(
            text("SELECT compliance_pct, total_servers FROM compliance.snapshots "
                 "WHERE id = :id"), {"id": UUID(snapshot["snapshot_id"])},
        ).mappings().first()

    summary(VIEWER, period="2026-Q3")
    summary(VIEWER, period="2026")

    with engine.connect() as conn:
        after = conn.execute(
            text("SELECT compliance_pct, total_servers FROM compliance.snapshots "
                 "WHERE id = :id"), {"id": UUID(snapshot["snapshot_id"])},
        ).mappings().first()
    assert dict(after) == dict(before)
