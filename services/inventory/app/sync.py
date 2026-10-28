"""Inventory synchronisation and patch-assessment collection.

Design rules enforced here:

* A failed AssetExplorer run records a failed ``sync_runs`` row and touches
  no asset rows.
* Assets absent from AssetExplorer are never deleted; they are marked
  ``retired`` only when AssetExplorer itself reports a retired lifecycle.
* A failed patch collection writes no assessment. Absence of data is never
  compliance.
* Records are matched to assets by stable source identifier first, then by
  exact FQDN, then hostname. Ambiguous matches are written to
  ``reconciliation_conflicts`` rather than guessed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

from patchtrack_common.db import connection, fetch_one
from patchtrack_common.secrets import SecretError, resolve as resolve_secret

from .connectors.assetexplorer import AssetExplorerClient, AssetExplorerError
from .connectors.base import AssetRecord, PatchAssessment
from .connectors.satellite import SatelliteClient, SatelliteError
from .connectors.sccm import SccmClient, SccmError
from .connectors.solaris import SolarisIngestError, load_inbox

log = logging.getLogger(__name__)

PATCH_SOURCE_KEYS = ("satellite", "sccm", "solaris")


# --------------------------------------------------------------------------- helpers
def _settings_row(conn, key: str) -> dict[str, Any] | None:
    row = conn.execute(
        text("SELECT * FROM inventory.integrations WHERE key = :k"), {"k": key}
    ).mappings().first()
    return dict(row) if row else None


def _start_run(conn, integration_key: str, trigger: str, requested_by: str) -> str:
    row = conn.execute(
        text(
            """
            INSERT INTO inventory.sync_runs (integration_key, trigger, requested_by)
            VALUES (:k, :t, :by) RETURNING id
            """
        ),
        {"k": integration_key, "t": trigger, "by": requested_by},
    ).first()
    return str(row[0])


def _finish_run(
    conn,
    run_id: str,
    status: str,
    *,
    seen: int = 0,
    created: int = 0,
    updated: int = 0,
    rejected: int = 0,
    unmatched: int = 0,
    error: str = "",
    details: dict | None = None,
) -> None:
    conn.execute(
        text(
            """
            UPDATE inventory.sync_runs
            SET status = :s, finished_at = now(), records_seen = :seen,
                records_created = :created, records_updated = :updated,
                records_rejected = :rejected, assets_unmatched = :unmatched,
                error_summary = :error, details = CAST(:details AS jsonb)
            WHERE id = :id
            """
        ),
        {
            "s": status, "seen": seen, "created": created, "updated": updated,
            "rejected": rejected, "unmatched": unmatched, "error": error[:2000],
            "details": json.dumps(details or {}, default=str), "id": run_id,
        },
    )


def _build_assetexplorer(settings, secret: str) -> AssetExplorerClient:
    extra = settings.get("extra") or {}
    if isinstance(extra, str):
        extra = json.loads(extra)
    return AssetExplorerClient(
        settings["endpoint"],
        auth_method=settings.get("auth_method", "bearer"),
        secret=secret,
        timeout_s=float(settings.get("connect_timeout_s") or 30),
        page_size=int(settings.get("page_size") or 200),
        verify_tls=bool(settings.get("verify_tls", True)),
        field_map=extra.get("field_map"),
    )


# --------------------------------------------------------------------------- upsert
def upsert_asset(
    conn, record: AssetRecord, run_id: str, *, seen_at: datetime | None = None
) -> tuple[str, bool]:
    """Insert or update one AssetExplorer record. Returns (asset_id, created)."""
    seen_at = seen_at or datetime.now(UTC)
    existing = conn.execute(
        text(
            # host() drops the /32 netmask that inet::text would append, so an
            # unchanged address compares equal and does not look like a change.
            "SELECT id, hostname, host(ip_address) AS ip, lifecycle_status "
            "FROM inventory.assets WHERE assetexplorer_id = :sid"
        ),
        {"sid": record.source_id},
    ).mappings().first()
    payload = asdict(record)
    payload.pop("raw", None)
    payload["os_family"] = record.os_family
    payload["raw"] = json.dumps(record.raw or {}, default=str)

    if existing:
        asset_id = str(existing["id"])
        conn.execute(
            text(
                """
                UPDATE inventory.assets SET
                    hostname = :hostname, fqdn = :fqdn,
                    ip_address = CAST(:ip_address AS inet),
                    os_family = :os_family, os_name = :os_name,
                    os_version = :os_version, asset_type = :asset_type,
                    asset_status = :asset_status, environment = :environment,
                    business_group = :business_group,
                    application_group = :application_group,
                    asset_owner = :asset_owner, department = :department,
                    location = :location, lifecycle_status = :lifecycle_status,
                    classification = :classification,
                    source_created_at = :source_created_at,
                    source_modified_at = :source_modified_at,
                    last_synced_at = :seen_at, last_sync_run_id = :run_id,
                    raw = CAST(:raw AS jsonb)
                WHERE id = :id
                """
            ),
            {**payload, "seen_at": seen_at, "run_id": run_id, "id": asset_id},
        )
        _record_aliases(conn, asset_id, record, seen_at)
        if existing["hostname"] != record.hostname or (
            existing["ip"] and record.ip_address and existing["ip"] != record.ip_address
        ):
            _maybe_conflict(
                conn, asset_id, record,
                "identifier_changed",
                {"previous_hostname": existing["hostname"],
                 "previous_ip": existing["ip"],
                 "new_hostname": record.hostname,
                 "new_ip": record.ip_address},
            )
        return asset_id, False

    asset_id = str(
        conn.execute(
            text(
                """
                INSERT INTO inventory.assets (
                    assetexplorer_id, hostname, fqdn, ip_address, os_family,
                    os_name, os_version, asset_type, asset_status, environment,
                    business_group, application_group, asset_owner, department,
                    location, lifecycle_status, classification, in_scope,
                    exclusion_reason, source_created_at, source_modified_at,
                    last_synced_at, last_sync_run_id, raw
                ) VALUES (
                    :source_id, :hostname, :fqdn, CAST(:ip_address AS inet),
                    :os_family, :os_name, :os_version, :asset_type,
                    :asset_status, :environment, :business_group,
                    :application_group, :asset_owner, :department, :location,
                    :lifecycle_status, :classification,
                    :lifecycle_status = 'active', '', :source_created_at,
                    :source_modified_at, :seen_at, :run_id, CAST(:raw AS jsonb)
                ) RETURNING id
                """
            ),
            {**payload, "seen_at": seen_at, "run_id": run_id},
        ).first()[0]
    )
    _record_aliases(conn, asset_id, record, seen_at)
    return asset_id, True


def _record_aliases(conn, asset_id: str, record: AssetRecord, seen_at: datetime) -> None:
    for alias_type, value in (
        ("hostname", record.hostname),
        ("fqdn", record.fqdn),
        ("ip", record.ip_address),
    ):
        if not value:
            continue
        row = conn.execute(
            text(
                "SELECT asset_id FROM inventory.asset_aliases "
                "WHERE alias_type = :t AND alias_value = :v"
            ),
            {"t": alias_type, "v": value},
        ).first()
        if row and str(row[0]) != asset_id:
            _maybe_conflict(
                conn, asset_id, record, f"alias_{alias_type}_collision",
                {"alias_type": alias_type, "alias_value": value,
                 "other_asset_id": str(row[0])},
            )
            continue
        conn.execute(
            text(
                """
                INSERT INTO inventory.asset_aliases
                    (asset_id, alias_type, alias_value, first_seen, last_seen)
                VALUES (:a, :t, :v, :seen, :seen)
                ON CONFLICT (alias_type, alias_value)
                DO UPDATE SET last_seen = EXCLUDED.last_seen
                """
            ),
            {"a": asset_id, "t": alias_type, "v": value, "seen": seen_at},
        )


def _maybe_conflict(
    conn, asset_id: str, record: AssetRecord, reason: str, payload: dict
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO inventory.reconciliation_conflicts
                (asset_id, source_system, source_ref, match_key, reason, payload)
            VALUES (:a, 'assetexplorer', :ref, :key, :reason, CAST(:payload AS jsonb))
            """
        ),
        {
            "a": asset_id, "ref": record.source_id,
            "key": record.hostname, "reason": reason,
            "payload": json.dumps(payload, default=str),
        },
    )


# --------------------------------------------------------------------------- sync
def run_assetexplorer_sync(
    *, trigger: str = "schedule", requested_by: str = "schedule",
    secret: str = "", client: AssetExplorerClient | None = None,
) -> dict[str, Any]:
    with connection() as conn:
        settings = _settings_row(conn, "assetexplorer")
        if settings is None:
            raise AssetExplorerError("AssetExplorer integration is not configured")
        if not settings["enabled"] and trigger != "manual":
            return {"status": "skipped", "reason": "integration disabled"}
        run_id = _start_run(conn, "assetexplorer", trigger, requested_by)

    owns_client = client is None
    try:
        if not secret:
            secret = resolve_secret(settings.get("secret_ref") or "")
        client = client or _build_assetexplorer(settings, secret)
        records = client.fetch_assets()
    except (AssetExplorerError, SecretError) as exc:
        with connection() as conn:
            _finish_run(conn, run_id, "failed", error=str(exc))
        log.error("AssetExplorer sync failed: %s", exc)
        return {"status": "failed", "run_id": run_id, "error": str(exc)}
    finally:
        if owns_client and client is not None:
            client.close()

    created = updated = rejected = 0
    with connection() as conn:
        for record in records:
            try:
                _, is_new = upsert_asset(conn, record, run_id)
            except Exception as exc:  # noqa: BLE001 - one bad record must not abort the run
                rejected += 1
                log.warning("record %s rejected: %s", record.source_id, exc)
                continue
            created += int(is_new)
            updated += int(not is_new)
        status = "success" if rejected == 0 else "partial"
        _finish_run(
            conn, run_id, status,
            seen=len(records), created=created, updated=updated, rejected=rejected,
            details={"source": "assetexplorer"},
        )
    return {
        "status": status, "run_id": run_id, "seen": len(records),
        "created": created, "updated": updated, "rejected": rejected,
    }


# --------------------------------------------------------------------------- matching
def match_asset(conn, assessment: PatchAssessment) -> tuple[str | None, str]:
    """Resolve an assessment to an asset id. Returns (asset_id, state)."""
    link = conn.execute(
        text(
            "SELECT asset_id FROM inventory.asset_source_links "
            "WHERE source_system = :s AND source_ref = :r"
        ),
        {"s": assessment.source_system, "r": assessment.source_ref},
    ).first()
    if link:
        return str(link[0]), "direct"
    for alias_type, value in (("fqdn", assessment.fqdn),
                              ("hostname", assessment.hostname),
                              ("ip", assessment.ip_address)):
        if not value:
            continue
        rows = conn.execute(
            text(
                """
                SELECT DISTINCT a.id FROM inventory.assets a
                LEFT JOIN inventory.asset_aliases al
                  ON al.asset_id = a.id AND al.alias_type = :t
                WHERE (al.alias_value = :v)
                   OR (:t = 'hostname' AND lower(a.hostname) = lower(:v))
                   OR (:t = 'fqdn' AND lower(a.fqdn) = lower(:v))
                   OR (:t = 'ip' AND a.ip_address = CAST(:v AS inet))
                """
            ),
            {"t": alias_type, "v": value},
        ).all()
        if len(rows) == 1:
            return str(rows[0][0]), alias_type
        if len(rows) > 1:
            return None, "ambiguous"
    return None, "unmatched"


def _link_source(conn, asset_id: str, assessment: PatchAssessment, method: str) -> None:
    conn.execute(
        text(
            """
            INSERT INTO inventory.asset_source_links
                (asset_id, source_system, source_ref, confidence, method)
            VALUES (:a, :s, :r, 1.000, :m)
            ON CONFLICT (source_system, source_ref)
            DO UPDATE SET asset_id = EXCLUDED.asset_id
            """
        ),
        {"a": asset_id, "s": assessment.source_system,
         "r": assessment.source_ref, "m": method},
    )


def _store_assessment(
    conn, asset_id: str, assessment: PatchAssessment, run_id: str
) -> None:
    counts = assessment.severity_counts()
    # Skip rewriting an identical assessment re-delivered by the source.
    latest = conn.execute(
        text(
            "SELECT a.assessed_at, a.total_patches FROM compliance.assessments a "
            "JOIN compliance.asset_latest_assessment l ON l.assessment_id = a.id "
            "WHERE l.asset_id = :a"
        ),
        {"a": asset_id},
    ).mappings().first()
    if (
        latest
        and assessment.assessed_at is not None
        and latest["assessed_at"] == assessment.assessed_at
        and latest["total_patches"] == len(assessment.findings)
    ):
        return
    assessment_id = str(
        conn.execute(
            text(
                """
                INSERT INTO compliance.assessments (
                    asset_id, source_system, assessed_at, sync_run_id, status,
                    total_patches, critical_count, important_count,
                    moderate_count, low_count, source_reference, details
                ) VALUES (
                    :asset, :source, :assessed, :run, :status, :total,
                    :critical, :important, :moderate, :low, :ref,
                    CAST(:details AS jsonb)
                ) RETURNING id
                """
            ),
            {
                "asset": asset_id, "source": assessment.source_system,
                "assessed": assessment.assessed_at, "run": run_id,
                "status": assessment.status, "total": len(assessment.findings),
                "critical": counts["critical"], "important": counts["important"],
                "moderate": counts["moderate"], "low": counts["low"],
                "ref": assessment.source_reference,
                "details": json.dumps({"hostname": assessment.hostname}),
            },
        ).first()[0]
    )
    for finding in assessment.findings:
        conn.execute(
            text(
                """
                INSERT INTO compliance.findings (
                    asset_id, assessment_id, source_system, advisory_id, title,
                    severity, category, released_at, source_reference
                ) VALUES (:a, :asmt, :src, :adv, :title, :sev, :cat, :rel, :ref)
                ON CONFLICT (assessment_id, advisory_id) DO NOTHING
                """
            ),
            {
                "a": asset_id, "asmt": assessment_id,
                "src": assessment.source_system, "adv": finding.advisory_id,
                "title": finding.title, "sev": finding.severity,
                "cat": finding.category, "rel": finding.released_at,
                "ref": finding.source_reference,
            },
        )
    conn.execute(
        text(
            """
            INSERT INTO compliance.asset_latest_assessment
                (asset_id, assessment_id, updated_at)
            VALUES (:a, :asmt, now())
            ON CONFLICT (asset_id) DO UPDATE
              SET assessment_id = EXCLUDED.assessment_id,
                  updated_at = EXCLUDED.updated_at
            """
        ),
        {"a": asset_id, "asmt": assessment_id},
    )


def ingest_assessments(
    assessments: list[PatchAssessment], *, run_id: str, source_key: str
) -> dict[str, int]:
    matched = unmatched = failed = 0
    with connection() as conn:
        for assessment in assessments:
            asset_id, method = match_asset(conn, assessment)
            if asset_id is None:
                unmatched += 1
                conn.execute(
                    text(
                        """
                        INSERT INTO inventory.reconciliation_conflicts
                            (source_system, source_ref, match_key, reason, payload)
                        VALUES (:s, :r, :k, :reason, CAST(:p AS jsonb))
                        """
                    ),
                    {
                        "s": assessment.source_system,
                        "r": assessment.source_ref,
                        "k": assessment.fqdn or assessment.hostname,
                        "reason": f"assessment_{method}",
                        "p": json.dumps(
                            {"hostname": assessment.hostname,
                             "ip": assessment.ip_address}, default=str
                        ),
                    },
                )
                continue
            try:
                if method not in ("direct", "ambiguous"):
                    _link_source(conn, asset_id, assessment, method)
                _store_assessment(conn, asset_id, assessment, run_id)
                matched += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log.warning("assessment for %s failed: %s", assessment.hostname, exc)
    return {"matched": matched, "unmatched": unmatched, "failed": failed}


def run_patch_sync(
    source_key: str,
    *,
    trigger: str = "schedule",
    requested_by: str = "schedule",
) -> dict[str, Any]:
    if source_key not in PATCH_SOURCE_KEYS:
        raise ValueError(f"unknown patch source {source_key}")
    with connection() as conn:
        settings = _settings_row(conn, source_key)
        if settings is None:
            raise RuntimeError(f"{source_key} integration is not configured")
        if not settings["enabled"] and trigger != "manual":
            return {"status": "skipped", "reason": "integration disabled"}
        run_id = _start_run(conn, source_key, trigger, requested_by)

    extra = settings.get("extra") or {}
    if isinstance(extra, str):
        extra = json.loads(extra)

    try:
        secret = resolve_secret(settings.get("secret_ref") or "")
        if source_key == "satellite":
            with SatelliteClient(
                settings["endpoint"],
                username=extra.get("username", ""),
                password=secret,
                timeout_s=float(settings.get("read_timeout_s") or 60),
                page_size=int(settings.get("page_size") or 100),
                verify_tls=bool(settings.get("verify_tls", True)),
                workers=int(settings.get("workers") or 1),
            ) as client:
                assessments = client.fetch_assessments()
        elif source_key == "sccm":
            with SccmClient(
                settings["endpoint"], token=secret,
                timeout_s=float(settings.get("read_timeout_s") or 60),
                page_size=int(settings.get("page_size") or 200),
                verify_tls=bool(settings.get("verify_tls", True)),
            ) as client:
                assessments = client.fetch_assessments()
        else:
            inbox = Path(extra.get("inbox") or "/var/lib/patchtrack/solaris-inbox")
            loaded = load_inbox(inbox)
            assessments = [a for _, a in loaded]
    except (SatelliteError, SccmError, SolarisIngestError, SecretError) as exc:
        with connection() as conn:
            _finish_run(conn, run_id, "failed", error=str(exc))
        log.error("%s collection failed: %s", source_key, exc)
        return {"status": "failed", "run_id": run_id, "error": str(exc)}

    counts = ingest_assessments(assessments, run_id=run_id, source_key=source_key)
    status = (
        "success" if counts["failed"] == 0 and counts["unmatched"] == 0
        else "partial"
    )
    with connection() as conn:
        _finish_run(
            conn, run_id, status, seen=len(assessments),
            updated=counts["matched"], rejected=counts["failed"],
            unmatched=counts["unmatched"],
            details={"source": source_key},
        )
    return {"status": status, "run_id": run_id, "seen": len(assessments), **counts}


def resolve_conflict(
    conn, conflict_id: int, asset_id: str | None, resolved_by: str, resolution: str
) -> bool:
    if asset_id:
        row = conn.execute(
            text(
                "SELECT source_system, source_ref FROM inventory.reconciliation_conflicts "
                "WHERE id = :id AND resolved_at IS NULL"
            ),
            {"id": conflict_id},
        ).mappings().first()
        if row is None:
            return False
        conn.execute(
            text(
                """
                INSERT INTO inventory.asset_source_links
                    (asset_id, source_system, source_ref, confidence, method)
                VALUES (:a, :s, :r, 1.000, 'manual')
                ON CONFLICT (source_system, source_ref)
                DO UPDATE SET asset_id = EXCLUDED.asset_id, method = 'manual'
                """
            ),
            {"a": asset_id, "s": row["source_system"], "r": row["source_ref"]},
        )
        if row["source_system"] == "assetexplorer":
            conn.execute(
                text(
                    "UPDATE inventory.assets SET reconciliation_state = 'matched' "
                    "WHERE assetexplorer_id = :r"
                ),
                {"r": row["source_ref"]},
            )
    updated = conn.execute(
        text(
            "UPDATE inventory.reconciliation_conflicts SET resolved_at = now(), "
            "resolved_by = :by, resolution = :res "
            "WHERE id = :id AND resolved_at IS NULL"
        ),
        {"by": resolved_by, "res": resolution, "id": conflict_id},
    ).rowcount
    return bool(updated)
