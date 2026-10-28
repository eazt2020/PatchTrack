"""Dashboard breakdowns read the effective placement and the OS family.

`/summary` is what the three composition panels draw. Two requirements are
pinned here because both were wrong in the delivered dashboard: the operating
system panel grouped by version (9.4, 2022…) instead of platform family, and a
placement correction was only checked at the view level, never through the
aggregation the panel actually consumes.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import text

from compliance.app.main import summary
from inventory.app.connectors.base import AssetRecord
from inventory.app.main import (
    PlacementOptionCreate,
    PlacementOptionRename,
    PlacementOverride,
    create_placement_option,
    rename_placement_option,
    set_asset_override,
)
from inventory.app.sync import upsert_asset
from patchtrack_common.security import Principal

VIEWER = Principal(
    user_id="00000000-0000-0000-0000-000000000003",
    username="viewer",
    role="viewer",
    display_name="Viewer",
)


ADMIN = Principal(
    user_id="00000000-0000-0000-0000-000000000002",
    username="admin",
    role="administrator",
    display_name="Administrator",
)


class _Request:
    client = SimpleNamespace(host="127.0.0.1")
    headers: dict[str, str] = {}


def _record(**overrides) -> AssetRecord:
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


def _create_asset(engine) -> str:
    with engine.begin() as conn:
        run_id = str(conn.execute(
            text("INSERT INTO inventory.sync_runs (integration_key, trigger) "
                 "VALUES ('assetexplorer', 'manual') RETURNING id")
        ).first()[0])
        asset_id, _ = upsert_asset(conn, _record(), run_id)
    return asset_id


def _groups(rows: list[dict], key: str = "group") -> dict[str, int]:
    return {row[key]: row["total_servers"] for row in rows}


def test_the_os_panel_groups_by_family_not_version(engine, clean_tables):
    _create_asset(engine)

    result = summary(VIEWER)

    assert result["by_os"] == [
        {**result["by_os"][0], "group": "rhel", "total_servers": 1}
    ], "the OS panel must group RHEL assets under the family, not '9.4'"
    assert all(row["group"] in {"rhel", "windows", "solaris", "other"}
               for row in result["by_os"])


def test_a_placement_override_moves_the_asset_between_panel_groups(
    engine, clean_tables
):
    asset_id = _create_asset(engine)
    set_asset_override(
        UUID(asset_id),
        PlacementOverride(
            environment="Staging",
            business_group="Others",
            reason="Moved after the October reorganisation",
        ),
        _Request(),
        Principal(
            user_id="00000000-0000-0000-0000-000000000001",
            username="operator",
            role="operations",
        ),
    )

    result = summary(VIEWER)

    # One asset, so each panel must show exactly the effective group. The
    # source values must not linger in the breakdown.
    assert _groups(result["by_environment"]) == {"Staging": 1}
    assert _groups(result["by_business_group"]) == {"Others": 1}
    assert _groups(result["by_application_group"]) == {"Core": 1}


def test_a_settings_rename_reaches_the_dashboard_panels(engine, clean_tables):
    # The complaint this pins: renaming a value in Settings left the dashboard
    # and the inventory showing the old name. The rename must be visible in the
    # exact aggregation the panels consume, without rewriting the asset.
    _create_asset(engine)
    environment = create_placement_option(
        PlacementOptionCreate(kind="environment", value="Production"),
        _Request(),
        ADMIN,
    )
    business_group = create_placement_option(
        PlacementOptionCreate(kind="business_group", value="Payments"),
        _Request(),
        ADMIN,
    )

    rename_placement_option(
        environment["id"], PlacementOptionRename(value="Prod"), _Request(), ADMIN,
    )
    rename_placement_option(
        business_group["id"], PlacementOptionRename(value="Billing"),
        _Request(), ADMIN,
    )

    result = summary(VIEWER)

    assert _groups(result["by_environment"]) == {"Prod": 1}
    assert _groups(result["by_business_group"]) == {"Billing": 1}


def test_every_group_is_returned_not_only_the_first_several(engine, clean_tables):
    # The business-service panel used to slice the API response to seven rows,
    # which silently hid the eighth and ninth groups. The API must return every
    # group; the panel is responsible for presenting all of them.
    for index in range(9):
        with engine.begin() as conn:
            run_id = str(conn.execute(
                text("INSERT INTO inventory.sync_runs (integration_key, trigger) "
                     "VALUES ('assetexplorer', 'manual') RETURNING id")
            ).first()[0])
            upsert_asset(
                conn,
                _record(source_id=f"AE-{index}", hostname=f"srv-{index:03d}",
                        business_group=f"Service {index:02d}"),
                run_id,
            )

    result = summary(VIEWER)

    assert len(result["by_business_group"]) == 9
    assert {row["group"] for row in result["by_business_group"]} == {
        f"Service {index:02d}" for index in range(9)
    }
