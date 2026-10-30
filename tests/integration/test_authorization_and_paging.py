"""Regressions for permission gates and finding pagination.

These pin server-side behaviour that the UI hides but the API must enforce:

* the asset detail page is readable with ``inventory:detail``, but the
  findings it embeds are gated by ``findings:view``;
* a viewer cannot trigger a compliance evaluation, because that writes state;
* the findings total counts only rows the list can return (the asset's latest
  assessment), so pagination cannot promise rows it never sends.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from compliance.app.main import app as compliance_app
from inventory.app.connectors.base import AssetRecord
from inventory.app.main import app as inventory_app
from inventory.app.sync import (
    SyncAlreadyRunning,
    _exclusive,
    run_patch_sync,
    upsert_asset,
)
from patchtrack_common.security import create_access_token


def _add_user(engine, role: str) -> str:
    user_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO identity.users
                    (id, username, display_name, password_hash, role,
                     auth_source, is_active, must_change_password)
                VALUES (:id, :u, :d, 'x', :r, 'local', true, false)
                """
            ),
            {"id": user_id, "u": f"{role}-{user_id.hex[:8]}", "d": role, "r": role},
        )
    return str(user_id)


def _client(app, user_id: str, role: str) -> TestClient:
    token, _ = create_access_token(
        user_id=user_id, username="someone", role=role, token_version=0
    )
    client = TestClient(app)
    client.headers.update({"authorization": f"Bearer {token}"})
    return client


def _add_asset(engine) -> str:
    record = AssetRecord(
        source_id="AE-AUTH-1", hostname="auth-001", fqdn="auth-001.example.com",
        ip_address="10.0.0.9", os_name="Red Hat Enterprise Linux",
        os_version="9.4", asset_type="server", asset_status="In Use",
        environment="Production", business_group="Platform",
        application_group="Core", asset_owner="Platform", department="IT",
        location="DC1", lifecycle_status="active", classification="internal",
        source_created_at=None, source_modified_at=None, raw={},
    )
    with engine.begin() as conn:
        run_id = str(conn.execute(
            text("INSERT INTO inventory.sync_runs (integration_key, trigger) "
                 "VALUES ('assetexplorer', 'manual') RETURNING id")
        ).first()[0])
        asset_id, _ = upsert_asset(conn, record, run_id)
    return asset_id


def _add_assessment(engine, asset_id: str, *, when: datetime, advisory: str) -> str:
    with engine.begin() as conn:
        assessment_id = str(conn.execute(
            text(
                "INSERT INTO compliance.assessments "
                "(asset_id, source_system, assessed_at, status, total_patches) "
                "VALUES (:a, 'satellite', :t, 'complete', 1) RETURNING id"
            ),
            {"a": asset_id, "t": when},
        ).first()[0])
        conn.execute(
            text(
                "INSERT INTO compliance.findings "
                "(asset_id, assessment_id, source_system, advisory_id, title, "
                " severity) VALUES (:a, :s, 'satellite', :adv, :adv, 'critical')"
            ),
            {"a": asset_id, "s": assessment_id, "adv": advisory},
        )
    return assessment_id


def _point_at_latest(engine, asset_id: str, assessment_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO compliance.asset_latest_assessment "
                "(asset_id, assessment_id) VALUES (:a, :s) "
                "ON CONFLICT (asset_id) DO UPDATE SET assessment_id = EXCLUDED.assessment_id"
            ),
            {"a": asset_id, "s": assessment_id},
        )


def test_asset_detail_withholds_findings_without_findings_view(engine):
    asset_id = _add_asset(engine)
    assessment = _add_assessment(
        engine, asset_id, when=datetime(2026, 10, 1, tzinfo=UTC), advisory="RHSA-1",
    )
    _point_at_latest(engine, asset_id, assessment)

    viewer = _client(inventory_app, _add_user(engine, "viewer"), "viewer")
    detail = viewer.get(f"/assets/{asset_id}")
    assert detail.status_code == 200
    assert detail.json()["findings"] == []
    assert viewer.get(f"/assets/{asset_id}/findings").status_code == 403

    operator = _client(
        inventory_app, _add_user(engine, "operations"), "operations",
    )
    allowed = operator.get(f"/assets/{asset_id}")
    assert allowed.status_code == 200
    assert [f["advisory_id"] for f in allowed.json()["findings"]] == ["RHSA-1"]


def test_viewer_cannot_trigger_a_compliance_evaluation(engine):
    viewer = _client(compliance_app, _add_user(engine, "viewer"), "viewer")
    assert viewer.post("/evaluate").status_code == 403


def test_findings_total_counts_only_the_latest_assessment(engine):
    asset_id = _add_asset(engine)
    old = _add_assessment(
        engine, asset_id, when=datetime(2026, 9, 1, tzinfo=UTC), advisory="RHSA-OLD",
    )
    new = _add_assessment(
        engine, asset_id, when=datetime(2026, 10, 1, tzinfo=UTC), advisory="RHSA-NEW",
    )
    _point_at_latest(engine, asset_id, new)

    operator = _client(
        compliance_app, _add_user(engine, "operations"), "operations",
    )
    response = operator.get("/findings", params={"page_size": 50})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [row["advisory_id"] for row in body["items"]] == ["RHSA-NEW"]
    # A stale assessment is not listed even though its rows still exist.
    assert UUID(str(new)) != UUID(str(old))


def test_concurrent_collection_of_one_integration_is_refused(engine):
    """The advisory lock is what the inventory docstring promised."""
    with _exclusive("satellite"):
        with pytest.raises(SyncAlreadyRunning):
            run_patch_sync("satellite", trigger="manual", requested_by="test")
