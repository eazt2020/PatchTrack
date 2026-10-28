"""Justification lifecycle against a real database.

Recording a new justification supersedes the previous one instead of editing
it, so the claim and evidence that applied when a report was produced remain
recoverable. Withdrawing leaves the history intact.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from inventory.app.main import (
    EvidenceUpload,
    JustificationCreate,
    create_justification,
    list_justifications,
    withdraw_justification,
)
from patchtrack_common.security import Principal

OPERATOR = Principal(
    user_id="00000000-0000-0000-0000-000000000002",
    username="operator",
    role="operations",
    display_name="Operations",
)


class _Request:
    client = SimpleNamespace(host="127.0.0.1")
    headers: dict[str, str] = {}


@pytest.fixture
def asset_id(engine, clean_tables) -> str:
    with engine.begin() as conn:
        return str(conn.execute(
            text(
                """
                INSERT INTO inventory.assets (
                    assetexplorer_id, hostname, os_family, environment,
                    business_group
                ) VALUES ('AE-JUSTIFY-1', 'srv-justify', 'rhel', 'Production',
                          'Payments')
                RETURNING id
                """
            )
        ).first()[0])


def payload(statement: str, reason: str = "scheduled_remediation",
            with_evidence: bool = True) -> JustificationCreate:
    return JustificationCreate(
        reason_code=reason,
        statement=statement,
        remediation_plan="Apply RHSA-2026:1234 in the Tuesday window.",
        evidence=[EvidenceUpload(
            filename="change.txt", content_type="text/plain",
            data=base64.b64encode(b"CHG-1042").decode(),
        )] if with_evidence else [],
    )


def test_recording_a_justification_keeps_state_and_evidence(asset_id):
    created = create_justification(
        asset_id, payload("Verified against change record CHG-1042."),
        _Request(), OPERATOR)
    result = list_justifications(asset_id, OPERATOR)
    assert str(result["active"]["id"]) == created["id"]
    assert result["active"]["compliance_state"] == "unknown"
    assert result["active"]["reason_code"] == "scheduled_remediation"
    assert result["active"]["evidence"][0]["filename"] == "change.txt"


def test_new_justification_supersedes_and_preserves_history(asset_id):
    first = create_justification(
        asset_id, payload("The first verified explanation of this state."),
        _Request(), OPERATOR)
    second = create_justification(
        asset_id, payload("A later verification supersedes the first one.",
                          reason="accepted_risk", with_evidence=False),
        _Request(), OPERATOR)
    result = list_justifications(asset_id, OPERATOR)
    assert str(result["active"]["id"]) == second["id"]
    history = {str(row["id"]): row for row in result["history"]}
    assert history[first["id"]]["status"] == "superseded"
    assert history[first["id"]]["evidence"][0]["filename"] == "change.txt"


def test_withdrawing_leaves_the_record_for_audit(asset_id):
    created = create_justification(
        asset_id, payload("A verification that will be withdrawn."),
        _Request(), OPERATOR)
    withdraw_justification(asset_id, created["id"], _Request(), OPERATOR)
    result = list_justifications(asset_id, OPERATOR)
    assert result["active"] is None
    assert result["history"][0]["status"] == "withdrawn"


def test_short_statement_is_refused(asset_id):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        create_justification(
            asset_id, payload("too short"), _Request(), OPERATOR)
    assert exc.value.status_code == 422
