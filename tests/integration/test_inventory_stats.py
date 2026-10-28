"""Aggregate tiles on the inventory register.

The register lists one page at a time; the tiles above it summarise the whole
filtered register, so their counts must come from the database rather than the
rows on screen. The Status filter is excluded from the summary on purpose:
selecting "Non-compliant" must not empty the Compliant tile.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text

from inventory.app.connectors.base import AssetRecord
from inventory.app.main import list_assets
from inventory.app.sync import upsert_asset
from patchtrack_common.security import Principal

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
OPERATOR = Principal(
    user_id="00000000-0000-0000-0000-000000000001",
    username="operator",
    role="operations",
    display_name="Operations",
)


def _record(source_id: str, **overrides) -> AssetRecord:
    base = {
        "source_id": source_id,
        "hostname": source_id.lower(),
        "fqdn": f"{source_id.lower()}.example.com",
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


def _create(engine, source_id: str, **overrides) -> str:
    with engine.begin() as conn:
        run_id = str(conn.execute(
            text("INSERT INTO inventory.sync_runs (integration_key, trigger) "
                 "VALUES ('assetexplorer', 'manual') RETURNING id")
        ).first()[0])
        asset_id, _ = upsert_asset(conn, _record(source_id, **overrides), run_id)
    return asset_id


def _set_state(engine, asset_id: str, state: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO compliance.asset_compliance (asset_id, state, evaluated_at) "
                 "VALUES (:id, :state, :at)"),
            {"id": asset_id, "state": state, "at": NOW},
        )


def test_stats_summarise_the_whole_register(engine, clean_tables):
    states = {
        "AE-C": "compliant",
        "AE-N": "non_compliant",
        "AE-U": "unknown",
        "AE-S": "stale",
        "AE-X": "excluded",
    }
    for source_id, state in states.items():
        _set_state(engine, _create(engine, source_id), state)
    _create(engine, "AE-NEW")   # no compliance row yet
    _create(engine, "AE-TEST", environment="Test")

    stats = list_assets(principal=OPERATOR, page=1, page_size=2)["stats"]

    assert stats["total"] == 7
    assert stats["excluded"] == 1
    assert stats["compliant"] == 1
    assert stats["non_compliant"] == 1
    assert stats["unknown"] == 1
    assert stats["stale"] == 1
    assert stats["not_assessed"] == 2   # AE-NEW and AE-TEST


def test_stats_follow_dimension_filters(engine, clean_tables):
    _set_state(engine, _create(engine, "AE-P1"), "compliant")
    _set_state(engine, _create(engine, "AE-P2"), "non_compliant")
    _create(engine, "AE-T1", environment="Test")

    stats = list_assets(principal=OPERATOR, page=1, page_size=25, environment="Test")["stats"]
    assert stats["total"] == 1
    assert stats["not_assessed"] == 1


def test_status_filter_does_not_narrow_the_summary(engine, clean_tables):
    _set_state(engine, _create(engine, "AE-C"), "compliant")
    _set_state(engine, _create(engine, "AE-N"), "non_compliant")

    response = list_assets(principal=OPERATOR, page=1, page_size=25, compliance_state="non_compliant")

    # The list is filtered...
    assert response["total"] == 1
    assert [row["hostname"] for row in response["items"]] == ["ae-n"]
    # ...but the tiles still report the whole register.
    assert response["stats"]["compliant"] == 1
    assert response["stats"]["non_compliant"] == 1
