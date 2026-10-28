"""Administrator-managed placement vocabulary against a real database.

The rules under test:

- the Settings list is the vocabulary every screen displays;
- a rename propagates to every server that holds the old spelling, while the
  value AssetExplorer reported stays visible as ``*_source``;
- a rename survives the next sync, because the source record is never rewritten
  (the old spelling is resolved on read);
- a value that is still in use cannot be deleted, so the inventory can never
  show a placement the list no longer recognises;
- the AssetExplorer sync never reads the list.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from inventory.app.connectors.base import AssetRecord
from inventory.app.main import (
    PlacementOptionCreate,
    PlacementOptionRename,
    PlacementOverride,
    create_placement_option,
    delete_placement_option,
    list_placement_options,
    rename_placement_option,
    set_asset_override,
)
from inventory.app.sync import upsert_asset
from patchtrack_common.security import Principal

ADMIN = Principal(
    user_id="00000000-0000-0000-0000-000000000002",
    username="admin",
    role="administrator",
    display_name="Administrator",
)
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


def values(engine, kind: str) -> list[str]:
    with engine.connect() as conn:
        return [
            row[0]
            for row in conn.execute(
                text("SELECT value FROM inventory.placement_options "
                     "WHERE kind = :k ORDER BY lower(value)"),
                {"k": kind},
            )
        ]


def test_an_option_is_added_to_the_picker(engine, clean_tables):
    option = create_placement_option(
        PlacementOptionCreate(kind="environment", value="  Staging  "),
        _Request(), ADMIN,
    )
    assert option["value"] == "Staging", "surrounding whitespace is trimmed"
    assert option["in_use"] == 0
    listed = list_placement_options(ADMIN)
    assert [o["value"] for o in listed["environments"]] == ["Staging"]
    assert listed["business_groups"] == []


def test_the_same_value_cannot_be_listed_twice(engine, clean_tables):
    create_placement_option(
        PlacementOptionCreate(kind="business_group", value="Payments"),
        _Request(), ADMIN,
    )
    with pytest.raises(HTTPException) as caught:
        create_placement_option(
            PlacementOptionCreate(kind="business_group", value=" payments "),
            _Request(), ADMIN,
        )
    assert caught.value.status_code == 409
    assert values(engine, "business_group") == ["Payments"]


def test_a_whitespace_only_value_is_rejected(engine, clean_tables):
    with pytest.raises(HTTPException) as caught:
        create_placement_option(
            PlacementOptionCreate(kind="environment", value="   "),
            _Request(), ADMIN,
        )
    assert caught.value.status_code == 422
    assert values(engine, "environment") == []


def effective(engine, asset_id: str) -> dict:
    with engine.connect() as conn:
        return dict(conn.execute(
            text("SELECT environment, environment_source, business_group, "
                 "business_group_source, environment_overridden "
                 "FROM inventory.asset_effective WHERE id = :i"),
            {"i": asset_id},
        ).mappings().one())


def test_renaming_an_option_propagates_to_the_servers(engine, clean_tables):
    asset_id = create_asset(engine)
    option = create_placement_option(
        PlacementOptionCreate(kind="environment", value="Production"),
        _Request(), ADMIN,
    )

    renamed = rename_placement_option(
        option["id"], PlacementOptionRename(value="Prod"), _Request(), ADMIN,
    )

    assert renamed["value"] == "Prod"
    assert renamed["in_use"] == 1, "the server now counts against the new name"
    row = effective(engine, asset_id)
    assert row["environment"] == "Prod", "the inventory shows the new name"
    assert row["environment_source"] == "Production", (
        "the value AssetExplorer reported is preserved for audit"
    )
    with engine.connect() as conn:
        audit = conn.execute(
            text("SELECT context FROM shared.audit_events "
                 "WHERE event_type = 'settings.placement_option_renamed' "
                 "AND target_id = :i"),
            {"i": str(option["id"])},
        ).scalar()
    assert audit["from"] == "Production"
    assert audit["to"] == "Prod"
    assert audit["propagated"] is True


def test_a_rename_survives_the_next_sync(engine, clean_tables):
    asset_id = create_asset(engine)
    option = create_placement_option(
        PlacementOptionCreate(kind="environment", value="Production"),
        _Request(), ADMIN,
    )
    rename_placement_option(
        option["id"], PlacementOptionRename(value="Prod"), _Request(), ADMIN,
    )

    # AssetExplorer keeps reporting the old spelling, as it will until someone
    # corrects it at the source. The sync must not undo the rename.
    with engine.begin() as conn:
        run_id = str(conn.execute(
            text("INSERT INTO inventory.sync_runs (integration_key, trigger) "
                 "VALUES ('assetexplorer', 'manual') RETURNING id")
        ).first()[0])
        upsert_asset(conn, asset_record(), run_id)

    row = effective(engine, asset_id)
    assert row["environment"] == "Prod"
    assert row["environment_source"] == "Production"


def test_a_rename_also_moves_an_override_saved_under_the_old_name(
    engine, clean_tables,
):
    asset_id = create_asset(engine)
    set_asset_override(
        UUID(asset_id),
        PlacementOverride(environment="Staging", reason="Reclassified"),
        _Request(), OPERATOR,
    )
    option = create_placement_option(
        PlacementOptionCreate(kind="environment", value="Staging"),
        _Request(), ADMIN,
    )

    rename_placement_option(
        option["id"], PlacementOptionRename(value="Stage"), _Request(), ADMIN,
    )

    row = effective(engine, asset_id)
    assert row["environment"] == "Stage", "the override resolves to the new name"
    assert row["environment_overridden"] is True
    with engine.connect() as conn:
        stored = conn.execute(
            text("SELECT environment FROM inventory.asset_overrides "
                 "WHERE asset_id = :i"),
            {"i": asset_id},
        ).scalar()
    assert stored == "Staging", "the operator's decision is recorded verbatim"


def test_a_former_name_cannot_be_added_again(engine, clean_tables):
    option = create_placement_option(
        PlacementOptionCreate(kind="environment", value="Production"),
        _Request(), ADMIN,
    )
    rename_placement_option(
        option["id"], PlacementOptionRename(value="Prod"), _Request(), ADMIN,
    )

    with pytest.raises(HTTPException) as caught:
        create_placement_option(
            PlacementOptionCreate(kind="environment", value="production"),
            _Request(), ADMIN,
        )

    assert caught.value.status_code == 409
    assert "Prod" in caught.value.detail
    assert values(engine, "environment") == ["Prod"]


def test_renaming_onto_another_entrys_old_name_is_refused(engine, clean_tables):
    first = create_placement_option(
        PlacementOptionCreate(kind="environment", value="Production"),
        _Request(), ADMIN,
    )
    second = create_placement_option(
        PlacementOptionCreate(kind="environment", value="Staging"),
        _Request(), ADMIN,
    )
    rename_placement_option(
        first["id"], PlacementOptionRename(value="Prod"), _Request(), ADMIN,
    )

    with pytest.raises(HTTPException) as caught:
        rename_placement_option(
            second["id"], PlacementOptionRename(value="Production"),
            _Request(), ADMIN,
        )

    assert caught.value.status_code == 409
    assert values(engine, "environment") == ["Prod", "Staging"]


def test_deleting_an_option_in_use_is_refused(engine, clean_tables):
    create_asset(engine)
    option = create_placement_option(
        PlacementOptionCreate(kind="environment", value="Production"),
        _Request(), ADMIN,
    )

    with pytest.raises(HTTPException) as caught:
        delete_placement_option(option["id"], _Request(), ADMIN)

    assert caught.value.status_code == 409
    assert "rename" in caught.value.detail.lower()
    assert values(engine, "environment") == ["Production"]


def test_deleting_an_unused_option_removes_it(engine, clean_tables):
    option = create_placement_option(
        PlacementOptionCreate(kind="environment", value="Staging"),
        _Request(), ADMIN,
    )

    result = delete_placement_option(option["id"], _Request(), ADMIN)

    assert result["deleted"] is True
    assert result["in_use"] == 0
    assert values(engine, "environment") == []
    with engine.connect() as conn:
        audit = conn.execute(
            text("SELECT context FROM shared.audit_events "
                 "WHERE event_type = 'settings.placement_option_removed' "
                 "AND target_id = :i"),
            {"i": str(option["id"])},
        ).scalar()
    assert audit["value"] == "Staging"
