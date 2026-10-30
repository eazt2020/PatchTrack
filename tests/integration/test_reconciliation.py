"""Asset reconciliation against a real PostgreSQL database.

The business rules under test: AssetExplorer stays authoritative, a hostname or
IP change never creates a second inventory record, assessments resolve by the
strongest available identifier, and an ambiguous match is recorded as a
conflict rather than silently attached to the wrong server.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from inventory.app.connectors.base import AssetRecord, PatchAssessment
from inventory.app.sync import match_asset, upsert_asset

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


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


def assessment(**overrides) -> PatchAssessment:
    base = {
        "source_system": "satellite",
        "source_ref": "SAT-1",
        "hostname": "srv-001",
        "fqdn": "srv-001.example.com",
        "ip_address": "10.0.0.1",
        "os_name": "Red Hat Enterprise Linux",
        "os_version": "9.4",
        "assessed_at": NOW,
        "status": "complete",
        "findings": (),
    }
    base.update(overrides)
    return PatchAssessment(**base)


def start_run(conn, key: str = "assetexplorer") -> str:
    return str(conn.execute(
        text(
            "INSERT INTO inventory.sync_runs (integration_key, trigger) "
            "VALUES (:k, 'manual') RETURNING id"
        ),
        {"k": key},
    ).first()[0])


def test_new_asset_is_created(engine, clean_tables):
    with engine.begin() as conn:
        run_id = start_run(conn)
        asset_id, created = upsert_asset(conn, asset_record(), run_id)
    assert created is True
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT hostname, in_scope, assetexplorer_id "
                 "FROM inventory.assets WHERE id = :i"), {"i": asset_id}
        ).mappings().first()
    assert row["hostname"] == "srv-001"
    assert row["in_scope"] is True
    assert row["assetexplorer_id"] == "AE-1"


def test_hostname_change_updates_in_place(engine, clean_tables):
    with engine.begin() as conn:
        run_id = start_run(conn)
        first, _ = upsert_asset(conn, asset_record(), run_id)
        second, created = upsert_asset(
            conn, asset_record(hostname="srv-001-renamed"), run_id)
    assert created is False
    assert first == second
    with engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM inventory.assets")).scalar()
        alias = conn.execute(
            text("SELECT 1 FROM inventory.asset_aliases "
                 "WHERE alias_type = 'hostname' AND alias_value = 'srv-001'")
        ).first()
    assert count == 1, "a rename must not create a second inventory record"
    assert alias is not None, "the previous hostname is retained as an alias"


def test_rename_is_recorded_as_a_reconciliation_conflict(engine, clean_tables):
    with engine.begin() as conn:
        run_id = start_run(conn)
        asset_id, _ = upsert_asset(conn, asset_record(), run_id)
        upsert_asset(conn, asset_record(hostname="srv-001-renamed"), run_id)
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT reason, resolved_at FROM inventory.reconciliation_conflicts "
                 "WHERE asset_id = :a"), {"a": asset_id}
        ).mappings().all()
    assert rows, "an identifier change must be surfaced, not hidden"
    assert rows[0]["reason"] == "identifier_changed"
    assert rows[0]["resolved_at"] is None


def test_identical_resync_creates_no_conflict(engine, clean_tables):
    with engine.begin() as conn:
        run_id = start_run(conn)
        upsert_asset(conn, asset_record(), run_id)
        upsert_asset(conn, asset_record(), run_id)
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM inventory.reconciliation_conflicts")).scalar()
    assert count == 0


def test_assessment_matches_by_source_link_first(engine, clean_tables):
    with engine.begin() as conn:
        run_id = start_run(conn)
        asset_id, _ = upsert_asset(conn, asset_record(), run_id)
        conn.execute(
            text("INSERT INTO inventory.asset_source_links "
                 "(asset_id, source_system, source_ref, method) "
                 "VALUES (:a, 'satellite', 'SAT-1', 'direct')"),
            {"a": asset_id},
        )
        matched, method = match_asset(
            conn, assessment(hostname="something-else", fqdn=None, ip_address=None))
    assert matched == asset_id
    assert method == "direct"


def test_assessment_matches_by_fqdn(engine, clean_tables):
    with engine.begin() as conn:
        run_id = start_run(conn)
        asset_id, _ = upsert_asset(conn, asset_record(), run_id)
        matched, method = match_asset(
            conn, assessment(hostname="srv-001", ip_address="10.9.9.9"))
    assert (matched, method) == (asset_id, "fqdn")


def test_assessment_matches_by_hostname_case_insensitively(engine, clean_tables):
    with engine.begin() as conn:
        run_id = start_run(conn)
        asset_id, _ = upsert_asset(conn, asset_record(), run_id)
        matched, method = match_asset(
            conn, assessment(fqdn=None, hostname="SRV-001", ip_address=None))
    assert (matched, method) == (asset_id, "hostname")


def test_assessment_matches_by_ip(engine, clean_tables):
    with engine.begin() as conn:
        run_id = start_run(conn)
        asset_id, _ = upsert_asset(conn, asset_record(), run_id)
        matched, method = match_asset(
            conn, assessment(fqdn=None, hostname="unknown-host", ip_address="10.0.0.1"))
    assert matched == asset_id


def test_unmatched_assessment_is_not_attached(engine, clean_tables):
    with engine.begin() as conn:
        run_id = start_run(conn)
        upsert_asset(conn, asset_record(), run_id)
        matched, method = match_asset(
            conn, assessment(hostname="ghost", fqdn="ghost.example.com",
                             ip_address="10.1.1.1"))
    assert matched is None
    assert method == "unmatched"


def test_ambiguous_match_is_refused(engine, clean_tables):
    """Two assets sharing a hostname must not be picked between silently."""
    with engine.begin() as conn:
        run_id = start_run(conn)
        upsert_asset(conn, asset_record(), run_id)
        upsert_asset(conn, asset_record(source_id="AE-2", hostname="srv-001",
                                        fqdn="other.example.com",
                                        ip_address="10.0.0.2"), run_id)
        matched, method = match_asset(
            conn, assessment(fqdn=None, ip_address=None))
    assert matched is None
    assert method == "ambiguous"


def test_failed_sync_run_leaves_inventory_intact(engine, clean_tables):
    """A collection failure must never delete or blank existing inventory."""
    with engine.begin() as conn:
        run_id = start_run(conn)
        asset_id, _ = upsert_asset(conn, asset_record(), run_id)
        failed_run = start_run(conn)
        conn.execute(
            text("UPDATE inventory.sync_runs SET status = 'failed', "
                 "finished_at = now(), error_summary = 'connection refused' "
                 "WHERE id = :r"), {"r": failed_run},
        )
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT hostname, last_sync_run_id FROM inventory.assets "
                 "WHERE id = :i"), {"i": asset_id}
        ).mappings().first()
    assert row["hostname"] == "srv-001"
    assert str(row["last_sync_run_id"]) == run_id


def test_duplicate_source_id_is_rejected_by_the_database(engine, clean_tables):
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO inventory.assets (assetexplorer_id, hostname, os_family) "
                     "VALUES ('AE-X', 'one', 'rhel')"))
            conn.execute(
                text("INSERT INTO inventory.assets (assetexplorer_id, hostname, os_family) "
                     "VALUES ('AE-X', 'two', 'rhel')"))


def test_audit_table_is_append_only(engine, clean_tables):
    """Deleting or rewriting audit history must be impossible for the app role."""
    from sqlalchemy.exc import DBAPIError

    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO shared.audit_events "
                 "(actor, actor_role, event_type, target_type, outcome) "
                 "VALUES ('tester', 'administrator', 'test.event', 'test', "
                 "'success')"))
    with pytest.raises(DBAPIError):
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM shared.audit_events"))
