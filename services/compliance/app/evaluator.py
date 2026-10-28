"""Loads asset state from PostgreSQL, evaluates the active policy and
persists per-asset compliance plus immutable snapshots."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from patchtrack_common.db import connection, fetch_all, fetch_one

from .engine import (
    DEFAULT_POLICY,
    AssetInput,
    Evaluation,
    aggregate,
    evaluate_asset,
    group_breakdown,
    period_bounds,
    severity_breakdown,
)


def active_policy() -> tuple[dict[str, Any], str | None]:
    row = fetch_one(
        """
        SELECT pv.id, pv.definition
        FROM compliance.policies p
        JOIN compliance.policy_versions pv ON pv.id = p.active_version_id
        WHERE p.is_active
        ORDER BY pv.effective_from DESC LIMIT 1
        """
    )
    if row is None:
        return {**DEFAULT_POLICY, "name": "Built-in baseline"}, None
    definition = row["definition"]
    if isinstance(definition, str):
        definition = json.loads(definition)
    return {**DEFAULT_POLICY, **definition}, str(row["id"])


def policy_version_for(version_id: str | None) -> dict[str, Any]:
    if version_id is None:
        return {**DEFAULT_POLICY, "name": "Built-in baseline"}
    row = fetch_one(
        "SELECT definition FROM compliance.policy_versions WHERE id = :id",
        id=version_id,
    )
    if row is None:
        return {**DEFAULT_POLICY, "name": "Built-in baseline"}
    definition = row["definition"]
    if isinstance(definition, str):
        definition = json.loads(definition)
    return {**DEFAULT_POLICY, **definition}


def load_assets() -> dict[str, AssetInput]:
    rows = fetch_all(
        """
        SELECT a.id, a.hostname, a.os_family, a.os_version, a.environment,
               a.business_group, a.application_group, a.lifecycle_status,
               a.in_scope, a.patch_source,
               asmt.assessed_at, asmt.status AS assessment_status,
               asmt.critical_count, asmt.important_count, asmt.moderate_count,
               asmt.low_count, asmt.total_patches
        FROM inventory.asset_effective a
        LEFT JOIN compliance.asset_latest_assessment la ON la.asset_id = a.id
        LEFT JOIN compliance.assessments asmt ON asmt.id = la.assessment_id
        """
    )
    result: dict[str, AssetInput] = {}
    for row in rows:
        counts: dict[str, int] = {}
        if row["assessment_status"] is not None:
            counts = {
                "critical": row["critical_count"] or 0,
                "important": row["important_count"] or 0,
                "moderate": row["moderate_count"] or 0,
                "low": row["low_count"] or 0,
            }
            declared = row["total_patches"] or 0
            accounted = sum(counts.values())
            if declared > accounted:
                counts["unknown"] = declared - accounted
        result[str(row["id"])] = AssetInput(
            asset_id=str(row["id"]),
            hostname=row["hostname"],
            os_family=row["os_family"],
            os_version=row["os_version"] or "",
            environment=row["environment"] or "UNKNOWN",
            business_group=row["business_group"] or "UNKNOWN",
            application_group=row["application_group"] or "UNKNOWN",
            lifecycle_status=row["lifecycle_status"],
            in_scope=row["in_scope"],
            patch_source=row["patch_source"] or "",
            assessment_at=row["assessed_at"],
            assessment_status=row["assessment_status"],
            severity_counts=counts,
        )
    return result


def evaluate_all(*, now: datetime | None = None) -> tuple[
    dict[str, AssetInput], dict[str, Evaluation], dict[str, Any], str | None
]:
    now = now or datetime.now(UTC)
    policy, version_id = active_policy()
    assets = load_assets()
    evaluations = {
        asset_id: evaluate_asset(asset, policy, now=now)
        for asset_id, asset in assets.items()
    }
    return assets, evaluations, policy, version_id


def persist_states(
    evaluations: dict[str, Evaluation], version_id: str | None
) -> None:
    with connection() as conn:
        for evaluation in evaluations.values():
            conn.execute(
                text(
                    """
                    INSERT INTO compliance.asset_compliance (
                        asset_id, state, reason, policy_version_id, evaluated_at,
                        assessment_age_hours, critical_count, total_patches
                    ) VALUES (
                        :a, :s, :r, :pv, now(), :age, :crit, :total
                    )
                    ON CONFLICT (asset_id) DO UPDATE SET
                        state = EXCLUDED.state, reason = EXCLUDED.reason,
                        policy_version_id = EXCLUDED.policy_version_id,
                        evaluated_at = EXCLUDED.evaluated_at,
                        assessment_age_hours = EXCLUDED.assessment_age_hours,
                        critical_count = EXCLUDED.critical_count,
                        total_patches = EXCLUDED.total_patches
                    """
                ),
                {
                    "a": evaluation.asset_id, "s": evaluation.state,
                    "r": evaluation.reason, "pv": version_id,
                    "age": evaluation.assessment_age_hours,
                    "crit": evaluation.severity_counts.get("critical", 0),
                    "total": evaluation.total_patches,
                },
            )


def refresh_compliance(*, now: datetime | None = None) -> dict[str, Any]:
    assets, evaluations, policy, version_id = evaluate_all(now=now)
    persist_states(evaluations, version_id)
    pop = aggregate(
        list(evaluations.values()), target_pct=policy.get("target_pct")
    )
    return {
        "evaluated_at": (now or datetime.now(UTC)).isoformat(),
        "policy_version_id": version_id,
        **pop.as_dict(),
    }


def create_snapshot(
    *,
    scope: str,
    period_start: datetime,
    period_end: datetime,
    generated_by: str,
    filters: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate the population and freeze the result. Snapshots are immutable."""
    now = now or datetime.now(UTC)
    assets, evaluations, policy, version_id = evaluate_all(now=now)
    evaluations_list = list(evaluations.values())
    pop = aggregate(evaluations_list, target_pct=policy.get("target_pct"))
    methodology = {
        "population": "in-scope assets with active lifecycle",
        "numerator": "assets whose state is compliant",
        "denominator": "compliant + non_compliant + unknown + stale",
        "unknown_treatment": "counted in denominator, never as compliant",
        "freshness_hours": policy.get("freshness_hours"),
        "blocking_severities": policy.get("default_rule", {}).get(
            "blocking_severities"
        ),
        "target_pct": policy.get("target_pct"),
        "quarterly_target_pct": policy.get("quarterly_target_pct"),
        "policy_name": policy.get("name"),
        "evaluated_at": now.isoformat(),
    }
    with connection() as conn:
        snapshot_id = str(
            conn.execute(
                text(
                    """
                    INSERT INTO compliance.snapshots (
                        scope, period_start, period_end, captured_at,
                        policy_version_id, filters, total_servers,
                        compliant_servers, non_compliant_servers, unknown_servers,
                        stale_servers, excluded_servers, compliance_pct, target_pct,
                        critical_findings, total_findings, generated_by, methodology
                    ) VALUES (
                        :scope, :ps, :pe, :captured, :pv, CAST(:filters AS jsonb),
                        :total, :comp, :non, :unk, :stale, :excluded, :pct, :target,
                        :crit, :findings, :by, CAST(:methodology AS jsonb)
                    ) RETURNING id
                    """
                ),
                {
                    "scope": scope, "ps": period_start.date(),
                    "pe": period_end.date(), "captured": now, "pv": version_id,
                    "filters": json.dumps(filters or {}),
                    "total": pop.total, "comp": pop.compliant,
                    "non": pop.non_compliant, "unk": pop.unknown,
                    "stale": pop.stale, "excluded": pop.excluded,
                    "pct": pop.compliance_pct, "target": pop.target_pct,
                    "crit": pop.critical_findings,
                    "findings": pop.total_findings, "by": generated_by,
                    "methodology": json.dumps(methodology),
                },
            ).first()[0]
        )
        rows = []
        # os_family is recorded alongside os_version: the executive panel asks
        # "which platform is behind", a question that cannot be answered from
        # the version rows after the fact.
        for dimension in ("os_family", "os_version", "environment",
                          "business_group", "application_group"):
            for entry in group_breakdown(evaluations_list, assets, dimension):
                rows.append(
                    (snapshot_id, dimension, entry["group"],
                     entry["total_servers"], entry["compliant"],
                     entry["non_compliant"], entry["unknown"],
                     entry["stale"], entry["total_findings"])
                )
        for entry in severity_breakdown(evaluations_list):
            rows.append(
                (snapshot_id, "severity", entry["severity"], 0, 0, 0, 0, 0,
                 entry["count"])
            )
        for row in rows:
            conn.execute(
                text(
                    """
                    INSERT INTO compliance.snapshot_groups (
                        snapshot_id, dimension, group_key, total_servers,
                        compliant, non_compliant, unknown, stale, finding_count
                    ) VALUES (:s, :d, :k, :t, :c, :n, :u, :st, :f)
                    ON CONFLICT (snapshot_id, dimension, group_key) DO NOTHING
                    """
                ),
                {
                    "s": row[0], "d": row[1], "k": row[2], "t": row[3],
                    "c": row[4], "n": row[5], "u": row[6], "st": row[7],
                    "f": row[8],
                },
            )
        # Persist current states so the dashboard and drill-down agree with
        # the snapshot just captured.
        persist_states(evaluations, version_id)
    return {"snapshot_id": snapshot_id, **pop.as_dict(), "methodology": methodology}


def daily_snapshot(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return create_snapshot(
        scope="daily", period_start=start, period_end=start,
        generated_by="scheduler", now=now,
    )


def period_snapshot(scope: str, period_start: datetime, period_end: datetime,
                    generated_by: str) -> dict[str, Any]:
    return create_snapshot(
        scope=scope, period_start=period_start, period_end=period_end,
        generated_by=generated_by,
    )


def ensure_daily_snapshot(now: datetime | None = None) -> str | None:
    """Create today's snapshot if missing. Returns the snapshot id, if any."""
    now = now or datetime.now(UTC)
    today = now.date()
    existing = fetch_one(
        """
        SELECT id FROM compliance.snapshots
        WHERE scope = 'daily' AND period_start = :d
          AND (captured_at AT TIME ZONE 'UTC')::date = :d
        """,
        d=today,
    )
    if existing:
        return str(existing["id"])
    result = daily_snapshot(now)
    return result["snapshot_id"]


def last_snapshot_for_period(scope: str, start: datetime) -> dict[str, Any] | None:
    return fetch_one(
        """
        SELECT * FROM compliance.snapshots
        WHERE scope = :s AND period_start = :p
        ORDER BY captured_at DESC LIMIT 1
        """,
        s=scope, p=start.date(),
    )
