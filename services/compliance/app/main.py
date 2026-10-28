"""PatchTrack compliance service.

Owns: compliance policy versions, evaluation, dashboards, historical
snapshots, remediation visibility and Solaris ingestion.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from patchtrack_common.audit import record_audit
from patchtrack_common.db import connection, execute, fetch_all, fetch_one
from patchtrack_common.net import client_ip
from patchtrack_common.security import Principal, PrincipalDep, require_permission

from .engine import (
    DIMENSIONS,
    aggregate,
    evaluate_asset,
    group_breakdown,
    parse_period_key,
    period_bounds,
    period_key,
    period_label,
    severity_breakdown,
)
from .evaluator import (
    active_policy,
    create_snapshot,
    ensure_daily_snapshot,
    evaluate_all,
    load_assets,
    policy_version_for,
    refresh_compliance,
)

APP = "compliance"
app = FastAPI(title="PatchTrack Compliance", version="1.0.0", docs_url="/docs")

FILTER_FIELDS = {
    "os_family": "a.os_family",
    "os_version": "a.os_version",
    "environment": "a.environment",
    "business_group": "a.business_group",
    "application_group": "a.application_group",
    "patch_source": "a.patch_source",
}


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


class FilterParams(BaseModel):
    os_family: str | None = None
    os_version: str | None = None
    environment: str | None = None
    business_group: str | None = None
    application_group: str | None = None
    patch_source: str | None = None

    def applied(self) -> dict[str, str]:
        return {k: v for k, v in self.model_dump().items() if v}


def _selected(evaluations, assets, filters: dict[str, str]):
    chosen_assets = {
        asset_id: asset for asset_id, asset in assets.items()
        if all(
            str(getattr(asset, key, "")).lower() == value.lower()
            for key, value in filters.items()
        )
    }
    selected = {
        asset_id: evaluation for asset_id, evaluation in evaluations.items()
        if asset_id in chosen_assets
    }
    return chosen_assets, selected


# --------------------------------------------------------------------------- summary
@app.get("/summary")
def summary(
    principal: Annotated[Principal, Depends(require_permission("dashboard:view"))],
    os_family: str | None = None,
    os_version: str | None = None,
    environment: str | None = None,
    business_group: str | None = None,
    application_group: str | None = None,
    patch_source: str | None = None,
    refresh: bool = False,
    period: str | None = None,
) -> dict[str, Any]:
    """The executive view, live or for one past calendar period.

    With ``period`` the response is built from the immutable daily snapshot
    captured inside that month, quarter or year — never from a re-evaluation of
    today's estate wearing a past label. A snapshot stores group totals, not
    server rows, so a dimension filter cannot be applied to it; asking for both
    is refused rather than silently ignored.
    """
    filters = FilterParams(
        os_family=os_family, os_version=os_version, environment=environment,
        business_group=business_group, application_group=application_group,
        patch_source=patch_source,
    ).applied()
    if period is not None:
        if filters:
            raise HTTPException(
                status_code=400,
                detail=(
                    "A historical period cannot be combined with dimension "
                    "filters; clear the filters or choose Live"
                ),
            )
        return _period_summary(period)
    if refresh:
        refresh_compliance()
    assets, evaluations, policy, version_id = evaluate_all()
    selected_assets, selected = _selected(evaluations, assets, filters)
    pop = aggregate(list(selected.values()), target_pct=policy.get("target_pct"))
    freshness = _freshness()
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "live",
        "period": None,
        "filters": filters,
        "policy": {
            "name": policy.get("name"),
            "version_id": version_id,
            "target_pct": policy.get("target_pct"),
            "quarterly_target_pct": policy.get("quarterly_target_pct"),
            "freshness_hours": policy.get("freshness_hours"),
        },
        "kpis": pop.as_dict(),
        "freshness": freshness,
        # Operating-system family, not version: the panel answers "which
        # platform is behind", and a version (9.4, 9.5, 2022…) is a different
        # question that is already answered by the OS filter.
        "by_os": group_breakdown(list(selected.values()), selected_assets, "os_family"),
        "by_environment": group_breakdown(
            list(selected.values()), selected_assets, "environment"
        ),
        "by_business_group": group_breakdown(
            list(selected.values()), selected_assets, "business_group"
        ),
        "by_application_group": group_breakdown(
            list(selected.values()), selected_assets, "application_group"
        ),
        "severity": severity_breakdown(list(selected.values())),
    }


def _snapshot_group_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shape snapshot group rows the way the live summary shapes its panels.

    ``snapshot_groups`` has no ``excluded`` column: group rows already carry
    the four judged states, so the in-scope denominator is their sum. Nothing
    is inferred beyond that.
    """
    shaped = []
    for row in rows:
        in_scope = (
            row["compliant"] + row["non_compliant"]
            + row["unknown"] + row["stale"]
        )
        shaped.append({
            "group": row["group_key"],
            "total_servers": row["total_servers"],
            "compliant": row["compliant"],
            "non_compliant": row["non_compliant"],
            "unknown": row["unknown"],
            "stale": row["stale"],
            "in_scope": in_scope,
            "total_findings": row["finding_count"],
        })
    shaped.sort(key=lambda r: (-r["total_servers"], r["group"]))
    return shaped


def _period_summary(value: str) -> dict[str, Any]:
    """The executive view as it was recorded in one calendar period.

    One immutable daily snapshot is used: the latest captured inside the
    period. Its identity travels with the response so a reader can open the
    snapshot ledger and see exactly which record the panels came from. Panels
    whose dimension was not recorded on that snapshot (older snapshots have no
    ``os_family`` rows) come back empty and are listed in ``dimensions``; they
    are never guessed from the current estate.
    """
    try:
        kind, start, end = parse_period_key(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    label = period_label(kind, start)
    window = {"start": start, "end": end + timedelta(days=1)}
    snapshot = fetch_one(
        """
        SELECT * FROM compliance.snapshots
        WHERE scope = 'daily' AND period_start >= :start AND period_start < :end
        ORDER BY period_start DESC, captured_at DESC LIMIT 1
        """,
        **window,
    )
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail=f"No daily snapshot was recorded in {label}",
        )
    sample = fetch_one(
        """
        SELECT count(*)::int AS samples FROM compliance.snapshots
        WHERE scope = 'daily' AND period_start >= :start AND period_start < :end
        """,
        **window,
    )
    groups = fetch_all(
        """
        SELECT dimension, group_key, total_servers, compliant, non_compliant,
               unknown, stale, finding_count
        FROM compliance.snapshot_groups
        WHERE snapshot_id = :id ORDER BY dimension, total_servers DESC
        """,
        id=snapshot["id"],
    )
    by_dimension: dict[str, list[dict[str, Any]]] = {}
    for group in groups:
        by_dimension.setdefault(group["dimension"], []).append(group)

    def panel(dimension: str) -> list[dict[str, Any]]:
        return _snapshot_group_rows(by_dimension.get(dimension, []))

    severity_order = ("critical", "important", "moderate", "low", "unknown")
    severity = [
        {"severity": group["group_key"], "count": group["finding_count"]}
        for group in sorted(
            by_dimension.get("severity", []),
            key=lambda g: severity_order.index(g["group_key"])
            if g["group_key"] in severity_order else len(severity_order),
        )
    ]

    total = snapshot["total_servers"] or 0
    excluded = snapshot["excluded_servers"] or 0
    compliance_pct = snapshot["compliance_pct"]
    target_pct = snapshot["target_pct"]
    compliance_pct = float(compliance_pct) if compliance_pct is not None else None
    target_pct = float(target_pct) if target_pct is not None else None
    in_scope = total - excluded
    gap_pct = (
        round(compliance_pct - target_pct, 2)
        if compliance_pct is not None and target_pct is not None else None
    )
    methodology = snapshot["methodology"] or {}
    if not isinstance(methodology, dict):
        methodology = {}

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "period",
        "filters": {},
        "policy": {
            "name": methodology.get("policy_name"),
            "version_id": snapshot["policy_version_id"],
            "target_pct": target_pct,
            "quarterly_target_pct": methodology.get("quarterly_target_pct"),
            "freshness_hours": methodology.get("freshness_hours"),
        },
        "kpis": {
            "total_servers": total,
            "compliant": snapshot["compliant_servers"],
            "non_compliant": snapshot["non_compliant_servers"],
            "unknown": snapshot["unknown_servers"],
            "stale": snapshot["stale_servers"],
            "excluded": excluded,
            "in_scope": in_scope,
            "compliance_pct": compliance_pct,
            "target_pct": target_pct,
            "gap_pct": gap_pct,
            "critical_findings": snapshot["critical_findings"],
            "total_findings": snapshot["total_findings"],
        },
        # The freshness of today's collection says nothing about a frozen
        # period, so it is not attached to this response.
        "freshness": None,
        "period": {
            "key": value,
            "kind": kind,
            "label": label,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "snapshot_id": str(snapshot["id"]),
            "scope": snapshot["scope"],
            "captured_at": snapshot["captured_at"],
            "samples": sample["samples"] if sample else 1,
            "policy_version_id": snapshot["policy_version_id"],
        },
        "by_os": panel("os_family"),
        "by_environment": panel("environment"),
        "by_business_group": panel("business_group"),
        "by_application_group": panel("application_group"),
        "severity": severity,
        "dimensions": sorted(by_dimension),
    }


@app.get("/snapshot-periods")
def snapshot_periods(
    _: Annotated[Principal, Depends(require_permission("dashboard:view"))],
) -> dict[str, Any]:
    """The periods a reader can choose, newest first, from recorded snapshots.

    Only periods with at least one daily snapshot appear: an empty month is
    not offered, so selecting one cannot produce a misleading blank dashboard.
    """
    # Five years of history is the documented retention window for the
    # dashboard, so the picker must reach back that far for months and
    # quarters too; stopping at two years made older recorded periods
    # unreachable even though the trend still averaged them.
    limits = {"month": 60, "quarter": 20, "year": 5}
    periods: list[dict[str, Any]] = []
    for kind in ("month", "quarter", "year"):
        rows = fetch_all(
            """
            SELECT date_trunc(:unit, period_start)::date AS period_start,
                   count(*)::int AS samples,
                   max(captured_at) AS as_of
            FROM compliance.snapshots
            WHERE scope = 'daily'
            GROUP BY 1 ORDER BY 1 DESC LIMIT :limit
            """,
            unit=kind, limit=limits[kind],
        )
        for row in rows:
            start = row["period_start"]
            key = period_key(kind, start)
            _, _, end = parse_period_key(key)
            periods.append({
                "kind": kind,
                "key": key,
                "label": period_label(kind, start),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "samples": row["samples"],
                "as_of": row["as_of"],
            })
    return {"periods": periods}


def _freshness() -> dict[str, Any]:
    inventory = fetch_one(
        """
        SELECT max(finished_at) AS at FROM inventory.sync_runs
        WHERE integration_key = 'assetexplorer' AND status IN ('success', 'partial')
        """
    )
    patch = fetch_one(
        """
        SELECT max(finished_at) AS at FROM inventory.sync_runs
        WHERE integration_key IN ('satellite', 'sccm', 'solaris')
          AND status IN ('success', 'partial')
        """
    )
    per_source = fetch_all(
        """
        SELECT integration_key, max(finished_at) AS at FROM inventory.sync_runs
        WHERE status IN ('success', 'partial') GROUP BY integration_key
        """
    )
    return {
        "inventory_synced_at": inventory["at"] if inventory else None,
        "patch_synced_at": patch["at"] if patch else None,
        "per_source": {r["integration_key"]: r["at"] for r in per_source},
    }


# period name -> (date_trunc unit, default points, human noun)
TREND_PERIODS = {
    "monthly": ("month", 12, "months"),
    "quarterly": ("quarter", 8, "quarters"),
    "yearly": ("year", 5, "years"),
}


def _truncate(when: datetime, unit: str) -> datetime:
    """Start of the calendar period containing ``when``, in UTC."""
    if unit == "month":
        return when.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if unit == "quarter":
        month = ((when.month - 1) // 3) * 3 + 1
        return when.replace(month=month, day=1, hour=0, minute=0,
                            second=0, microsecond=0)
    return when.replace(month=1, day=1, hour=0, minute=0, second=0,
                        microsecond=0)


def _shift_back(when: datetime, unit: str, steps: int) -> datetime:
    """Start of the period ``steps`` periods before ``when``."""
    size = {"month": 1, "quarter": 3, "year": 12}[unit]
    total = when.year * 12 + (when.month - 1) - size * steps
    year, month = divmod(total, 12)
    return when.replace(year=year, month=month + 1, day=1)


def _trend_label(unit: str, start) -> str:
    if unit == "month":
        return start.strftime("%b %y")
    if unit == "quarter":
        return f"Q{(start.month - 1) // 3 + 1} {start.year}"
    return str(start.year)


@app.get("/trends")
def trends(
    _: Annotated[Principal, Depends(require_permission("dashboard:view"))],
    months: int = Query(6, ge=1, le=36),
    period: Literal["monthly", "quarterly", "yearly"] = "monthly",
    points: int | None = Query(None, ge=2, le=60),
) -> dict[str, Any]:
    """Compliance history in the requested calendar basis.

    ``series`` is the basis-independent result the dashboard uses: one point
    per month, quarter or year, each the average of the daily snapshots
    recorded inside it. Averaging recorded observations is an aggregation,
    not an estimate: a period with no snapshot simply has no point, and the
    sample count travels with every point so the UI can say how thin it is.

    ``monthly`` and ``quarterly`` remain for the history page, which shows
    the fixed monthly roll-up and the immutable quarterly records side by
    side.
    """
    unit, default_points, noun = TREND_PERIODS[period]
    count = points or default_points
    cutoff = _shift_back(_truncate(datetime.now(UTC), unit), unit, count - 1)
    series_rows = fetch_all(
        """
        SELECT date_trunc(:unit, period_start)::date AS period_start,
               avg(compliance_pct)::numeric(6,2) AS compliance_pct,
               avg(target_pct)::numeric(6,2) AS target_pct,
               round(avg(total_servers))::int AS total_servers,
               round(avg(compliant_servers))::int AS compliant_servers,
               round(avg(non_compliant_servers))::int AS non_compliant_servers,
               round(avg(unknown_servers))::int AS unknown_servers,
               round(avg(stale_servers))::int AS stale_servers,
               count(*)::int AS samples
        FROM compliance.snapshots
        WHERE scope = 'daily' AND period_start >= :cutoff
        GROUP BY 1 ORDER BY 1
        """,
        unit=unit, cutoff=cutoff.date(),
    )
    series = [
        {**row, "label": _trend_label(unit, row["period_start"])}
        for row in series_rows
    ]

    rows = fetch_all(
        """
        SELECT date_trunc('month', period_start)::date AS month,
               avg(compliance_pct)::numeric(6,2) AS compliance_pct,
               avg(target_pct)::numeric(6,2) AS target_pct,
               max(compliant_servers) AS compliant,
               sum(total_servers) AS observations,
               count(*) AS samples
        FROM compliance.snapshots
        WHERE scope = 'daily'
          AND period_start >= (date_trunc('month', now()) - make_interval(months => :m))::date
        GROUP BY 1 ORDER BY 1
        """,
        m=months,
    )
    quarterly = fetch_all(
        """
        SELECT period_start, period_end, compliance_pct, target_pct,
               total_servers, compliant_servers, captured_at
        FROM compliance.snapshots
        WHERE scope = 'quarterly'
        ORDER BY period_start DESC LIMIT 8
        """
    )
    return {
        "monthly": rows,
        "quarterly": quarterly,
        "period": period,
        "period_noun": noun,
        "points": count,
        "series": series,
    }


@app.get("/breakdown/{dimension}")
def breakdown(
    dimension: str,
    _: Annotated[Principal, Depends(require_permission("dashboard:view"))],
) -> list[dict[str, Any]]:
    if dimension not in DIMENSIONS:
        raise HTTPException(status_code=404, detail="Unknown dimension")
    _, evaluations, _, _ = evaluate_all()
    assets = load_assets()
    return group_breakdown(list(evaluations.values()), assets, dimension)


# --------------------------------------------------------------------------- server lists
# The console may re-order the server list, but only by these known columns:
# the sort name is a key into this table, never a fragment of SQL from the
# request. A new sortable column is added here and nowhere else.
SERVER_SORT_COLUMNS = {
    "hostname": "a.hostname",
    "ip_address": "a.ip_address",
    "os_name": "a.os_name",
    "os_version": "a.os_version",
    "environment": "a.environment",
    "business_group": "a.business_group",
    "state": "c.state",
    "critical_count": "c.critical_count",
    "total_patches": "c.total_patches",
    "last_assessed_at": "asmt.assessed_at",
}


def server_order_clause(sort: str, direction: str) -> str:
    """Build the ORDER BY tail, or refuse an unknown field.

    Unknown rows (no assessment) sort last in both directions: "never assessed"
    is not the same as "assessed earliest".
    """
    column = SERVER_SORT_COLUMNS.get(sort)
    if column is None:
        raise HTTPException(status_code=422, detail="Unknown sort field")
    order = "DESC" if direction == "desc" else "ASC"
    return f"{column} {order} NULLS LAST, a.hostname, a.id"


@app.get("/servers")
def servers(
    _: Annotated[Principal, Depends(require_permission("compliance:view"))],
    state: str | None = None,
    search: str | None = None,
    os_family: str | None = None,
    os_version: str | None = None,
    business_group: str | None = None,
    application_group: str | None = None,
    environment: str | None = None,
    patch_source: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    sort: str = "total_patches",
    direction: Literal["asc", "desc"] = "desc",
) -> dict[str, Any]:
    clauses = ["1=1"]
    params: dict[str, Any] = {}
    for column, value in (
        ("os_family", os_family), ("os_version", os_version),
        ("business_group", business_group),
        ("application_group", application_group),
        ("environment", environment), ("patch_source", patch_source),
    ):
        if value:
            clauses.append(f"a.{column} = :{column}")
            params[column] = value
    if search:
        # The console's search box covers what the table shows, so one term can
        # match any visible column rather than only the hostname. Compliance
        # state matches as a whole term: "Compliant" must not also return every
        # "Non-compliant" row.
        clauses.append(
            "(a.hostname ILIKE :search OR a.fqdn ILIKE :search "
            "OR host(a.ip_address) LIKE :search "
            "OR a.os_name ILIKE :search OR a.os_version ILIKE :search "
            "OR a.environment ILIKE :search OR a.business_group ILIKE :search "
            "OR a.application_group ILIKE :search OR a.patch_source ILIKE :search "
            "OR c.state = :state_search OR c.reason ILIKE :search)"
        )
        params["search"] = f"%{search}%"
        params["state_search"] = search.strip().lower().replace(" ", "_").replace("-", "_")
    if state:
        if state not in ("compliant", "non_compliant", "unknown", "stale", "excluded"):
            raise HTTPException(status_code=422, detail="Unknown compliance state")
        if state == "unknown":
            clauses.append("(c.state = 'unknown' OR c.state IS NULL)")
        else:
            clauses.append("c.state = :state")
            params["state"] = state
    where = " AND ".join(clauses)
    order_by = server_order_clause(sort, direction)
    total = fetch_one(
        f"SELECT count(*) AS n FROM inventory.asset_effective a "
        f"LEFT JOIN compliance.asset_compliance c ON c.asset_id = a.id WHERE {where}",
        **params,
    )
    rows = fetch_all(
        f"""
        SELECT a.id, a.hostname, host(a.ip_address) AS ip_address, a.os_name,
               a.os_version, a.environment, a.business_group, a.patch_source,
               c.state, c.reason, c.critical_count, c.total_patches,
               asmt.assessed_at AS last_assessed_at
        FROM inventory.asset_effective a
        LEFT JOIN compliance.asset_compliance c ON c.asset_id = a.id
        LEFT JOIN compliance.asset_latest_assessment la ON la.asset_id = a.id
        LEFT JOIN compliance.assessments asmt ON asmt.id = la.assessment_id
        WHERE {where}
        ORDER BY {order_by}
        LIMIT :limit OFFSET :offset
        """,
        **params, limit=page_size, offset=(page - 1) * page_size,
    )
    return {"items": rows, "total": total["n"] if total else 0,
            "page": page, "page_size": page_size}


@app.get("/hierarchy")
def hierarchy(
    _: Annotated[Principal, Depends(require_permission("dashboard:view"))],
) -> list[dict[str, Any]]:
    """Business group → application group → environment roll-up."""
    rows = fetch_all(
        """
        SELECT a.business_group, a.application_group, a.environment,
               count(*) AS total,
               count(*) FILTER (WHERE c.state = 'compliant') AS compliant,
               count(*) FILTER (WHERE c.state = 'non_compliant') AS non_compliant,
               count(*) FILTER (WHERE c.state IN ('unknown', 'stale')
                                 OR c.state IS NULL) AS unknown
        FROM inventory.asset_effective a
        LEFT JOIN compliance.asset_compliance c ON c.asset_id = a.id
        WHERE a.in_scope
        GROUP BY 1, 2, 3
        ORDER BY 1, 2, 3
        """
    )
    return rows


# --------------------------------------------------------------------------- findings
@app.get("/findings")
def findings(
    _: Annotated[Principal, Depends(require_permission("findings:view"))],
    severity: str | None = None,
    source: str | None = None,
    business_group: str | None = None,
    overdue: bool | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    clauses = ["1=1"]
    params: dict[str, Any] = {}
    if severity:
        clauses.append("f.severity = :severity")
        params["severity"] = severity
    if source:
        clauses.append("f.source_system = :source")
        params["source"] = source
    if business_group:
        clauses.append("a.business_group = :bg")
        params["bg"] = business_group
    if overdue:
        clauses.append("f.released_at IS NOT NULL AND f.released_at + "
                       "interval '30 days' < now()")
    where = " AND ".join(clauses)
    total = fetch_one(
        f"SELECT count(*) AS n FROM compliance.findings f "
        f"JOIN inventory.asset_effective a ON a.id = f.asset_id WHERE {where}", **params
    )
    rows = fetch_all(
        f"""
        SELECT f.id, f.advisory_id, f.title, f.severity, f.category,
               f.source_system, f.released_at, f.remediation_due,
               a.id AS asset_id, a.hostname, a.business_group, a.environment
        FROM compliance.findings f
        JOIN inventory.asset_effective a ON a.id = f.asset_id
        JOIN compliance.asset_latest_assessment la ON la.assessment_id = f.assessment_id
        WHERE {where}
        ORDER BY CASE f.severity WHEN 'critical' THEN 0 WHEN 'important' THEN 1
                 WHEN 'moderate' THEN 2 WHEN 'low' THEN 3 ELSE 4 END,
                 f.released_at NULLS LAST
        LIMIT :limit OFFSET :offset
        """,
        **params, limit=page_size, offset=(page - 1) * page_size,
    )
    return {"items": rows, "total": total["n"] if total else 0,
            "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- policies
class PolicyRule(BaseModel):
    match: dict[str, str] = Field(default_factory=dict)
    blocking_severities: list[str] = Field(default_factory=list)
    remediation_days: dict[str, int] = Field(default_factory=dict)


class PolicyDefinition(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    target_pct: float = Field(ge=0, le=100)
    quarterly_target_pct: float = Field(ge=0, le=100)
    freshness_hours: int = Field(ge=1, le=8760)
    max_blocking_findings: int = Field(default=0, ge=0, le=10000)
    blocking_severities: list[str] = Field(
        default_factory=lambda: ["critical", "important"]
    )
    remediation_days: dict[str, int] = Field(
        default_factory=lambda: {"critical": 7, "important": 30,
                                 "moderate": 90, "low": 180}
    )
    scope_rules: list[PolicyRule] = Field(default_factory=list)
    excluded_environments: list[str] = Field(default_factory=list)
    change_note: str = Field(default="", max_length=500)

    @field_validator("blocking_severities")
    @classmethod
    def _valid_severity(cls, value: list[str]) -> list[str]:
        allowed = {"critical", "important", "moderate", "low"}
        if not set(value) <= allowed:
            raise ValueError("blocking_severities must be a subset of "
                             "critical/important/moderate/low")
        return value

    def to_definition(self) -> dict[str, Any]:
        data = self.model_dump(exclude={"name", "change_note"})
        data["default_rule"] = {
            "blocking_severities": data.pop("blocking_severities"),
            "remediation_days": data.pop("remediation_days"),
        }
        data["scope_rules"] = [r.model_dump() for r in self.scope_rules]
        return data


@app.get("/policies")
def list_policies(
    _: Annotated[Principal, Depends(require_permission("compliance:view"))],
) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT p.id, p.name, p.description, p.is_active, p.created_at,
               p.active_version_id, pv.version AS active_version,
               pv.definition, pv.effective_from
        FROM compliance.policies p
        LEFT JOIN compliance.policy_versions pv ON pv.id = p.active_version_id
        ORDER BY p.name
        """
    )


@app.post("/policies", status_code=201)
def create_policy(
    payload: PolicyDefinition,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("policies:manage"))],
) -> dict[str, Any]:
    definition = payload.to_definition()
    with connection() as conn:
        policy_id = str(
            conn.execute(
                text(
                    "INSERT INTO compliance.policies (name, is_active, created_by) "
                    "VALUES (:n, false, :by) RETURNING id"
                ),
                {"n": payload.name, "by": principal.username},
            ).first()[0]
        )
        version_id = str(
            conn.execute(
                text(
                    "INSERT INTO compliance.policy_versions "
                    "(policy_id, version, definition, created_by, change_note) "
                    "VALUES (:p, 1, CAST(:d AS jsonb), :by, :note) RETURNING id"
                ),
                {"p": policy_id, "d": json.dumps(definition),
                 "by": principal.username, "note": payload.change_note},
            ).first()[0]
        )
    record_audit(
        event_type="policy.created", outcome="success", principal=principal,
        target_type="policy", target_id=policy_id,
        context={"name": payload.name, "version": 1},
        source_ip=_client_ip(request), service=APP,
    )
    return {"id": policy_id, "version_id": version_id, "version": 1,
            "definition": definition}


@app.post("/policies/{policy_id}/versions", status_code=201)
def add_version(
    policy_id: str,
    payload: PolicyDefinition,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("policies:manage"))],
) -> dict[str, Any]:
    definition = payload.to_definition()
    with connection() as conn:
        current = conn.execute(
            text(
                "SELECT max(version) AS v FROM compliance.policy_versions "
                "WHERE policy_id = :p"
            ),
            {"p": policy_id},
        ).mappings().first()
        if current is None or current["v"] is None:
            raise HTTPException(status_code=404, detail="Policy not found")
        next_version = int(current["v"]) + 1
        version_id = str(
            conn.execute(
                text(
                    "INSERT INTO compliance.policy_versions "
                    "(policy_id, version, definition, created_by, change_note) "
                    "VALUES (:p, :v, CAST(:d AS jsonb), :by, :note) RETURNING id"
                ),
                {"p": policy_id, "v": next_version, "d": json.dumps(definition),
                 "by": principal.username, "note": payload.change_note},
            ).first()[0]
        )
    record_audit(
        event_type="policy.version_created", outcome="success", principal=principal,
        target_type="policy", target_id=policy_id,
        context={"version": next_version, "note": payload.change_note},
        source_ip=_client_ip(request), service=APP,
    )
    return {"id": policy_id, "version_id": version_id, "version": next_version,
            "definition": definition}


@app.post("/policies/{policy_id}/activate")
def activate_policy(
    policy_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("policies:manage"))],
    version_id: str | None = None,
) -> dict[str, Any]:
    with connection() as conn:
        if version_id:
            exists = conn.execute(
                text("SELECT 1 FROM compliance.policy_versions "
                     "WHERE id = :v AND policy_id = :p"),
                {"v": version_id, "p": policy_id},
            ).first()
            if not exists:
                raise HTTPException(status_code=404, detail="Policy version not found")
        else:
            row = conn.execute(
                text("SELECT id FROM compliance.policy_versions WHERE policy_id = :p "
                     "ORDER BY version DESC LIMIT 1"),
                {"p": policy_id},
            ).first()
            if not row:
                raise HTTPException(status_code=404, detail="Policy has no versions")
            version_id = str(row[0])
        conn.execute(text("UPDATE compliance.policies SET is_active = false"))
        conn.execute(
            text(
                "UPDATE compliance.policies SET is_active = true, "
                "active_version_id = :v WHERE id = :p"
            ),
            {"v": version_id, "p": policy_id},
        )
    refresh_compliance()
    record_audit(
        event_type="policy.activated", outcome="success", principal=principal,
        target_type="policy", target_id=policy_id,
        context={"version_id": version_id}, source_ip=_client_ip(request), service=APP,
    )
    return {"status": "activated", "policy_id": policy_id, "version_id": version_id}


@app.get("/policies/active")
def get_active_policy(
    _: Annotated[Principal, Depends(require_permission("compliance:view"))],
) -> dict[str, Any]:
    policy, version_id = active_policy()
    return {"policy": policy, "version_id": version_id}


# --------------------------------------------------------------------------- snapshots
@app.post("/evaluate")
def evaluate_now(
    principal: Annotated[Principal, Depends(require_permission("compliance:view"))],
) -> dict[str, Any]:
    return refresh_compliance()


class SnapshotRequest(BaseModel):
    scope: str = "on_demand"
    reporting_month: str | None = None
    quarter: str | None = None


@app.post("/snapshots")
def take_snapshot(
    payload: SnapshotRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("reports:generate"))],
) -> dict[str, Any]:
    try:
        start, end = period_bounds(payload.reporting_month, payload.quarter)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    scope = payload.scope if payload.scope in (
        "on_demand", "monthly", "quarterly", "daily"
    ) else "on_demand"
    result = create_snapshot(
        scope=scope, period_start=start, period_end=end,
        generated_by=principal.username,
    )
    record_audit(
        event_type="compliance.snapshot_created", outcome="success",
        principal=principal, target_type="snapshot",
        target_id=result["snapshot_id"], context={"scope": scope},
        source_ip=_client_ip(request), service=APP,
    )
    return result


@app.get("/snapshots")
def list_snapshots(
    _: Annotated[Principal, Depends(require_permission("compliance:view"))],
    scope: str | None = None,
    limit: int = Query(24, ge=1, le=200),
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM compliance.snapshots"
    params: dict[str, Any] = {"l": limit}
    if scope:
        sql += " WHERE scope = :scope"
        params["scope"] = scope
    return fetch_all(sql + " ORDER BY period_start DESC, captured_at DESC LIMIT :l",
                     **params)


@app.get("/snapshots/{snapshot_id}")
def get_snapshot(
    snapshot_id: str,
    _: Annotated[Principal, Depends(require_permission("compliance:view"))],
) -> dict[str, Any]:
    snapshot = fetch_one(
        "SELECT * FROM compliance.snapshots WHERE id = :id", id=snapshot_id
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    groups = fetch_all(
        "SELECT dimension, group_key, total_servers, compliant, non_compliant, "
        "unknown, stale, finding_count FROM compliance.snapshot_groups "
        "WHERE snapshot_id = :id ORDER BY dimension, total_servers DESC",
        id=snapshot_id,
    )
    by_dimension: dict[str, list[dict[str, Any]]] = {}
    for group in groups:
        by_dimension.setdefault(group["dimension"], []).append(group)
    return {"snapshot": snapshot, "groups": by_dimension}


# --------------------------------------------------------------------------- daily job
@app.post("/jobs/daily")
def daily_job(
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("sync:run"))],
) -> dict[str, Any]:
    snapshot_id = ensure_daily_snapshot()
    result = refresh_compliance()
    record_audit(
        event_type="compliance.daily_job", outcome="success", principal=principal,
        target_type="snapshot", target_id=snapshot_id or "",
        source_ip=_client_ip(request), service=APP,
    )
    return {"snapshot_id": snapshot_id, **result}


class SolarisRecord(BaseModel):
    hostname: str
    fqdn: str | None = None
    ip_address: str | None = None
    os_name: str = "Oracle Solaris"
    os_version: str = ""
    assessed_at: datetime
    patches: list[dict[str, Any]] = Field(default_factory=list)


@app.post("/solaris/ingest")
def solaris_ingest(
    records: list[SolarisRecord],
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("sync:run"))],
) -> dict[str, Any]:
    """Accepts pre-validated Solaris assessments from the approved collector."""
    import hashlib

    from .solaris_ingest import ingest_solaris_records

    digest = hashlib.sha256(
        json.dumps([r.model_dump(mode="json") for r in records],
                   sort_keys=True).encode()
    ).hexdigest()
    result = ingest_solaris_records(records, uploaded_by=principal.username)  # type: ignore[arg-type]
    record_audit(
        event_type="solaris.ingested",
        outcome="success" if result["accepted"] else "failure",
        principal=principal, target_type="solaris_upload",
        context=result, source_ip=_client_ip(request), service=APP,
    )
    return {"sha256": digest, **result}


@app.get("/solaris/uploads")
def solaris_uploads(
    _: Annotated[Principal, Depends(require_permission("integrations:view"))],
) -> list[dict[str, Any]]:
    return fetch_all(
        "SELECT * FROM compliance.solaris_uploads ORDER BY received_at DESC LIMIT 50"
    )
