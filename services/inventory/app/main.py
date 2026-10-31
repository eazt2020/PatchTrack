"""PatchTrack inventory service.

Owns: AssetExplorer inventory synchronisation, patch-source collection,
asset/source reconciliation, integration configuration and health.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from patchtrack_common.metrics import install as install_metrics
from patchtrack_common.request_guards import install as install_request_guards
from patchtrack_common.scheduler import (
    SCHEDULER_PRINCIPAL,
    ScheduledJob,
    install as install_scheduler,
)
from patchtrack_common.audit import record_audit
from patchtrack_common.config import get_settings, release_version
from patchtrack_common.db import connection, execute, fetch_all, fetch_one
from patchtrack_common.net import client_ip
from patchtrack_common.secrets import (
    SecretError,
    resolve as resolve_secret,
    validate_reference,
)
from patchtrack_common.security import Principal, require_permission

from .connectors.assetexplorer import AssetExplorerClient, AssetExplorerError
from .connectors.satellite import SatelliteClient, SatelliteError
from .connectors.sccm import SccmClient, SccmError
from .connectors.solaris import SolarisIngestError, probe_inbox
from .connectors.solaris_ssh import (
    DEFAULT_COLLECTOR_COMMAND,
    SolarisSshClient,
    SolarisSshError,
    SolarisTarget,
    validate_baseline_entries,
)
from . import justifications as justification_rules
from .sync import (
    PATCH_SOURCE_KEYS,
    load_solaris_targets,
    resolve_conflict,
    run_assetexplorer_sync,
    run_patch_sync,
)

APP = "inventory"
app = FastAPI(title="PatchTrack Inventory", version=release_version(), docs_url="/docs")
install_metrics(app, APP)
install_request_guards(app)

ASSET_FILTER_COLUMNS = {
    "os_family": "a.os_family",
    "os_version": "a.os_version",
    "environment": "a.environment",
    "business_group": "a.business_group",
    "application_group": "a.application_group",
    "patch_source": "a.patch_source",
    "lifecycle_status": "a.lifecycle_status",
    "state": "c.state",
}

ASSET_BASE_SQL = """
    SELECT a.id, a.assetexplorer_id, a.hostname, a.fqdn,
           host(a.ip_address) AS ip_address, a.os_family, a.os_name,
           a.os_version, a.environment, a.business_group, a.application_group,
           a.asset_owner, a.department, a.location, a.lifecycle_status,
           a.asset_status, a.patch_source, a.in_scope, a.exclusion_reason,
           a.reconciliation_state, a.last_synced_at,
           c.state AS compliance_state, c.reason AS compliance_reason,
           c.evaluated_at, c.critical_count, c.total_patches,
           asmt.assessed_at AS last_assessed_at,
           a.environment_source, a.business_group_source,
           a.environment_overridden, a.business_group_overridden,
           a.override_reason, a.override_set_by, a.override_set_at
    FROM inventory.asset_effective a
    LEFT JOIN compliance.asset_compliance c ON c.asset_id = a.id
    LEFT JOIN compliance.asset_latest_assessment la ON la.asset_id = a.id
    LEFT JOIN compliance.assessments asmt ON asmt.id = la.assessment_id
"""


def _client_ip(request: Request) -> str | None:
    """The caller's address, or None when it cannot be established."""
    return client_ip(request)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": APP}


@app.get("/readyz")
def readyz() -> dict[str, str]:
    try:
        fetch_one("SELECT 1 AS ok")
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ready", "service": APP}


# --------------------------------------------------------------------------- assets
@app.get("/assets")
def list_assets(
    principal: Annotated[Principal, Depends(require_permission("inventory:view"))],
    search: str | None = None,
    os_family: str | None = None,
    os_version: str | None = None,
    environment: str | None = None,
    business_group: str | None = None,
    application_group: str | None = None,
    patch_source: str | None = None,
    lifecycle_status: str | None = None,
    compliance_state: str | None = None,
    in_scope: bool | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    sort: str = "hostname",
    direction: str = "asc",
) -> dict[str, Any]:
    clauses = ["1=1"]
    params: dict[str, Any] = {}
    # The stats tiles summarise the register the filters select, not just the
    # page on screen. The compliance-state filter is deliberately left out:
    # narrowing the list to "Non-compliant" should not zero the Compliant tile.
    stats_clauses = ["1=1"]
    stats_params: dict[str, Any] = {}
    if search:
        # Search spans the columns the register displays: identity, placement,
        # ownership and lifecycle all answer the same box. Compliance state is
        # matched as a whole term, not a substring: "Compliant" must not drag
        # in every "Non-compliant" server. Use the Status filter for exact
        # states.
        search_clause = (
            "(a.hostname ILIKE :search OR a.fqdn ILIKE :search "
            "OR host(a.ip_address) LIKE :search OR a.assetexplorer_id = :exact "
            "OR a.os_name ILIKE :search OR a.os_version ILIKE :search "
            "OR a.environment ILIKE :search OR a.business_group ILIKE :search "
            "OR a.application_group ILIKE :search OR a.patch_source ILIKE :search "
            "OR a.lifecycle_status ILIKE :search OR a.asset_owner ILIKE :search "
            "OR a.department ILIKE :search OR a.location ILIKE :search "
            "OR c.state = :state_search)"
        )
        search_params = {
            "search": f"%{search}%",
            "exact": search,
            "state_search": search.strip().lower().replace(" ", "_").replace("-", "_"),
        }
        clauses.append(search_clause)
        stats_clauses.append(search_clause)
        params.update(search_params)
        stats_params.update(search_params)
    for column, value in (
        ("os_family", os_family), ("os_version", os_version),
        ("environment", environment), ("business_group", business_group),
        ("application_group", application_group), ("patch_source", patch_source),
        ("lifecycle_status", lifecycle_status),
    ):
        if value:
            clause = f"{ASSET_FILTER_COLUMNS[column]} = :{column}"
            clauses.append(clause)
            stats_clauses.append(clause)
            params[column] = value
            stats_params[column] = value
    if compliance_state:
        if compliance_state == "unknown":
            clauses.append("(c.state = 'unknown' OR c.state IS NULL)")
        else:
            clauses.append("c.state = :compliance_state")
            params["compliance_state"] = compliance_state
    if in_scope is not None:
        clause = "a.in_scope = :in_scope"
        clauses.append(clause)
        stats_clauses.append(clause)
        params["in_scope"] = in_scope
        stats_params["in_scope"] = in_scope
    where = " AND ".join(clauses)
    stats_where = " AND ".join(stats_clauses)

    sort_column = ASSET_FILTER_COLUMNS.get(sort, "a.hostname")
    if sort == "hostname":
        sort_column = "a.hostname"
    order = "DESC" if direction.lower() == "desc" else "ASC"
    # The stats query already counts the whole filtered register, so when the
    # compliance-state filter is absent (the common page load) its total is the
    # page total too. Only a state-filtered list needs the extra count.
    stats = fetch_one(
        "SELECT count(*) AS total, "
        "count(*) FILTER (WHERE c.state = 'excluded') AS excluded, "
        "count(*) FILTER (WHERE c.state = 'compliant') AS compliant, "
        "count(*) FILTER (WHERE c.state = 'non_compliant') AS non_compliant, "
        "count(*) FILTER (WHERE c.state = 'unknown') AS unknown, "
        "count(*) FILTER (WHERE c.state = 'stale') AS stale, "
        "count(*) FILTER (WHERE c.state IS NULL) AS not_assessed "
        "FROM inventory.asset_effective a "
        "LEFT JOIN compliance.asset_compliance c ON c.asset_id = a.id "
        f"WHERE {stats_where}",
        **stats_params,
    ) or {}
    if compliance_state:
        total = fetch_one(
            f"SELECT count(*) AS n FROM inventory.asset_effective a "
            f"LEFT JOIN compliance.asset_compliance c ON c.asset_id = a.id "
            f"WHERE {where}",
            **params,
        )
        total_n = total["n"] if total else 0
    else:
        total_n = stats.get("total", 0)
    rows = fetch_all(
        f"{ASSET_BASE_SQL} WHERE {where} ORDER BY {sort_column} {order}, a.id "
        "LIMIT :limit OFFSET :offset",
        **params, limit=page_size, offset=(page - 1) * page_size,
    )
    return {
        "items": rows,
        "total": total_n,
        "page": page,
        "page_size": page_size,
        "stats": stats,
    }


@app.get("/assets/{asset_id}")
def get_asset(
    asset_id: UUID,
    principal: Annotated[Principal, Depends(require_permission("inventory:detail"))],
) -> dict[str, Any]:
    asset = fetch_one(
        f"{ASSET_BASE_SQL} WHERE a.id = :id", id=asset_id
    )
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    assessment = fetch_one(
        """
        SELECT asmt.*, la.updated_at AS became_latest_at
        FROM compliance.asset_latest_assessment la
        JOIN compliance.assessments asmt ON asmt.id = la.assessment_id
        WHERE la.asset_id = :id
        """,
        id=asset_id,
    )
    # The asset page is available to every role holding inventory:detail, but
    # the findings themselves are gated by findings:view on the dedicated
    # /findings route. Honour the same gate here rather than widening it.
    findings = (
        fetch_all(
            """
            SELECT source_system, advisory_id, title, severity, category,
                   released_at, remediation_due, source_reference
            FROM compliance.findings WHERE asset_id = :id
            ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'important' THEN 1
                     WHEN 'moderate' THEN 2 WHEN 'low' THEN 3 ELSE 4 END,
                     released_at DESC NULLS LAST
            LIMIT 500
            """,
            id=asset_id,
        )
        if principal.can("findings:view")
        else []
    )
    links = fetch_all(
        "SELECT source_system, source_ref, confidence, method, linked_at "
        "FROM inventory.asset_source_links WHERE asset_id = :id",
        id=asset_id,
    )
    history = fetch_all(
        """
        SELECT source_system, assessed_at, collected_at, status, total_patches,
               critical_count, important_count, moderate_count, low_count
        FROM compliance.assessments WHERE asset_id = :id
        ORDER BY assessed_at DESC NULLS LAST LIMIT 30
        """,
        id=asset_id,
    )
    return {
        "asset": asset,
        "assessment": assessment,
        "findings": findings,
        "source_links": links,
        "assessment_history": history,
        "justifications": _justification_rows(asset_id),
    }


@app.get("/assets/{asset_id}/findings")
def asset_findings(
    asset_id: UUID,
    principal: Annotated[Principal, Depends(require_permission("findings:view"))],
    severity: str | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM compliance.findings WHERE asset_id = :id"
    params: dict[str, Any] = {"id": asset_id}
    if severity:
        sql += " AND severity = :sev"
        params["sev"] = severity
    return fetch_all(sql + " ORDER BY released_at DESC NULLS LAST LIMIT 1000", **params)


# ---------------------------------------------------------------- justifications
class EvidenceUpload(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=128)
    # Base64, optionally a data: URI. Kept in JSON so the service needs no
    # multipart parser and the gateway can proxy it unchanged.
    data: str


class JustificationCreate(BaseModel):
    reason_code: str = Field(min_length=1, max_length=64)
    statement: str = Field(min_length=1, max_length=4000)
    remediation_plan: str = Field(default="", max_length=2000)
    remediation_target: date | None = None
    evidence: list[EvidenceUpload] = Field(default_factory=list)


def _justification_rows(asset_id: UUID) -> dict[str, Any]:
    rows = fetch_all(
        """
        SELECT j.id, j.asset_id, j.compliance_state, j.reason_code, j.statement,
               j.remediation_plan, j.remediation_target, j.status, j.created_by,
               j.created_at, j.superseded_at, j.superseded_by,
               e.id AS evidence_id, e.filename, e.content_type, e.byte_size,
               e.sha256, e.uploaded_by, e.uploaded_at
        FROM inventory.asset_justifications j
        LEFT JOIN inventory.justification_evidence e ON e.justification_id = j.id
        WHERE j.asset_id = :id
        ORDER BY j.created_at DESC, e.uploaded_at
        """,
        id=asset_id,
    )
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        jid = str(row["id"])
        item = by_id.get(jid)
        if item is None:
            item = {
                key: row[key] for key in (
                    "id", "asset_id", "compliance_state", "reason_code",
                    "statement", "remediation_plan", "remediation_target",
                    "status", "created_by", "created_at", "superseded_at",
                    "superseded_by",
                )
            }
            item["reason_label"] = justification_rules.REASONS.get(
                row["reason_code"], row["reason_code"])
            item["evidence"] = []
            by_id[jid] = item
        if row["evidence_id"]:
            item["evidence"].append({
                "id": row["evidence_id"], "filename": row["filename"],
                "content_type": row["content_type"], "byte_size": row["byte_size"],
                "sha256": row["sha256"], "uploaded_by": row["uploaded_by"],
                "uploaded_at": row["uploaded_at"],
            })
    records = list(by_id.values())
    active = next((r for r in records if r["status"] == "active"), None)
    return {"active": active, "history": records}


@app.get("/assets/{asset_id}/justifications")
def list_justifications(
    asset_id: UUID,
    _: Annotated[Principal, Depends(require_permission("inventory:detail"))],
) -> dict[str, Any]:
    if fetch_one("SELECT id FROM inventory.assets WHERE id = :id", id=asset_id) is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return _justification_rows(asset_id)


@app.post("/assets/{asset_id}/justifications", status_code=201)
def create_justification(
    asset_id: UUID,
    payload: JustificationCreate,
    request: Request,
    principal: Annotated[Principal, Depends(
        require_permission("compliance:justify"))],
) -> dict[str, Any]:
    asset = fetch_one(
        """
        SELECT a.hostname, c.state
        FROM inventory.assets a
        LEFT JOIN compliance.asset_compliance c ON c.asset_id = a.id
        WHERE a.id = :id
        """,
        id=asset_id,
    )
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    state = justification_rules.effective_state(asset["state"])
    if state not in justification_rules.JUSTIFIABLE_STATES:
        raise HTTPException(
            status_code=409,
            detail="Only a non-compliant, stale or unknown asset needs a justification",
        )
    try:
        reason = justification_rules.validate_reason(payload.reason_code)
        statement = justification_rules.validate_statement(payload.statement)
        plan = justification_rules.validate_plan(payload.remediation_plan)
        evidence = justification_rules.normalise_evidence(
            [item.model_dump() for item in payload.evidence])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        with connection() as conn:
            conn.execute(
                text(
                    """
                    UPDATE inventory.asset_justifications
                    SET status = 'superseded', superseded_at = now(),
                        superseded_by = :by
                    WHERE asset_id = :id AND status = 'active'
                    """
                ),
                {"by": principal.username, "id": asset_id},
            )
            justification_id = conn.execute(
                text(
                    """
                    INSERT INTO inventory.asset_justifications (
                        asset_id, compliance_state, reason_code, statement,
                        remediation_plan, remediation_target, created_by
                    ) VALUES (:asset, :state, :reason, :statement, :plan, :target, :by)
                    RETURNING id
                    """
                ),
                {
                    "asset": asset_id, "state": state, "reason": reason,
                    "statement": statement, "plan": plan,
                    "target": payload.remediation_target, "by": principal.username,
                },
            ).first()[0]
            for item in evidence:
                conn.execute(
                    text(
                        """
                        INSERT INTO inventory.justification_evidence (
                            justification_id, filename, content_type, byte_size,
                            sha256, content, uploaded_by
                        ) VALUES (:j, :f, :t, :s, :h, :c, :by)
                        """
                    ),
                    {
                        "j": justification_id, "f": item.filename,
                        "t": item.content_type, "s": item.size,
                        "h": item.sha256, "c": item.content,
                        "by": principal.username,
                    },
                )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="Another justification was recorded at the same time; reload "
                   "the server and try again",
        ) from exc
    record_audit(
        event_type="compliance.justification_recorded", outcome="success",
        principal=principal, target_type="asset", target_id=str(asset_id),
        context={
            "justification_id": str(justification_id), "state": state,
            "reason": reason, "evidence_count": len(evidence),
        },
        source_ip=_client_ip(request), service=APP,
    )
    return {"status": "recorded", "id": str(justification_id),
            "evidence_count": len(evidence)}


@app.post("/assets/{asset_id}/justifications/{justification_id}/evidence",
          status_code=201)
def add_justification_evidence(
    asset_id: UUID,
    justification_id: UUID,
    payload: list[EvidenceUpload],
    request: Request,
    principal: Annotated[Principal, Depends(
        require_permission("compliance:justify"))],
) -> dict[str, Any]:
    item = fetch_one(
        """
        SELECT id, status FROM inventory.asset_justifications
        WHERE id = :j AND asset_id = :a
        """,
        j=justification_id, a=asset_id,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Justification not found")
    if item["status"] != "active":
        raise HTTPException(
            status_code=409,
            detail="Evidence can only be attached to the current justification",
        )
    try:
        evidence = justification_rules.normalise_evidence(
            [entry.model_dump() for entry in payload])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    existing = fetch_one(
        "SELECT count(*) AS n FROM inventory.justification_evidence "
        "WHERE justification_id = :j", j=justification_id,
    )
    if (existing["n"] if existing else 0) + len(evidence) > (
            justification_rules.MAX_EVIDENCE_PER_JUSTIFICATION):
        raise HTTPException(
            status_code=422,
            detail=f"Attach at most "
                   f"{justification_rules.MAX_EVIDENCE_PER_JUSTIFICATION} "
                   "evidence files to one justification",
        )
    with connection() as conn:
        for entry in evidence:
            conn.execute(
                text(
                    """
                    INSERT INTO inventory.justification_evidence (
                        justification_id, filename, content_type, byte_size,
                        sha256, content, uploaded_by
                    ) VALUES (:j, :f, :t, :s, :h, :c, :by)
                    """
                ),
                {
                    "j": justification_id, "f": entry.filename,
                    "t": entry.content_type, "s": entry.size,
                    "h": entry.sha256, "c": entry.content,
                    "by": principal.username,
                },
            )
    record_audit(
        event_type="compliance.justification_evidence_added", outcome="success",
        principal=principal, target_type="asset", target_id=str(asset_id),
        context={"justification_id": str(justification_id),
                 "evidence_count": len(evidence)},
        source_ip=_client_ip(request), service=APP,
    )
    return {"status": "attached", "evidence_count": len(evidence)}


@app.get("/assets/{asset_id}/justifications/{justification_id}/evidence/{evidence_id}")
def download_justification_evidence(
    asset_id: UUID,
    justification_id: UUID,
    evidence_id: UUID,
    _: Annotated[Principal, Depends(require_permission("inventory:detail"))],
) -> Response:
    row = fetch_one(
        """
        SELECT e.filename, e.content_type, e.content
        FROM inventory.justification_evidence e
        JOIN inventory.asset_justifications j ON j.id = e.justification_id
        WHERE e.id = :e AND j.id = :j AND j.asset_id = :a
        """,
        e=evidence_id, j=justification_id, a=asset_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Evidence not found")
    safe = re.sub(r'[^\w.\- ]', "_", row["filename"])[:120] or "evidence"
    return Response(
        content=bytes(row["content"]), media_type=row["content_type"],
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
    )


@app.delete("/assets/{asset_id}/justifications/{justification_id}")
def withdraw_justification(
    asset_id: UUID,
    justification_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(
        require_permission("compliance:justify"))],
) -> dict[str, str]:
    updated = execute(
        """
        UPDATE inventory.asset_justifications
        SET status = 'withdrawn', superseded_at = now(), superseded_by = :by
        WHERE id = :j AND asset_id = :a AND status = 'active'
        """,
        j=justification_id, a=asset_id, by=principal.username,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Current justification not found")
    record_audit(
        event_type="compliance.justification_withdrawn", outcome="success",
        principal=principal, target_type="asset", target_id=str(asset_id),
        context={"justification_id": str(justification_id)},
        source_ip=_client_ip(request), service=APP,
    )
    return {"status": "withdrawn"}


@app.post("/assets/{asset_id}/exclude")
def exclude_asset(
    asset_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("policies:manage"))],
    reason: str = Query(min_length=5, max_length=500),
) -> dict[str, str]:
    # Validate after trimming for the same reason as PlacementOverride: the
    # query length check counts whitespace.
    reason = reason.strip()
    if len(reason) < 5:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="reason must be at least 5 non-space characters",
        )
    updated = execute(
        "UPDATE inventory.assets SET in_scope = false, exclusion_reason = :r "
        "WHERE id = :id",
        r=reason, id=asset_id,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Asset not found")
    record_audit(
        event_type="compliance.exclusion_added", outcome="success",
        principal=principal, target_type="asset", target_id=str(asset_id),
        context={"reason": reason}, source_ip=_client_ip(request), service=APP,
    )
    return {"status": "excluded"}


@app.post("/assets/{asset_id}/include")
def include_asset(
    asset_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("policies:manage"))],
) -> dict[str, str]:
    updated = execute(
        "UPDATE inventory.assets SET in_scope = true, exclusion_reason = '' WHERE id = :id",
        id=asset_id,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Asset not found")
    record_audit(
        event_type="compliance.exclusion_removed", outcome="success",
        principal=principal, target_type="asset", target_id=str(asset_id),
        source_ip=_client_ip(request), service=APP,
    )
    return {"status": "included"}


# --------------------------------------------------------------------------- placement overrides
class PlacementOverride(BaseModel):
    """An explicit local correction to the placement AssetExplorer supplies.

    A null or blank field means "use the source value for this field". Both
    blank is the same as clearing the override.
    """

    environment: str | None = Field(default=None, max_length=128)
    business_group: str | None = Field(default=None, max_length=128)
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def _trim_reason(cls, value: str) -> str:
        # Length is checked before trimming, so whitespace alone would pass
        # min_length and then be stored as an empty justification.
        value = value.strip()
        if len(value) < 3:
            raise ValueError("reason must be at least 3 non-space characters")
        return value


def _placement_query(asset_id: UUID) -> dict[str, Any] | None:
    return fetch_one(
        """
        SELECT id, environment, business_group,
               environment_source, business_group_source,
               environment_overridden, business_group_overridden,
               override_reason, override_set_by, override_set_at
        FROM inventory.asset_effective WHERE id = :id
        """,
        id=asset_id,
    )


def _clean_placement(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


@app.put("/assets/{asset_id}/overrides")
def set_asset_override(
    asset_id: UUID,
    payload: PlacementOverride,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("inventory:manage"))],
) -> dict[str, Any]:
    """Set or replace the local environment / business-service override.

    The source columns are never written, so the AssetExplorer sync keeps
    updating its own values and the override survives it. The asset remains
    matched by assetexplorer_id; nothing about reconciliation changes.
    """
    current = _placement_query(asset_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    environment = _clean_placement(payload.environment)
    business_group = _clean_placement(payload.business_group)
    reason = payload.reason

    if environment is None and business_group is None:
        execute("DELETE FROM inventory.asset_overrides WHERE asset_id = :id",
                id=asset_id)
    else:
        execute(
            """
            INSERT INTO inventory.asset_overrides
                (asset_id, environment, business_group, reason, set_by, set_at)
            VALUES (:id, :environment, :business_group, :reason, :by, now())
            ON CONFLICT (asset_id) DO UPDATE SET
                environment = EXCLUDED.environment,
                business_group = EXCLUDED.business_group,
                reason = EXCLUDED.reason,
                set_by = EXCLUDED.set_by,
                set_at = now()
            """,
            id=asset_id, environment=environment,
            business_group=business_group, reason=reason,
            by=principal.username,
        )

    record_audit(
        event_type="inventory.placement_overridden", outcome="success",
        principal=principal, target_type="asset", target_id=str(asset_id),
        context={
            "environment": {"from": current["environment"], "to": environment},
            "business_group": {"from": current["business_group"],
                               "to": business_group},
            "reason": reason,
        },
        source_ip=_client_ip(request), service=APP,
    )
    return {"asset": _placement_query(asset_id)}


@app.delete("/assets/{asset_id}/overrides")
def clear_asset_override(
    asset_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("inventory:manage"))],
) -> dict[str, Any]:
    """Return the asset to the placement AssetExplorer reports."""
    current = _placement_query(asset_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    removed = execute(
        "DELETE FROM inventory.asset_overrides WHERE asset_id = :id", id=asset_id
    )
    if removed:
        record_audit(
            event_type="inventory.placement_restored", outcome="success",
            principal=principal, target_type="asset", target_id=str(asset_id),
            context={"environment": current["environment"],
                     "business_group": current["business_group"]},
            source_ip=_client_ip(request), service=APP,
        )
    return {"asset": _placement_query(asset_id)}


# --------------------------------------------------------------------------- dimensions
@app.get("/dimensions")
def dimensions(
    _: Annotated[Principal, Depends(require_permission("inventory:view"))],
) -> dict[str, list[str]]:
    def distinct(column: str) -> list[str]:
        rows = fetch_all(
            f"SELECT DISTINCT {column} AS v FROM inventory.asset_effective "
            f"WHERE {column} IS NOT NULL AND {column} <> '' ORDER BY 1"
        )
        return [r["v"] for r in rows]

    return {
        "os_family": distinct("os_family"),
        "os_version": distinct("os_version"),
        "environment": distinct("environment"),
        "business_group": distinct("business_group"),
        "application_group": distinct("application_group"),
        "patch_source": distinct("patch_source"),
        "lifecycle_status": distinct("lifecycle_status"),
    }


# --------------------------------------------------------------------------- settings
# The organisation block is the report's branding. It lives in the settings
# table rather than on disk so a container replacement cannot lose the logo,
# and so every reporting replica renders the same cover.
LOGO_SIGNATURES = {
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/jpeg": b"\xff\xd8\xff",
}
MAX_LOGO_BYTES = 1_000_000
LOGO_DATA_RE = re.compile(r"^data:(image/(?:png|jpeg));base64,([A-Za-z0-9+/=\s]+)$")


class OrganizationSetting(BaseModel):
    """Partial update: an omitted field keeps its stored value.

    A plain PUT of ``{"name": ...}`` therefore still works and, importantly,
    cannot erase a previously uploaded logo by accident.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    logo_data: str | None = Field(default=None, max_length=2_000_000)
    logo_filename: str | None = Field(default=None, max_length=120)
    preparer_name: str | None = Field(default=None, max_length=120)
    preparer_title: str | None = Field(default=None, max_length=120)
    concur_name: str | None = Field(default=None, max_length=120)
    concur_title: str | None = Field(default=None, max_length=120)
    remove_logo: bool = False


def _validated_logo(data_uri: str) -> str:
    """Accept only a small PNG or JPEG data URI, verified by its signature.

    The file extension and the browser's Content-Type are both caller-supplied,
    so neither is trusted. ReportLab can only embed raster images here, and a
    logo that is not really an image would either break report generation or,
    worse, be published as part of the evidence.
    """
    match = LOGO_DATA_RE.match(data_uri.strip())
    if not match:
        raise HTTPException(
            status_code=422,
            detail="Logo must be a PNG or JPEG image encoded as a data URI",
        )
    media_type, payload = match.group(1), re.sub(r"\s+", "", match.group(2))
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Logo data is not valid base64") from exc
    if len(raw) > MAX_LOGO_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"Logo must be no larger than {MAX_LOGO_BYTES // 1000} KB",
        )
    signature = LOGO_SIGNATURES[media_type]
    if not raw.startswith(signature):
        raise HTTPException(
            status_code=422,
            detail="Logo content does not match its declared image type",
        )
    return f"data:{media_type};base64,{payload}"


def _organization_view(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": value.get("name", ""),
        "logo_data": value.get("logo_data") or "",
        "logo_filename": value.get("logo_filename") or "",
        "preparer_name": value.get("preparer_name", ""),
        "preparer_title": value.get("preparer_title", ""),
        "concur_name": value.get("concur_name", ""),
        "concur_title": value.get("concur_title", ""),
    }


@app.get("/settings/organization")
def get_organization(
    _: Annotated[Principal, Depends(require_permission("inventory:view"))],
) -> dict[str, Any]:
    row = fetch_one(
        "SELECT value, updated_at, updated_by FROM shared.app_settings "
        "WHERE key = 'organization'"
    )
    if row is None:
        return {**_organization_view({}), "updated_at": None, "updated_by": None}
    value = row["value"]
    if isinstance(value, str):
        value = json.loads(value)
    return {
        **_organization_view(value),
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
    }


@app.put("/settings/organization")
def set_organization(
    payload: OrganizationSetting,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("settings:manage"))],
) -> dict[str, Any]:
    """Report branding used on generated PDFs. Survives restarts (stored in the DB)."""
    row = fetch_one(
        "SELECT value FROM shared.app_settings WHERE key = 'organization'"
    )
    value: dict[str, Any] = {}
    if row:
        value = row["value"]
        if isinstance(value, str):
            value = json.loads(value)

    if payload.name is not None:
        value["name"] = payload.name.strip()
    for field in ("preparer_name", "preparer_title", "concur_name", "concur_title"):
        field_value = getattr(payload, field)
        if field_value is not None:
            value[field] = field_value.strip()
    if payload.remove_logo:
        value.pop("logo_data", None)
        value.pop("logo_filename", None)
    if payload.logo_data is not None:
        value["logo_data"] = _validated_logo(payload.logo_data)
        value["logo_filename"] = (payload.logo_filename or "logo").strip()[:120]

    execute(
        """
        INSERT INTO shared.app_settings (key, value, updated_at, updated_by)
        VALUES ('organization', CAST(:v AS jsonb), now(), :by)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
            updated_at = now(), updated_by = EXCLUDED.updated_by
        """,
        v=json.dumps(value), by=principal.username,
    )
    record_audit(
        event_type="settings.updated", outcome="success", principal=principal,
        target_type="setting", target_id="organization",
        context={
            "name": value.get("name", ""),
            "logo": bool(value.get("logo_data")),
            "preparer": value.get("preparer_name", ""),
            "concur": value.get("concur_name", ""),
        },
        source_ip=_client_ip(request), service=APP,
    )
    return _organization_view(value)


# --------------------------------------------------------------------------- placement options
# Environment and business service are free text in AssetExplorer. These rows
# are the values offered by the placement editor, and they are authoritative
# for display: renaming an entry records the old spelling as an alias, so every
# reader resolves the old value to the new name while the source record stays
# untouched and the next sync cannot undo it. Deleting is allowed only when no
# server holds the value, because removing a value that is still in use would
# leave the inventory disagreeing with the list.
PLACEMENT_KINDS: tuple[str, ...] = ("environment", "business_group")
PLACEMENT_LABELS = {
    "environment": "environment",
    "business_group": "business service",
}


class PlacementOptionCreate(BaseModel):
    kind: Literal["environment", "business_group"]
    value: str = Field(min_length=1, max_length=128)


class PlacementOptionRename(BaseModel):
    value: str = Field(min_length=1, max_length=128)


def _option_value(value: str) -> str:
    """Trimmed value, rejected rather than stored when it is only whitespace."""
    value = value.strip()
    if not value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A value is required",
        )
    return value


def _alias_holder(kind: str, value: str) -> dict[str, Any] | None:
    """The entry whose previous name is `value`, if any.

    A value that was renamed away is still resolved by every reader, so it
    cannot also be introduced as a new entry: the two spellings would mean
    different things in different places.
    """
    return fetch_one(
        """
        SELECT pa.option_id, po.value AS current_value
        FROM inventory.placement_aliases pa
        JOIN inventory.placement_options po ON po.id = pa.option_id
        WHERE pa.kind = :kind
          AND lower(btrim(pa.alias)) = lower(btrim(:value))
        """,
        kind=kind, value=value,
    )


def _placement_option(option_id: UUID) -> dict[str, Any] | None:
    return fetch_one(
        """
        SELECT o.id, o.kind, o.value, o.created_by, o.created_at,
               o.updated_by, o.updated_at,
               CASE WHEN o.kind = 'environment'
                    THEN (SELECT count(*) FROM inventory.asset_effective a
                          WHERE a.environment = o.value)
                    ELSE (SELECT count(*) FROM inventory.asset_effective a
                          WHERE a.business_group = o.value)
               END AS in_use
        FROM inventory.placement_options o
        WHERE o.id = :id
        """,
        id=option_id,
    )


def _placement_options() -> dict[str, Any]:
    rows = fetch_all(
        """
        SELECT o.id, o.kind, o.value, o.created_by, o.created_at,
               o.updated_by, o.updated_at,
               CASE WHEN o.kind = 'environment'
                    THEN (SELECT count(*) FROM inventory.asset_effective a
                          WHERE a.environment = o.value)
                    ELSE (SELECT count(*) FROM inventory.asset_effective a
                          WHERE a.business_group = o.value)
               END AS in_use
        FROM inventory.placement_options o
        ORDER BY lower(o.value)
        """
    )
    return {
        "environments": [r for r in rows if r["kind"] == "environment"],
        "business_groups": [r for r in rows if r["kind"] == "business_group"],
    }


@app.get("/settings/placement")
def list_placement_options(
    _: Annotated[Principal, Depends(require_permission("inventory:view"))],
) -> dict[str, Any]:
    """The vocabulary offered by the placement editor, with current usage.

    `in_use` is how many servers' **effective** placement equals the value
    (source, override, or a previous name that was renamed to it). A rename
    moves those servers to the new name; a removal is refused while any server
    still holds the value.
    """
    return _placement_options()


@app.post("/settings/placement", status_code=status.HTTP_201_CREATED)
def create_placement_option(
    payload: PlacementOptionCreate,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("settings:manage"))],
) -> dict[str, Any]:
    value = _option_value(payload.value)
    holder = _alias_holder(payload.kind, value)
    if holder is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"“{value}” was renamed to “{holder['current_value']}” "
                   "and is still resolved there",
        )
    try:
        row = fetch_one(
            """
            INSERT INTO inventory.placement_options (kind, value, created_by)
            VALUES (:kind, :value, :by)
            RETURNING id
            """,
            kind=payload.kind, value=value, by=principal.username,
        )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"“{value}” is already in the "
                   f"{PLACEMENT_LABELS[payload.kind]} list",
        ) from exc
    if row is None:  # pragma: no cover - RETURNING always yields a row
        raise HTTPException(status_code=500, detail="Placement option not stored")
    record_audit(
        event_type="settings.placement_option_added", outcome="success",
        principal=principal, target_type="placement_option",
        target_id=str(row["id"]),
        context={"kind": payload.kind, "value": value},
        source_ip=_client_ip(request), service=APP,
    )
    return _placement_option(row["id"]) or {}


@app.patch("/settings/placement/{option_id}")
def rename_placement_option(
    option_id: UUID,
    payload: PlacementOptionRename,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("settings:manage"))],
) -> dict[str, Any]:
    value = _option_value(payload.value)
    kind: str | None = None
    previous: str | None = None
    try:
        with connection() as conn:
            current = conn.execute(
                text(
                    """
                    SELECT kind, value
                    FROM inventory.placement_options
                    WHERE id = :id
                    FOR UPDATE
                    """
                ),
                {"id": option_id},
            ).mappings().first()
            if current is None:
                raise HTTPException(
                    status_code=404, detail="Placement option not found"
                )
            kind = current["kind"]
            previous = current["value"]
            if previous != value:
                # Another entry previously answered to the new name: renaming
                # into it would make one spelling mean two things.
                holder = conn.execute(
                    text(
                        """
                        SELECT po.value
                        FROM inventory.placement_aliases pa
                        JOIN inventory.placement_options po
                          ON po.id = pa.option_id
                        WHERE pa.kind = :kind
                          AND lower(btrim(pa.alias)) = lower(btrim(:value))
                          AND po.id <> :id
                        """
                    ),
                    {"kind": kind, "value": value, "id": option_id},
                ).mappings().first()
                if holder is not None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"“{value}” was renamed to "
                               f"“{holder['value']}” and is still resolved "
                               "there",
                    )
                # The old spelling keeps resolving to this entry, so every
                # reader (inventory, compliance and reports) shows the new
                # name without the source record being rewritten.
                conn.execute(
                    text(
                        """
                        INSERT INTO inventory.placement_aliases
                            (kind, alias, option_id, created_by)
                        VALUES (:kind, :alias, :id, :by)
                        ON CONFLICT DO NOTHING
                        """
                    ),
                    {
                        "kind": kind, "alias": previous,
                        "id": option_id, "by": principal.username,
                    },
                )
                # Reclaiming a former name of this same entry must not leave
                # an alias that now contradicts the current value.
                conn.execute(
                    text(
                        """
                        DELETE FROM inventory.placement_aliases
                        WHERE option_id = :id
                          AND kind = :kind
                          AND lower(btrim(alias)) = lower(btrim(:value))
                        """
                    ),
                    {"id": option_id, "kind": kind, "value": value},
                )
                conn.execute(
                    text(
                        """
                        UPDATE inventory.placement_options
                        SET value = :value, updated_at = now(),
                            updated_by = :by
                        WHERE id = :id
                        """
                    ),
                    {"id": option_id, "value": value, "by": principal.username},
                )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"“{value}” is already in the "
                   f"{PLACEMENT_LABELS.get(kind, 'placement')} list",
        ) from exc
    record_audit(
        event_type="settings.placement_option_renamed", outcome="success",
        principal=principal, target_type="placement_option",
        target_id=str(option_id),
        context={
            "kind": kind, "from": previous, "to": value,
            # Every server holding the old spelling now displays the new one;
            # the value AssetExplorer reported is preserved.
            "propagated": previous != value,
        },
        source_ip=_client_ip(request), service=APP,
    )
    return _placement_option(option_id) or {}


@app.delete("/settings/placement/{option_id}")
def delete_placement_option(
    option_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("settings:manage"))],
) -> dict[str, Any]:
    """Remove a value from the list.

    Refused while any server still holds it: deleting an in-use value would
    leave the inventory showing a placement the list no longer recognises.
    Rename it instead, which propagates to the servers.
    """
    existing = _placement_option(option_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Placement option not found")
    if existing["in_use"] > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{existing['in_use']} server(s) still use “"
                   f"{existing['value']}”. Rename it instead, or change "
                   "those servers' placement first.",
        )
    execute("DELETE FROM inventory.placement_options WHERE id = :id", id=option_id)
    record_audit(
        event_type="settings.placement_option_removed", outcome="success",
        principal=principal, target_type="placement_option",
        target_id=str(option_id),
        context={
            "kind": existing["kind"], "value": existing["value"],
            "servers_unchanged": existing["in_use"],
        },
        source_ip=_client_ip(request), service=APP,
    )
    return {
        "deleted": True,
        "kind": existing["kind"],
        "value": existing["value"],
        "in_use": existing["in_use"],
    }


# --------------------------------------------------------------------------- integrations
class IntegrationUpdate(BaseModel):
    display_name: str | None = None
    endpoint: str | None = None
    auth_method: str | None = None
    secret_ref: str | None = Field(
        default=None, description="Write-only. Never returned by the API."
    )
    enabled: bool | None = None
    sync_interval_minutes: int | None = Field(default=None, ge=5, le=10080)
    connect_timeout_s: int | None = Field(default=None, ge=1, le=300)
    read_timeout_s: int | None = Field(default=None, ge=1, le=900)
    page_size: int | None = Field(default=None, ge=1, le=1000)
    verify_tls: bool | None = None
    extra: dict[str, Any] | None = None


INTEGRATION_PUBLIC_COLUMNS = """
    key, display_name, kind, endpoint, auth_method,
    (secret_ref <> '') AS secret_configured, enabled,
    sync_interval_minutes, connect_timeout_s, read_timeout_s, page_size,
    verify_tls, extra, updated_at, updated_by
"""


@app.get("/integrations")
def list_integrations(
    _: Annotated[Principal, Depends(require_permission("integrations:view"))],
) -> list[dict[str, Any]]:
    rows = fetch_all(
        f"SELECT {INTEGRATION_PUBLIC_COLUMNS} FROM inventory.integrations ORDER BY key"
    )
    # Solaris target counts are set-based, so read them once rather than from
    # inside the per-integration loop.
    solaris_counts = fetch_one(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE enabled) AS enabled,
               count(*) FILTER (WHERE enabled AND last_error <> '') AS failing
        FROM inventory.solaris_targets
        """
    ) or {}
    for row in rows:
        # The latest run and the latest successful run come back together;
        # one round trip per integration instead of two.
        last = fetch_one(
            """
            SELECT status, started_at, finished_at, records_seen, records_created,
                   records_updated, records_rejected, assets_unmatched,
                   error_summary,
                   EXTRACT(EPOCH FROM (finished_at - started_at)) AS duration_s,
                   (SELECT max(finished_at) FROM inventory.sync_runs
                    WHERE integration_key = :k AND status = 'success')
                     AS last_success_at
            FROM inventory.sync_runs WHERE integration_key = :k
            ORDER BY started_at DESC LIMIT 1
            """,
            k=row["key"],
        )
        row["last_run"] = last
        row["last_success_at"] = last.pop("last_success_at") if last else None
        if row["key"] == "solaris":
            row["target_count"] = int(solaris_counts.get("enabled") or 0)
            row["target_total"] = int(solaris_counts.get("total") or 0)
            row["target_failing"] = int(solaris_counts.get("failing") or 0)
        row["health"] = _health(row, last)
    return rows


def _health(integration: dict[str, Any], last: dict[str, Any] | None) -> str:
    # Solaris is pulled per host: there is no single endpoint, so configured
    # targets stand in for one.
    configured = bool(integration["endpoint"]) or bool(
        integration.get("target_count")
    )
    if not integration["enabled"]:
        return "disabled" if configured else "not_configured"
    if not configured:
        return "not_configured"
    if not last:
        return "pending"
    if last["status"] == "failed":
        error = (last["error_summary"] or "").lower()
        if "auth" in error:
            return "authentication_failure"
        return "unavailable"
    if last["status"] == "partial":
        return "degraded"
    interval = timedelta(minutes=integration["sync_interval_minutes"])
    last_success = integration.get("last_success_at")
    if last_success and datetime.now(UTC) - last_success > interval * 2:
        return "stale"
    return "healthy"


@app.patch("/integrations/{key}")
def update_integration(
    key: str,
    payload: IntegrationUpdate,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("integrations:manage"))],
) -> dict[str, Any]:
    if key not in ("assetexplorer", *PATCH_SOURCE_KEYS):
        raise HTTPException(status_code=404, detail="Unknown integration")
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No changes supplied")
    for field in ("endpoint", "auth_method", "sync_interval_minutes",
                  "connect_timeout_s", "read_timeout_s", "page_size",
                  "verify_tls", "display_name"):
        if field in changes and changes[field] is not None:
            changes[field] = changes[field]
    if "endpoint" in changes and changes["endpoint"]:
        endpoint = changes["endpoint"]
        if not endpoint.startswith(("https://", "http://")):
            raise HTTPException(
                status_code=422, detail="Endpoint must be an http(s) URL"
            )
        if endpoint.startswith("http://") and payload.verify_tls is not False:
            raise HTTPException(
                status_code=422,
                detail="Plain HTTP endpoints require explicit TLS verification opt-out",
            )
    if changes.get("secret_ref"):
        # The API accepts a *reference* only. A pasted credential is rejected
        # so that secrets never enter the database or the audit trail.
        try:
            changes["secret_ref"] = validate_reference(changes["secret_ref"])
        except SecretError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if changes.get("verify_tls") is False:
        # Allowed only for explicitly approved internal endpoints; recorded.
        record_audit(
            event_type="integration.tls_verification_disabled", outcome="success",
            principal=principal, target_type="integration", target_id=key,
            context={"endpoint": changes.get("endpoint", "")},
            source_ip=_client_ip(request), service=APP,
        )
    if "extra" in changes and changes["extra"] is not None:
        changes["extra"] = json.dumps(changes["extra"])
    assignments = []
    params: dict[str, Any] = {"key": key, "by": principal.username}
    for field, value in changes.items():
        if field == "extra":
            assignments.append("extra = CAST(:extra AS jsonb)")
        else:
            assignments.append(f"{field} = :{field}")
        params[field] = value
    row = execute(
        f"UPDATE inventory.integrations SET {', '.join(assignments)}, "
        "updated_at = now(), updated_by = :by WHERE key = :key",
        **params,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Unknown integration")
    record_audit(
        event_type="integration.updated", outcome="success", principal=principal,
        target_type="integration", target_id=key,
        context={"fields": sorted(changes)}, source_ip=_client_ip(request),
        service=APP,
    )
    return fetch_one(
        f"SELECT {INTEGRATION_PUBLIC_COLUMNS} FROM inventory.integrations WHERE key = :k",
        k=key,
    )


class IntegrationTest(BaseModel):
    """Optional draft overrides. Absent fields mean "use the saved value".

    The credential is still a reference (``env:`` / ``file:``); a blank
    reference tests with the saved one.
    """

    endpoint: str | None = None
    auth_method: str | None = None
    secret_ref: str | None = None
    connect_timeout_s: int | None = Field(default=None, ge=1, le=300)
    read_timeout_s: int | None = Field(default=None, ge=1, le=900)
    page_size: int | None = Field(default=None, ge=1, le=1000)
    verify_tls: bool | None = None
    extra: dict[str, Any] | None = None


def _probe_integration(settings: dict[str, Any], secret: str) -> str:
    """Exercise the configured source and return a human-readable result.

    A probe is deliberately shallow: it proves the endpoint, TLS and
    credential work and that the first page has the expected shape. It never
    writes an assessment, an asset or a sync run, so a failed test leaves the
    integration's health and history untouched.
    """
    key = settings["key"]
    extra = settings.get("extra") or {}
    if isinstance(extra, str):
        extra = json.loads(extra)
    verify_tls = bool(settings.get("verify_tls", True))

    if key == "assetexplorer":
        with AssetExplorerClient(
            settings["endpoint"],
            auth_method=settings.get("auth_method", "integration_key"),
            secret=secret,
            timeout_s=float(settings.get("connect_timeout_s") or 30),
            read_timeout_s=float(settings.get("read_timeout_s") or 60),
            page_size=int(settings.get("page_size") or 200),
            verify_tls=verify_tls,
            field_map=extra.get("field_map"),
            api_path=extra.get("api_path", "/api/v3/asset_servers"),
        ) as client:
            return client.probe()
    if key == "satellite":
        with SatelliteClient(
            settings["endpoint"],
            username=extra.get("username", ""),
            password=secret,
            timeout_s=float(settings.get("read_timeout_s") or 60),
            page_size=int(settings.get("page_size") or 100),
            verify_tls=verify_tls,
            host_search=str(extra.get("host_search") or ""),
        ) as client:
            return client.probe()
    if key == "sccm":
        with SccmClient(
            settings["endpoint"],
            token=secret,
            timeout_s=float(settings.get("read_timeout_s") or 60),
            page_size=int(settings.get("page_size") or 200),
            verify_tls=verify_tls,
            api_path=str(extra.get("api_path") or "/SMS_REST/DeviceCompliance"),
        ) as client:
            return client.probe()
    if key == "solaris":
        detail = _probe_solaris_targets(extra)
        if detail:
            return detail
    inbox = Path(extra.get("inbox") or "/var/lib/patchtrack/solaris-inbox")
    return probe_inbox(inbox)


def _probe_solaris_targets(extra: dict[str, Any]) -> str | None:
    """Probe every enabled SSH target; return None when none are configured.

    A probe proves SSH, the pinned host key, the collector and — for Solaris
    10 — that a baseline is assigned. It writes no assessment.
    """
    targets = load_solaris_targets()
    if not targets:
        return None
    client = SolarisSshClient(
        targets,
        collector_command=str(
            extra.get("collector_command") or DEFAULT_COLLECTOR_COMMAND
        ),
        workers=int(extra.get("workers") or 4),
    )

    def probe(target: SolarisTarget) -> str:
        return client.probe(target, resolve_secret(target.secret_ref))

    details: list[str] = []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=min(4, len(targets))) as pool:
        futures = {pool.submit(probe, target): target for target in targets}
        for future in as_completed(futures):
            target = futures[future]
            try:
                details.append(f"{target.name}: {future.result()}")
            except (SolarisSshError, SecretError) as exc:
                failures.append(f"{target.name}: {exc}")
            except Exception as exc:  # noqa: BLE001 - one target must not hide the rest
                failures.append(f"{target.name}: {type(exc).__name__}: {exc}")
    if failures:
        raise SolarisSshError(
            f"{len(failures)} of {len(targets)} Solaris target(s) failed: "
            + "; ".join(sorted(failures)[:5])
        )
    return (
        f"all {len(targets)} Solaris target(s) OK: " + "; ".join(sorted(details))
    )


@app.post("/integrations/{key}/test")
def test_integration(
    key: str,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("integrations:manage"))],
    payload: IntegrationTest | None = None,
) -> dict[str, Any]:
    """Test an integration without collecting anything.

    The body is optional and may carry the values currently in the editor, so
    an administrator can validate a change before saving it. Nothing in the
    request is persisted. The result is audited with the failure reason (but
    never a credential).
    """
    if key not in ("assetexplorer", *PATCH_SOURCE_KEYS):
        raise HTTPException(status_code=404, detail="Unknown integration")
    stored = fetch_one("SELECT * FROM inventory.integrations WHERE key = :k", k=key)
    if stored is None:
        raise HTTPException(status_code=404, detail="Unknown integration")

    changes = payload.model_dump(exclude_unset=True) if payload else {}
    changes = {k: v for k, v in changes.items() if v is not None}
    settings = {**stored, **changes}
    draft = bool(changes)

    def failed(detail: str, started: float) -> dict[str, Any]:
        record_audit(
            event_type="integration.tested", outcome="failure", principal=principal,
            target_type="integration", target_id=key,
            context={"detail": detail[:500], "draft": draft},
            source_ip=_client_ip(request), service=APP,
        )
        return {
            "status": "failed", "detail": detail,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "tested": "draft" if draft else "saved",
        }

    started = time.perf_counter()
    endpoint = str(settings.get("endpoint") or "")
    if key != "solaris":
        if not endpoint:
            return failed("Endpoint is not configured", started)
        if not endpoint.startswith(("https://", "http://")):
            return failed("Endpoint must be an http(s) URL", started)
        if endpoint.startswith("http://") and settings.get("verify_tls") is not False:
            return failed(
                "Plain HTTP endpoints require explicit TLS verification opt-out",
                started,
            )

    reference = str(changes.get("secret_ref") or stored.get("secret_ref") or "")
    try:
        if reference:
            validate_reference(reference)
        secret = resolve_secret(reference)
    except SecretError as exc:
        return failed(f"Credential reference: {exc}", started)

    try:
        detail = _probe_integration(settings, secret)
    except (
        AssetExplorerError, SatelliteError, SccmError, SolarisIngestError,
        SolarisSshError, SecretError,
    ) as exc:
        return failed(str(exc), started)
    except Exception as exc:  # noqa: BLE001 - a probe must never 500 the screen
        return failed(f"{type(exc).__name__}: {exc}", started)

    duration_ms = round((time.perf_counter() - started) * 1000)
    record_audit(
        event_type="integration.tested", outcome="success", principal=principal,
        target_type="integration", target_id=key,
        context={"detail": detail[:500], "draft": draft},
        source_ip=_client_ip(request), service=APP,
    )
    return {
        "status": "success", "detail": detail, "duration_ms": duration_ms,
        "tested": "draft" if draft else "saved",
    }


class SolarisTargetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    hostname: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=64)
    secret_ref: str = Field(min_length=1, max_length=500)
    host_key: str = Field(default="", max_length=200)
    os_major: Literal[10, 11]
    zone: str = Field(default="global", max_length=64)
    baseline_id: UUID | None = None
    collector_command: str = Field(default="", max_length=500)
    enabled: bool = True
    connect_timeout_s: int = Field(default=15, ge=1, le=300)
    read_timeout_s: int = Field(default=120, ge=1, le=900)


class SolarisTargetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    hostname: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, min_length=1, max_length=64)
    secret_ref: str | None = Field(default=None, min_length=1, max_length=500)
    host_key: str | None = Field(default=None, max_length=200)
    os_major: Literal[10, 11] | None = None
    zone: str | None = Field(default=None, max_length=64)
    baseline_id: UUID | None = None
    collector_command: str | None = Field(default=None, max_length=500)
    enabled: bool | None = None
    connect_timeout_s: int | None = Field(default=None, ge=1, le=300)
    read_timeout_s: int | None = Field(default=None, ge=1, le=900)


class SolarisBaselineCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    os_major: Literal[10, 11]
    description: str = Field(default="", max_length=1000)
    source: str = Field(default="manual", max_length=200)
    entries: list[dict[str, Any]]


SOLARIS_TARGET_COLUMNS = """
    t.id, t.name, t.hostname, t.port, t.username, t.secret_ref, t.host_key,
    t.os_major, t.zone, t.baseline_id, t.collector_command, t.enabled,
    t.connect_timeout_s, t.read_timeout_s, t.last_success_at, t.last_error,
    t.created_at, t.updated_at
"""


def _validate_target_fields(
    changes: dict[str, Any], *, existing: dict[str, Any] | None = None
) -> None:
    if "secret_ref" in changes and changes["secret_ref"]:
        try:
            changes["secret_ref"] = validate_reference(changes["secret_ref"])
        except SecretError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if changes.get("collector_command"):
        command = changes["collector_command"]
        if not command.startswith("/") or not re.fullmatch(
            r"/[A-Za-z0-9_./@%:+=, -]*", command
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Collector command must be an absolute path without shell "
                    "metacharacters"
                ),
            )
    os_major = changes.get("os_major") or (existing or {}).get("os_major")
    if "baseline_id" in changes:
        baseline_id = changes["baseline_id"]
    else:
        baseline_id = (existing or {}).get("baseline_id")
    if baseline_id is not None:
        baseline = fetch_one(
            "SELECT id, os_major, name FROM inventory.solaris_baselines WHERE id = :id",
            id=str(baseline_id),
        )
        if baseline is None:
            raise HTTPException(status_code=422, detail="Unknown baseline")
        if os_major is not None and int(baseline["os_major"]) != int(os_major):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Baseline {baseline['name']} is for Solaris "
                    f"{baseline['os_major']}, not {os_major}"
                ),
            )


@app.get("/solaris/targets")
def list_solaris_targets(
    _: Annotated[Principal, Depends(require_permission("integrations:view"))],
) -> list[dict[str, Any]]:
    return fetch_all(
        f"""
        SELECT {SOLARIS_TARGET_COLUMNS},
               b.name AS baseline_name
        FROM inventory.solaris_targets t
        LEFT JOIN inventory.solaris_baselines b ON b.id = t.baseline_id
        ORDER BY t.name
        """
    )


@app.post("/solaris/targets", status_code=status.HTTP_201_CREATED)
def create_solaris_target(
    payload: SolarisTargetCreate,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("integrations:manage"))],
) -> dict[str, Any]:
    changes = payload.model_dump()
    _validate_target_fields(changes)
    try:
        execute(
            """
            INSERT INTO inventory.solaris_targets
                (name, hostname, port, username, secret_ref, host_key, os_major,
                 zone, baseline_id, collector_command, enabled,
                 connect_timeout_s, read_timeout_s)
            VALUES
                (:name, :hostname, :port, :username, :secret_ref, :host_key,
                 :os_major, :zone, :baseline_id, :collector_command, :enabled,
                 :connect_timeout_s, :read_timeout_s)
            """,
            **{**changes, "baseline_id": str(changes["baseline_id"])
               if changes["baseline_id"] else None},
        )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="A target with that name already exists"
        ) from exc
    row = fetch_one(
        f"SELECT {SOLARIS_TARGET_COLUMNS} FROM inventory.solaris_targets t "
        "WHERE t.name = :name", name=changes["name"],
    )
    record_audit(
        event_type="solaris.target_created", outcome="success",
        principal=principal, target_type="solaris_target",
        target_id=str(row["id"]) if row else changes["name"],
        context={"name": changes["name"], "hostname": changes["hostname"],
                 "os_major": changes["os_major"]},
        source_ip=_client_ip(request), service=APP,
    )
    return row or {}


@app.patch("/solaris/targets/{target_id}")
def update_solaris_target(
    target_id: UUID,
    payload: SolarisTargetUpdate,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("integrations:manage"))],
) -> dict[str, Any]:
    existing = fetch_one(
        f"SELECT {SOLARIS_TARGET_COLUMNS} FROM inventory.solaris_targets t "
        "WHERE t.id = :id",
        id=str(target_id),
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="Unknown Solaris target")
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No changes supplied")
    if "baseline_id" in changes and changes["baseline_id"] is not None:
        changes["baseline_id"] = str(changes["baseline_id"])
    _validate_target_fields(changes, existing=existing)
    assignments = ", ".join(f"{field} = :{field}" for field in changes)
    execute(
        f"UPDATE inventory.solaris_targets SET {assignments}, updated_at = now() "
        "WHERE id = :id",
        **changes, id=str(target_id),
    )
    record_audit(
        event_type="solaris.target_updated", outcome="success",
        principal=principal, target_type="solaris_target", target_id=str(target_id),
        context={"fields": sorted(changes)}, source_ip=_client_ip(request),
        service=APP,
    )
    return fetch_one(
        f"SELECT {SOLARIS_TARGET_COLUMNS} FROM inventory.solaris_targets t "
        "WHERE t.id = :id",
        id=str(target_id),
    ) or {}


@app.delete("/solaris/targets/{target_id}")
def delete_solaris_target(
    target_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("integrations:manage"))],
) -> dict[str, Any]:
    existing = fetch_one(
        "SELECT id, name FROM inventory.solaris_targets WHERE id = :id",
        id=str(target_id),
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="Unknown Solaris target")
    execute("DELETE FROM inventory.solaris_targets WHERE id = :id", id=str(target_id))
    record_audit(
        event_type="solaris.target_deleted", outcome="success",
        principal=principal, target_type="solaris_target", target_id=str(target_id),
        context={"name": existing["name"]}, source_ip=_client_ip(request),
        service=APP,
    )
    return {"deleted": True, "name": existing["name"]}


@app.post("/solaris/targets/{target_id}/test")
def test_solaris_target(
    target_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("integrations:manage"))],
) -> dict[str, Any]:
    started = time.perf_counter()
    target = fetch_one(
        "SELECT * FROM inventory.solaris_targets WHERE id = :id", id=str(target_id)
    )
    if target is None:
        raise HTTPException(status_code=404, detail="Unknown Solaris target")
    baseline = None
    if target["baseline_id"]:
        baseline = fetch_one(
            "SELECT id, name, entries FROM inventory.solaris_baselines WHERE id = :id",
            id=str(target["baseline_id"]),
        )
        if baseline:
            baseline = {
                "id": str(baseline["id"]), "name": baseline["name"],
                "entries": baseline["entries"],
            }
    view = SolarisTarget(
        id=str(target["id"]), name=target["name"], hostname=target["hostname"],
        username=target["username"], secret_ref=target["secret_ref"],
        os_major=int(target["os_major"]), port=int(target["port"]),
        host_key=target["host_key"] or "", zone=target["zone"] or "global",
        collector_command=target["collector_command"] or "",
        connect_timeout_s=int(target["connect_timeout_s"]),
        read_timeout_s=int(target["read_timeout_s"]), baseline=baseline,
    )
    try:
        secret = resolve_secret(target["secret_ref"])
        detail = SolarisSshClient([view]).probe(view, secret)
    except (SolarisSshError, SecretError) as exc:
        detail = str(exc)
        status_text = "failed"
    else:
        status_text = "success"
    duration_ms = round((time.perf_counter() - started) * 1000)
    record_audit(
        event_type="solaris.target_tested", outcome=status_text,
        principal=principal, target_type="solaris_target", target_id=str(target_id),
        context={"detail": detail[:500]}, source_ip=_client_ip(request), service=APP,
    )
    return {"status": status_text, "detail": detail, "duration_ms": duration_ms}


@app.get("/solaris/baselines")
def list_solaris_baselines(
    _: Annotated[Principal, Depends(require_permission("integrations:view"))],
) -> list[dict[str, Any]]:
    return fetch_all(
        "SELECT id, name, os_major, description, source, entries, created_by, "
        "created_at, updated_at FROM inventory.solaris_baselines ORDER BY name"
    )


@app.post("/solaris/baselines", status_code=status.HTTP_201_CREATED)
def create_solaris_baseline(
    payload: SolarisBaselineCreate,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("integrations:manage"))],
) -> dict[str, Any]:
    try:
        entries = validate_baseline_entries(payload.entries)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        execute(
            """
            INSERT INTO inventory.solaris_baselines
                (name, os_major, description, source, entries, created_by)
            VALUES (:name, :os_major, :description, :source,
                    CAST(:entries AS jsonb), :by)
            """,
            name=payload.name, os_major=payload.os_major,
            description=payload.description, source=payload.source,
            entries=json.dumps(entries), by=principal.username,
        )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="A baseline with that name already exists"
        ) from exc
    row = fetch_one(
        "SELECT id, name, os_major, description, source, entries, created_by, "
        "created_at, updated_at FROM inventory.solaris_baselines WHERE name = :n",
        n=payload.name,
    )
    record_audit(
        event_type="solaris.baseline_created", outcome="success",
        principal=principal, target_type="solaris_baseline",
        target_id=str(row["id"]) if row else payload.name,
        context={"name": payload.name, "os_major": payload.os_major,
                 "entries": len(entries)},
        source_ip=_client_ip(request), service=APP,
    )
    return row or {}


class TriggerSync(BaseModel):
    integration: str


@app.post("/sync")
def trigger_sync(
    payload: TriggerSync,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("sync:run"))],
) -> dict[str, Any]:
    key = payload.integration
    if key != "assetexplorer" and key not in PATCH_SOURCE_KEYS:
        raise HTTPException(status_code=404, detail="Unknown integration")
    try:
        return collect_integration(key, principal, source_ip=_client_ip(request))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def collect_integration(
    key: str,
    principal: Principal,
    *,
    trigger: str = "manual",
    source_ip: str | None = None,
) -> dict[str, Any]:
    """Collect one integration. Shared by the HTTP route and the scheduler.

    A concurrent run raises ``RuntimeError``; the route turns that into 409
    and the scheduler records a failed attempt and retries later.
    """
    try:
        result = (
            run_assetexplorer_sync(trigger=trigger, requested_by=principal.username)
            if key == "assetexplorer"
            else run_patch_sync(key, trigger=trigger, requested_by=principal.username)
        )
    except RuntimeError as exc:
        record_audit(
            event_type="sync.requested", outcome="failure", principal=principal,
            target_type="integration", target_id=key,
            context={"error": str(exc), "trigger": trigger},
            source_ip=source_ip, service=APP,
        )
        raise
    record_audit(
        event_type="sync.requested",
        outcome="success" if result.get("status") != "failed" else "failure",
        principal=principal, target_type="integration", target_id=key,
        context={"result": result.get("status"), "run_id": result.get("run_id"),
                 "trigger": trigger},
        source_ip=source_ip, service=APP,
    )
    return result


@app.get("/sync-runs")
def sync_runs(
    _: Annotated[Principal, Depends(require_permission("integrations:view"))],
    integration: str | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[dict[str, Any]]:
    if integration:
        return fetch_all(
            "SELECT * FROM inventory.sync_runs WHERE integration_key = :k "
            "ORDER BY started_at DESC LIMIT :l", k=integration, l=limit,
        )
    return fetch_all(
        "SELECT * FROM inventory.sync_runs ORDER BY started_at DESC LIMIT :l", l=limit
    )


# --------------------------------------------------------------------------- conflicts
@app.get("/reconciliation")
def reconciliation(
    _: Annotated[Principal, Depends(require_permission("inventory:view"))],
    open_only: bool = True,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    where = "WHERE resolved_at IS NULL" if open_only else ""
    items = fetch_all(
        f"""
        SELECT rc.*, a.hostname AS asset_hostname
        FROM inventory.reconciliation_conflicts rc
        LEFT JOIN inventory.assets a ON a.id = rc.asset_id
        {where} ORDER BY rc.detected_at DESC LIMIT :l
        """,
        l=limit,
    )
    summary = fetch_all(
        "SELECT source_system, reason, count(*) AS n "
        "FROM inventory.reconciliation_conflicts WHERE resolved_at IS NULL "
        "GROUP BY 1, 2 ORDER BY n DESC"
    )
    return {"items": items, "summary": summary}


class ResolveConflict(BaseModel):
    asset_id: UUID | None = None
    resolution: str = Field(min_length=3, max_length=500)


@app.post("/reconciliation/{conflict_id}/resolve")
def resolve(
    conflict_id: int,
    payload: ResolveConflict,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("integrations:manage"))],
) -> dict[str, str]:
    with connection() as conn:
        ok = resolve_conflict(
            conn, conflict_id,
            str(payload.asset_id) if payload.asset_id else None,
            principal.username, payload.resolution,
        )
    if not ok:
        raise HTTPException(status_code=404, detail="Conflict not found or already resolved")
    record_audit(
        event_type="inventory.reconciliation_resolved", outcome="success",
        principal=principal, target_type="reconciliation_conflict",
        target_id=str(conflict_id),
        context={"asset_id": str(payload.asset_id) if payload.asset_id else None},
        source_ip=_client_ip(request), service=APP,
    )
    return {"status": "resolved"}


# --------------------------------------------------------------------------- in-app scheduler
_settings = get_settings()
install_scheduler(app, [
    ScheduledJob(
        name="inventory.assetexplorer",
        interval_seconds=_settings.sync_assetexplorer_interval,
        run=lambda: collect_integration(
            "assetexplorer", SCHEDULER_PRINCIPAL, trigger="schedule"
        ),
    ),
    *[
        ScheduledJob(
            name=f"inventory.{key}",
            interval_seconds=getattr(_settings, f"sync_{key}_interval"),
            run=lambda key=key: collect_integration(
                key, SCHEDULER_PRINCIPAL, trigger="schedule"
            ),
        )
        for key in PATCH_SOURCE_KEYS
    ],
])
