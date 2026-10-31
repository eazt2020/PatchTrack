"""The management summary needs deadline and ageing numbers, not a count.

NIST SP 800-40r4 measures remediation against the maintenance plan's deadline
and cuts it by severity. The exported management sheet draws its SLA table, its
overdue-age buckets and its unit heatmap from `/summary`'s `management` block,
so these tests pin the aggregation the PDF consumes rather than the PDF itself:
severity rows, overdue buckets, unit ranking and the environment filter.

The tests compute dates relative to today because the block compares against
the current date; fixed dates would pass today and fail in a year.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from compliance.app.main import summary
from inventory.app.connectors.base import AssetRecord
from inventory.app.sync import upsert_asset
from patchtrack_common.security import Principal

VIEWER = Principal(
    user_id="00000000-0000-0000-0000-000000000003",
    username="viewer",
    role="viewer",
    display_name="Viewer",
)


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


def _create_asset(engine, source_id: str, environment: str,
                  os_name: str = "Red Hat Enterprise Linux") -> str:
    with engine.begin() as conn:
        run_id = str(conn.execute(
            text("INSERT INTO inventory.sync_runs (integration_key, trigger) "
                 "VALUES ('assetexplorer', 'manual') RETURNING id")
        ).first()[0])
        asset_id, _ = upsert_asset(
            conn,
            _record(source_id=source_id, hostname=source_id.lower(),
                    environment=environment, os_name=os_name),
            run_id,
        )
    return asset_id


def _add_findings(
    engine, asset_id: str, findings: list[tuple[str, str | None, datetime | None]],
) -> None:
    """Insert the current findings for one asset in a fresh assessment."""
    with engine.begin() as conn:
        assessment_id = str(conn.execute(
            text(
                "INSERT INTO compliance.assessments "
                "(asset_id, source_system, assessed_at, status, total_patches) "
                "VALUES (:a, 'satellite', :t, 'complete', :n) RETURNING id"
            ),
            {"a": asset_id, "t": datetime.now(UTC), "n": len(findings)},
        ).first()[0])
        for advisory, severity, due in findings:
            conn.execute(
                text(
                    "INSERT INTO compliance.findings "
                    "(asset_id, assessment_id, source_system, advisory_id, title, "
                    " severity, remediation_due, released_at) "
                    "VALUES (:a, :s, 'satellite', :adv, :adv, :sev, :due, :rel)"
                ),
                {
                    "a": asset_id,
                    "s": assessment_id,
                    "adv": advisory,
                    "sev": severity,
                    "due": due,
                    "rel": datetime.now(UTC) - timedelta(days=20),
                },
            )
        conn.execute(
            text(
                "INSERT INTO compliance.asset_latest_assessment "
                "(asset_id, assessment_id) VALUES (:a, :s) "
                "ON CONFLICT (asset_id) DO UPDATE SET assessment_id = EXCLUDED.assessment_id"
            ),
            {"a": asset_id, "s": assessment_id},
        )


def _today():
    return datetime.now(UTC).date()


def test_the_block_counts_deadlines_and_buckets_the_overdue_findings(
    engine, clean_tables,
):
    today = _today()
    asset_id = _create_asset(engine, "AE-1", "Production")
    _add_findings(engine, asset_id, [
        ("RHSA-1", "critical", today - timedelta(days=40)),
        ("RHSA-2", "critical", today + timedelta(days=3)),
        ("RHSA-3", "moderate", today - timedelta(days=2)),
        ("RHSA-4", "moderate", today - timedelta(days=100)),
        ("RHSA-5", "low", None),
        ("RHSA-6", "low", today + timedelta(days=30)),
    ])

    management = summary(VIEWER)["management"]

    assert management["as_of"] == today.isoformat()
    assert management["due_soon_days"] == 7
    assert management["total_open"] == 6
    assert management["due_soon"] == 1
    assert management["overdue"] == 3
    assert management["undated"] == 1

    # Severities are ordered for the table, not alphabetically.
    assert [row["severity"] for row in management["by_severity"]] == [
        "critical", "moderate", "low",
    ]
    critical = management["by_severity"][0]
    assert (critical["open"], critical["overdue"],
            critical["due_soon"], critical["oldest_days"]) == (2, 1, 1, 40)
    assert critical["median_age_days"] == 20.0
    assert (critical["days_1_7"], critical["days_8_30"],
            critical["days_31_90"], critical["days_90_plus"]) == (0, 0, 1, 0)

    moderate = management["by_severity"][1]
    assert (moderate["open"], moderate["overdue"],
            moderate["oldest_days"]) == (2, 2, 100)
    assert (moderate["days_1_7"], moderate["days_8_30"],
            moderate["days_31_90"], moderate["days_90_plus"]) == (1, 0, 0, 1)

    assert management["overdue_age"] == {
        "days_1_7": 1, "days_8_30": 0, "days_31_90": 1, "days_90_plus": 1,
    }

    # Patch-level transparency: one row per outstanding advisory, aggregated
    # across hosts, carrying the late count and the oldest overdue age. The
    # list is faceted by platform, so a single-OS estate contributes its two
    # widest/most overdue advisories rather than an arbitrary global top-N.
    advisories = {row["advisory_id"]: row for row in management["advisories"]}
    assert len(advisories) == 2
    assert {row["os_family"] for row in advisories.values()} == {"rhel"}
    assert advisories["RHSA-1"]["severity"] == "critical"
    assert (advisories["RHSA-1"]["open"], advisories["RHSA-1"]["overdue"],
            advisories["RHSA-1"]["oldest_days"],
            advisories["RHSA-1"]["affected_servers"]) == (1, 1, 40, 1)
    assert advisories["RHSA-4"]["oldest_days"] == 100


def test_the_advisory_table_represents_each_operating_system(
    engine, clean_tables,
):
    today = _today()
    rhel = _create_asset(engine, "AE-1", "Production")
    windows = _create_asset(engine, "AE-2", "Production",
                            os_name="Windows Server 2022")
    solaris = _create_asset(engine, "AE-3", "Production",
                            os_name="Solaris 11.4")
    _add_findings(engine, rhel, [
        ("RHSA-1", "critical", today - timedelta(days=3)),
        ("RHSA-2", "critical", today - timedelta(days=4)),
        ("RHSA-3", "critical", today - timedelta(days=5)),
    ])
    _add_findings(engine, windows, [
        ("KB5007552", "critical", today - timedelta(days=2)),
    ])
    _add_findings(engine, solaris, [
        ("353092-01", "critical", today - timedelta(days=1)),
    ])

    advisories = summary(VIEWER)["management"]["advisories"]

    # Three RHEL advisories compete for two slots; every platform still gets
    # its own rows instead of the RHEL backlog crowding the others out.
    assert {row["os_family"] for row in advisories} == {
        "rhel", "windows", "solaris",
    }
    assert sum(1 for row in advisories if row["os_family"] == "rhel") == 2
    assert [row["advisory_id"] for row in advisories
            if row["os_family"] == "windows"] == ["KB5007552"]
    assert [row["advisory_id"] for row in advisories
            if row["os_family"] == "solaris"] == ["353092-01"]


def test_the_unit_table_ranks_environments_by_overdue_findings(
    engine, clean_tables,
):
    today = _today()
    production = _create_asset(engine, "AE-1", "Production")
    staging = _create_asset(engine, "AE-2", "Staging")
    _add_findings(engine, production, [
        ("RHSA-1", "critical", today - timedelta(days=3)),
        ("RHSA-2", "moderate", today + timedelta(days=5)),
    ])
    _add_findings(engine, staging, [
        ("RHSA-3", "critical", today - timedelta(days=90)),
        ("RHSA-4", "critical", today - timedelta(days=10)),
    ])

    units = summary(VIEWER)["management"]["units"]

    assert [unit["group_key"] for unit in units] == ["Staging", "Production"]
    assert units[0] == {
        "group_key": "Staging", "affected_servers": 1, "open": 2,
        "overdue": 2, "oldest_days": 90,
    }
    assert units[1]["overdue"] == 1
    assert units[1]["oldest_days"] == 3


def test_the_environment_filter_bounds_the_management_block(
    engine, clean_tables,
):
    today = _today()
    production = _create_asset(engine, "AE-1", "Production")
    staging = _create_asset(engine, "AE-2", "Staging")
    _add_findings(engine, production,
                  [("RHSA-1", "critical", today - timedelta(days=3))])
    _add_findings(engine, staging,
                  [("RHSA-2", "critical", today - timedelta(days=3))])

    management = summary(VIEWER, environment="Staging")["management"]

    assert management["total_open"] == 1
    assert management["overdue"] == 1
    assert [unit["group_key"] for unit in management["units"]] == ["Staging"]
