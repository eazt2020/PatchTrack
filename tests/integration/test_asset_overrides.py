"""Local environment / business-service overrides against a real database.

The rule under test: AssetExplorer remains the authority for the values it
supplies. An operator override is stored beside the source value, survives the
next collection, and is what every reader (inventory, compliance and reports)
sees. Clearing it returns the source value without another sync.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import text

from inventory.app.connectors.base import AssetRecord
from inventory.app.main import PlacementOverride, clear_asset_override, set_asset_override
from inventory.app.sync import upsert_asset
from patchtrack_common.security import Principal

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
OPERATOR = Principal(
    user_id="00000000-0000-0000-0000-000000000001",
    username="operator",
    role="operations",
    display_name="Operations",
)


class _Request:
    """Only `request.client` is read, via patchtrack_common.net.client_ip."""

    client = SimpleNamespace(host="127.0.0.1")
    headers: dict[str, str] = {}


def asset_record(**overrides) -> AssetRecord:
    base = {
        "source_id": "AE-1",
        "hostname": "srv-001",
        "fqdn": "srv-001.example.com",
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


def create_asset(engine) -> str:
    with engine.begin() as conn:
        run_id = str(conn.execute(
            text("INSERT INTO inventory.sync_runs (integration_key, trigger) "
                 "VALUES ('assetexplorer', 'manual') RETURNING id")
        ).first()[0])
        asset_id, _ = upsert_asset(conn, asset_record(), run_id)
    return asset_id


def read_effective(engine, asset_id: str) -> dict:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT environment, business_group, environment_source, "
                "business_group_source, environment_overridden, "
                "business_group_overridden FROM inventory.asset_effective "
                "WHERE id = :i"
            ),
            {"i": asset_id},
        ).mappings().one()


def test_override_survives_the_next_asset_sync(engine, clean_tables):
    asset_id = create_asset(engine)
    set_asset_override(
        UUID(asset_id),
        PlacementOverride(
            environment="Staging",
            business_group="Payments Platform",
            reason="Server was reclassified after the October migration",
        ),
        _Request(),
        OPERATOR,
    )

    # The next collection reports different source values; it must update its
    # own columns and leave the override in place.
    with engine.begin() as conn:
        run_id = str(conn.execute(
            text("INSERT INTO inventory.sync_runs (integration_key, trigger) "
                 "VALUES ('assetexplorer', 'manual') RETURNING id")
        ).first()[0])
        upsert_asset(
            conn,
            asset_record(environment="QA", business_group="Payments Legacy"),
            run_id,
        )

    row = read_effective(engine, asset_id)
    assert row["environment"] == "Staging", "the override must survive a sync"
    assert row["environment_source"] == "QA", "the source value is still updated"
    assert row["business_group"] == "Payments Platform"
    assert row["business_group_source"] == "Payments Legacy"
    assert row["environment_overridden"] is True
    assert row["business_group_overridden"] is True


def test_clearing_the_override_returns_the_source_value(engine, clean_tables):
    asset_id = create_asset(engine)
    set_asset_override(
        UUID(asset_id),
        PlacementOverride(environment="Staging", business_group=None, reason="Wrong in source"),
        _Request(),
        OPERATOR,
    )
    result = clear_asset_override(UUID(asset_id), _Request(), OPERATOR)

    row = read_effective(engine, asset_id)
    assert row["environment"] == "Production"
    assert row["business_group"] == "Payments"
    assert row["environment_overridden"] is False
    assert row["business_group_overridden"] is False
    assert result["asset"]["environment"] == "Production"


def test_a_blank_put_clears_the_override(engine, clean_tables):
    asset_id = create_asset(engine)
    set_asset_override(
        UUID(asset_id), PlacementOverride(environment="Staging", reason="Source is wrong"),
        _Request(), OPERATOR,
    )
    set_asset_override(
        UUID(asset_id), PlacementOverride(reason="Back to the source value"),
        _Request(), OPERATOR,
    )
    row = read_effective(engine, asset_id)
    assert row["environment"] == "Production"
    assert row["environment_overridden"] is False


def test_the_override_is_attributed_and_audited(engine, clean_tables):
    asset_id = create_asset(engine)
    set_asset_override(
        UUID(asset_id),
        PlacementOverride(environment="Staging", reason="Reclassified"),
        _Request(),
        OPERATOR,
    )
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT set_by FROM inventory.asset_overrides WHERE asset_id = :i"),
            {"i": asset_id},
        ).mappings().one()
        audit = conn.execute(
            text(
                "SELECT actor, actor_role, event_type, context "
                "FROM shared.audit_events "
                "WHERE event_type = 'inventory.placement_overridden' "
                "AND target_id = :i"
            ),
            {"i": asset_id},
        ).mappings().all()
    assert row["set_by"] == "operator"
    assert len(audit) == 1
    assert audit[0]["actor"] == "operator"
    assert audit[0]["actor_role"] == "operations"
    assert audit[0]["context"]["environment"] == {
        "from": "Production", "to": "Staging",
    }
    assert audit[0]["context"]["reason"] == "Reclassified"


def test_an_override_does_not_change_asset_identity(engine, clean_tables):
    asset_id = create_asset(engine)
    with engine.connect() as conn:
        before = conn.execute(
            text("SELECT assetexplorer_id, hostname, ip_address::text AS ip "
                 "FROM inventory.assets WHERE id = :i"), {"i": asset_id},
        ).mappings().one()
        reconciliation = conn.execute(
            text("SELECT reconciliation_state FROM inventory.assets WHERE id = :i"),
            {"i": asset_id},
        ).scalar()
    set_asset_override(
        UUID(asset_id), PlacementOverride(business_group="Payments Platform",
                                          reason="Regrouped by the service owner"),
        _Request(), OPERATOR,
    )
    with engine.connect() as conn:
        after = conn.execute(
            text("SELECT assetexplorer_id, hostname, ip_address::text AS ip, "
                 "reconciliation_state FROM inventory.assets WHERE id = :i"),
            {"i": asset_id},
        ).mappings().one()
    assert after["assetexplorer_id"] == before["assetexplorer_id"]
    assert after["hostname"] == before["hostname"]
    assert after["ip"] == before["ip"]
    assert after["reconciliation_state"] == reconciliation
