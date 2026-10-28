"""PatchTrack inventory service.

Owns: AssetExplorer inventory synchronisation, patch-source collection,
asset/source reconciliation, integration configuration and health.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from patchtrack_common.audit import record_audit
from patchtrack_common.db import connection, execute, fetch_all, fetch_one
from patchtrack_common.net import client_ip
from patchtrack_common.secrets import SecretError, validate_reference
from patchtrack_common.security import Principal, PrincipalDep, require_permission

from .sync import PATCH_SOURCE_KEYS, resolve_conflict, run_assetexplorer_sync, run_patch_sync

APP = "inventory"
app = FastAPI(title="PatchTrack Inventory", version="1.0.0", docs_url="/docs")

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
    total = fetch_one(
        f"SELECT count(*) AS n FROM inventory.asset_effective a "
        f"LEFT JOIN compliance.asset_compliance c ON c.asset_id = a.id WHERE {where}",
        **params,
    )
    rows = fetch_all(
        f"{ASSET_BASE_SQL} WHERE {where} ORDER BY {sort_column} {order}, a.id "
        "LIMIT :limit OFFSET :offset",
        **params, limit=page_size, offset=(page - 1) * page_size,
    )
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
    return {
        "items": rows,
        "total": total["n"] if total else 0,
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
    findings = fetch_all(
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
        ORDER BY assessed_at DESC LIMIT 30
        """,
        id=asset_id,
    )
    return {
        "asset": asset,
        "assessment": assessment,
        "findings": findings,
        "source_links": links,
        "assessment_history": history,
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


@app.post("/assets/{asset_id}/exclude")
def exclude_asset(
    asset_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("policies:manage"))],
    reason: str = Query(min_length=5, max_length=500),
) -> dict[str, str]:
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
    reason = payload.reason.strip()

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
class OrganizationSetting(BaseModel):
    name: str = Field(min_length=1, max_length=200)


@app.get("/settings/organization")
def get_organization(
    _: Annotated[Principal, Depends(require_permission("inventory:view"))],
) -> dict[str, Any]:
    row = fetch_one(
        "SELECT value, updated_at, updated_by FROM shared.app_settings "
        "WHERE key = 'organization'"
    )
    if row is None:
        return {"name": "", "updated_at": None, "updated_by": None}
    value = row["value"]
    if isinstance(value, str):
        value = json.loads(value)
    return {
        "name": value.get("name", ""),
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
    }


@app.put("/settings/organization")
def set_organization(
    payload: OrganizationSetting,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("settings:manage"))],
) -> dict[str, Any]:
    """Branding used on generated PDFs. Survives restarts (stored in the DB)."""
    value = json.dumps({"name": payload.name.strip()})
    execute(
        """
        INSERT INTO shared.app_settings (key, value, updated_at, updated_by)
        VALUES ('organization', CAST(:v AS jsonb), now(), :by)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
            updated_at = now(), updated_by = EXCLUDED.updated_by
        """,
        v=value, by=principal.username,
    )
    record_audit(
        event_type="settings.updated", outcome="success", principal=principal,
        target_type="setting", target_id="organization",
        context={"name": payload.name}, source_ip=_client_ip(request), service=APP,
    )
    return {"name": payload.name}


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
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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
                # reader (inventory, compliance, reports, Grafana) shows the
                # new name without the source record being rewritten.
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
    for row in rows:
        last = fetch_one(
            """
            SELECT status, started_at, finished_at, records_seen, records_created,
                   records_updated, records_rejected, assets_unmatched, error_summary,
                   EXTRACT(EPOCH FROM (finished_at - started_at)) AS duration_s
            FROM inventory.sync_runs WHERE integration_key = :k
            ORDER BY started_at DESC LIMIT 1
            """,
            k=row["key"],
        )
        success = fetch_one(
            "SELECT max(finished_at) AS at FROM inventory.sync_runs "
            "WHERE integration_key = :k AND status = 'success'",
            k=row["key"],
        )
        row["last_run"] = last
        row["last_success_at"] = success["at"] if success else None
        row["health"] = _health(row, last)
    return rows


def _health(integration: dict[str, Any], last: dict[str, Any] | None) -> str:
    if not integration["enabled"]:
        return "not_configured" if not integration["endpoint"] else "disabled"
    if not integration["endpoint"]:
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
        result = (
            run_assetexplorer_sync(trigger="manual", requested_by=principal.username)
            if key == "assetexplorer"
            else run_patch_sync(key, trigger="manual", requested_by=principal.username)
        )
    except RuntimeError as exc:
        record_audit(
            event_type="sync.requested", outcome="failure", principal=principal,
            target_type="integration", target_id=key,
            context={"error": str(exc)}, source_ip=_client_ip(request), service=APP,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    record_audit(
        event_type="sync.requested",
        outcome="success" if result.get("status") != "failed" else "failure",
        principal=principal, target_type="integration", target_id=key,
        context={"result": result.get("status"), "run_id": result.get("run_id")},
        source_ip=_client_ip(request), service=APP,
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
