"""Report generation orchestration shared by the API and the scheduler."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text

from patchtrack_common.config import get_settings
from patchtrack_common.db import connection, fetch_all, fetch_one

from .mailer import send_report_email
from .pdf import ReportContext, render_report, sha256_of, validate_pdf
from .smtp_config import resolve_smtp
from patchtrack_common import timezones as timezone_settings

log = logging.getLogger(__name__)

REPORT_TITLES = {
    "monthly_management": "Monthly Patch Management Report",
    "quarterly_compliance": "Quarterly Patch Compliance Report",
    "inventory_register": "Server Inventory Register",
    "board_pack": "Executive Board Pack",
}


class ReportError(RuntimeError):
    pass


# Retention is an administrator setting stored in shared.app_settings, not a
# deployment constant: changing it applies to existing reports and to the next
# purge without a restart. The environment value is the default until an
# administrator saves a window.
REPORT_RETENTION_KEY = "report_retention_days"
REPORT_RETENTION_DEFAULT = 2555
REPORT_RETENTION_MIN = 30
REPORT_RETENTION_MAX = 36500


def report_retention_days() -> tuple[int, dict[str, Any] | None]:
    """The configured file-retention window, and the settings row if any."""
    row = fetch_one(
        "SELECT value, updated_at, updated_by FROM shared.app_settings "
        "WHERE key = :key",
        key=REPORT_RETENTION_KEY,
    )
    fallback = int(get_settings().report_retention_days)
    if row is None:
        return fallback, None
    value = row["value"]
    if isinstance(value, str):
        value = json.loads(value)
    stored = value.get("days") if isinstance(value, dict) else value
    try:
        days = int(stored)
    except (TypeError, ValueError):
        return fallback, row
    if not REPORT_RETENTION_MIN <= days <= REPORT_RETENTION_MAX:
        return fallback, row
    return days, row


def _report_dir() -> Path:
    path = Path(get_settings().report_dir)
    path.mkdir(parents=True, exist_ok=True, mode=0o750)
    return path


def _load_snapshot_for(report: dict) -> dict[str, Any]:
    """Prefer the snapshot the report was queued against, else the latest one
    whose period matches. Reports never silently fall back to live data."""
    if report.get("snapshot_id"):
        snapshot = fetch_one(
            "SELECT * FROM compliance.snapshots WHERE id = :id",
            id=report["snapshot_id"],
        )
        if snapshot is None:
            raise ReportError("The snapshot this report was queued against is missing")
        return snapshot
    snapshot = fetch_one(
        """
        SELECT * FROM compliance.snapshots
        WHERE scope IN ('monthly', 'quarterly', 'daily', 'on_demand')
          AND period_start <= :pe AND period_end >= :ps
        ORDER BY (scope IN ('monthly', 'quarterly')) DESC, captured_at DESC
        LIMIT 1
        """,
        ps=report["period_start"], pe=report["period_end"],
    )
    if snapshot is None:
        raise ReportError(
            "No compliance snapshot covers this period. Generate the period "
            "snapshot before requesting the report."
        )
    return snapshot


def _snapshot_groups(snapshot_id: str) -> dict[str, list[dict[str, Any]]]:
    rows = fetch_all(
        "SELECT dimension, group_key, total_servers, compliant, non_compliant, "
        "unknown, stale, finding_count FROM compliance.snapshot_groups "
        "WHERE snapshot_id = :id ORDER BY dimension, total_servers DESC",
        id=snapshot_id,
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["dimension"], []).append(row)
    return grouped


def _previous_snapshot(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    return fetch_one(
        """
        SELECT * FROM compliance.snapshots
        WHERE scope = :scope AND period_start < :ps
        ORDER BY period_start DESC LIMIT 1
        """,
        scope=snapshot["scope"], ps=snapshot["period_start"],
    )


def _trend(limit: int = 6) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT date_trunc('month', period_start)::date AS month,
               avg(compliance_pct)::numeric(6,2) AS compliance_pct
        FROM compliance.snapshots
        WHERE scope = 'daily'
        GROUP BY 1 ORDER BY 1 DESC LIMIT :l
        """,
        l=limit,
    )[::-1]


def _freshness() -> dict[str, Any]:
    inventory = fetch_one(
        "SELECT max(finished_at) AS at FROM inventory.sync_runs "
        "WHERE integration_key = 'assetexplorer' "
        "AND status IN ('success', 'partial')"
    )
    patch = fetch_one(
        "SELECT max(finished_at) AS at FROM inventory.sync_runs "
        "WHERE integration_key IN ('satellite', 'sccm', 'solaris') "
        "AND status IN ('success', 'partial')"
    )
    return {
        "inventory_synced_at": inventory["at"] if inventory else None,
        "patch_synced_at": patch["at"] if patch else None,
    }


def _integration_health() -> list[dict[str, Any]]:
    integrations = fetch_all(
        "SELECT key, enabled, endpoint FROM inventory.integrations ORDER BY key"
    )
    for integration in integrations:
        last = fetch_one(
            "SELECT status, finished_at FROM inventory.sync_runs "
            "WHERE integration_key = :k ORDER BY started_at DESC LIMIT 1",
            k=integration["key"],
        )
        success = fetch_one(
            "SELECT max(finished_at) AS at FROM inventory.sync_runs "
            "WHERE integration_key = :k AND status = 'success'",
            k=integration["key"],
        )
        integration["last_run"] = last
        integration["last_success_at"] = success["at"] if success else None
        if not integration["enabled"] or not integration["endpoint"]:
            integration["health"] = "not_configured"
        elif not last:
            integration["health"] = "pending"
        elif last["status"] == "failed":
            integration["health"] = "unavailable"
        elif last["status"] == "partial":
            integration["health"] = "degraded"
        else:
            integration["health"] = "healthy"
    return integrations


def _top_non_compliant(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT a.hostname, a.os_name, a.os_version, a.environment,
               a.business_group, c.total_patches
        FROM compliance.asset_compliance c
        JOIN inventory.asset_effective a ON a.id = c.asset_id
        WHERE c.state = 'non_compliant'
        ORDER BY c.total_patches DESC, a.hostname LIMIT 25
        """
    )


def _period_label(snapshot: dict[str, Any]) -> str:
    """period_end is exclusive, so the label states the last included day."""
    start = snapshot["period_start"]
    end = snapshot["period_end"]
    last_day = end - timedelta(days=1) if end > start else start
    if start.day == 1 and last_day.day >= 28 and last_day.month == start.month:
        return start.strftime("%B %Y")
    return f"{start.isoformat()} to {last_day.isoformat()}"


def _failing_servers() -> list[dict[str, Any]]:
    """Every server in a failing state, with its current verification if any.

    The section has to show both halves of the picture: what was justified and
    what was not. A missing compliance row means the engine treated the server
    as unknown, so it belongs in the failing list too. Only the active
    justification per server is joined; superseded and withdrawn records stay
    in the database for audit.
    """
    return fetch_all(
        """
        SELECT a.hostname, a.os_name, a.os_version, a.environment,
               a.business_group,
               COALESCE(c.state, 'unknown') AS state,
               COALESCE(c.reason, '') AS compliance_reason,
               COALESCE(c.total_patches, 0) AS total_patches,
               COALESCE(c.critical_count, 0) AS critical_count,
               j.id AS justification_id, j.reason_code, j.statement,
               j.remediation_plan, j.remediation_target, j.created_by,
               j.created_at,
               count(e.id) AS evidence_count,
               string_agg(e.filename, ', ' ORDER BY e.uploaded_at)
                   AS evidence_names
        FROM inventory.asset_effective a
        LEFT JOIN compliance.asset_compliance c ON c.asset_id = a.id
        LEFT JOIN inventory.asset_justifications j
               ON j.asset_id = a.id AND j.status = 'active'
        LEFT JOIN inventory.justification_evidence e
               ON e.justification_id = j.id
        WHERE a.in_scope AND a.lifecycle_status = 'active'
          AND (c.state IN ('non_compliant', 'stale', 'unknown')
               OR c.state IS NULL)
        GROUP BY a.hostname, a.os_name, a.os_version, a.environment,
                 a.business_group, c.state, c.reason, c.total_patches,
                 c.critical_count, j.id
        ORDER BY a.hostname
        """
    )


def _reason_labels(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Imported lazily: the reporting service does not otherwise depend on the
    # inventory package, and the label map is the only shared vocabulary.
    labels = {
        "accepted_risk": "Accepted risk",
        "compensating_control": "Compensating control in place",
        "scheduled_remediation": "Remediation scheduled",
        "false_positive": "False positive / assessment error",
        "decommissioning": "Asset being decommissioned",
        "collection_gap": "Collection or authentication gap",
        "investigation": "Under investigation",
    }
    for row in rows:
        row["reason_label"] = labels.get(row["reason_code"], row["reason_code"])
        names = row.get("evidence_names")
        row["evidence_names"] = [name.strip() for name in names.split(",")] if names else []
    return rows


def build_context(report: dict[str, Any]) -> ReportContext:
    snapshot = _load_snapshot_for(report)
    groups = _snapshot_groups(str(snapshot["id"]))
    period_label = _period_label(snapshot)
    organization = fetch_one(
        "SELECT value FROM shared.app_settings WHERE key = 'organization'"
    )
    org_name = "PatchTrack"
    logo_path = None
    logo_data = None
    organization_fields: dict[str, str] = {}
    if organization:
        value = organization["value"]
        if isinstance(value, str):
            import json
            value = json.loads(value)
        if isinstance(value, dict):
            org_name = value.get("name") or org_name
            # The logo is stored with the setting so every reporting replica
            # renders the same cover; a legacy path is still honoured for an
            # installation that had one before the uploader existed.
            logo_path = value.get("logo_path")
            logo_data = _decode_logo(value.get("logo_data"))
            organization_fields = {
                key: str(value.get(key) or "") for key in (
                    "preparer_name", "preparer_title", "concur_name", "concur_title",
                )
            }
    methodology = snapshot.get("methodology") or {}
    if isinstance(methodology, str):
        import json
        methodology = json.loads(methodology)
    return ReportContext(
        title=REPORT_TITLES.get(report["report_type"], "PatchTrack Report"),
        organization=org_name,
        classification="CONFIDENTIAL — INTERNAL USE ONLY",
        period_label=period_label,
        generated_at=datetime.now(UTC),
        timezone=timezone_settings.system_timezone(),
        generated_by=report["requested_by"],
        snapshot=snapshot,
        groups=groups,
        previous=_previous_snapshot(snapshot),
        methodology=methodology,
        freshness=_freshness(),
        integration_health=_integration_health(),
        top_non_compliant=_top_non_compliant(snapshot),
        findings=[],
        trend=_trend(),
        failing_servers=_reason_labels(_failing_servers()),
        logo_path=logo_path,
        logo_data=logo_data,
        **organization_fields,
    )


def _decode_logo(data_uri: Any) -> bytes | None:
    """Decode the stored data URI; a malformed value must not block a report."""
    if not isinstance(data_uri, str) or "," not in data_uri:
        return None
    try:
        import base64
        return base64.b64decode(data_uri.split(",", 1)[1], validate=True)
    except Exception:  # noqa: BLE001 - branding is optional
        log.warning("stored organisation logo could not be decoded")
        return None


def _target_path(report_id: str, report_type: str,
                 period_start) -> tuple[Path, str]:
    period = str(period_start)[:7]
    filename = f"patchtrack-{report_type}-{period}.pdf"
    directory = _report_dir()
    return directory / f"{report_id}.pdf", filename


def generate_report(report_id: str, *, deliver: bool = False) -> dict[str, Any]:
    with connection() as conn:
        report = conn.execute(
            text("SELECT * FROM reporting.reports WHERE id = :id"),
            {"id": report_id},
        ).mappings().first()
        if report is None:
            raise ReportError("Report not found")
        report = dict(report)
        if report["status"] == "completed":
            return report
        conn.execute(
            text("UPDATE reporting.reports SET status = 'generating', "
                 "started_at = now(), error_summary = '' WHERE id = :id"),
            {"id": report_id},
        )

    try:
        context = build_context(report)
        pdf_bytes = render_report(context)
        ok, problem = validate_pdf(pdf_bytes)
        if not ok:
            raise ReportError(problem)
        path, filename = _target_path(report_id, report["report_type"],
                                      report["period_start"])
        tmp = path.with_suffix(".pdf.tmp")
        tmp.write_bytes(pdf_bytes)
        os.chmod(tmp, 0o640)
        tmp.replace(path)
        digest = sha256_of(pdf_bytes)
        retention_days, _ = report_retention_days()
        with connection() as conn:
            conn.execute(
                text(
                    """
                    UPDATE reporting.reports SET
                        status = 'completed', completed_at = now(),
                        filename = :f, file_path = :p, sha256 = :h,
                        file_size = :s, page_count = :pages,
                        snapshot_id = :snap,
                        expires_at = now() + make_interval(days => :days)
                    WHERE id = :id
                    """
                ),
                {
                    "f": filename, "p": str(path), "h": digest,
                    "s": len(pdf_bytes),
                    "pages": pdf_bytes.count(b"/Type /Page") + 1,
                    "snap": context.snapshot["id"], "id": report_id,
                    "days": retention_days,
                },
            )
    except Exception as exc:  # noqa: BLE001 - failures are persisted, not raised
        log.exception("report generation failed")
        with connection() as conn:
            conn.execute(
                text(
                    "UPDATE reporting.reports SET status = 'failed', "
                    "completed_at = now(), error_summary = :e WHERE id = :id"
                ),
                {"e": str(exc)[:1500], "id": report_id},
            )
        return {"id": report_id, "status": "failed", "error": str(exc)}

    result = fetch_one("SELECT * FROM reporting.reports WHERE id = :id", id=report_id)
    if deliver and report.get("schedule_id"):
        schedule = fetch_one(
            "SELECT * FROM reporting.schedules WHERE id = :id",
            id=report["schedule_id"],
        )
        if schedule:
            deliver_report(report_id, schedule["recipients"])
    return result


def deliver_report(report_id: str, recipients: list[str]) -> dict[str, Any]:
    report = fetch_one(
        "SELECT * FROM reporting.reports WHERE id = :id", id=report_id
    )
    if report is None or report["status"] != "completed":
        return {"status": "skipped", "detail": "report not completed"}
    smtp = resolve_smtp()
    subject = (
        f"{REPORT_TITLES.get(report['report_type'], 'PatchTrack report')}: "
        f"{report['period_start']} to {report['period_end']}"
    )
    body = (
        f"Please find attached the {REPORT_TITLES.get(report['report_type'])} "
        f"covering {report['period_start']} to {report['period_end']}.\n\n"
        f"This report was generated by PatchTrack at {report['completed_at']}.\n"
        f"SHA-256: {report['sha256']}\n\n"
        f"Classification: CONFIDENTIAL — INTERNAL USE ONLY\n"
    )
    status, detail = send_report_email(
        recipients=[r for r in recipients or [] if r],
        subject=subject,
        body=body,
        pdf_path=Path(report["file_path"]),
        filename=report["filename"],
        config=smtp,
    )
    with connection() as conn:
        conn.execute(
            text(
                "INSERT INTO reporting.delivery_attempts "
                "(report_id, recipients, status, detail) "
                "VALUES (:r, :rec, :s, :d)"
            ),
            {"r": report_id, "rec": list(recipients or []), "s": status,
             "d": detail[:1000]},
        )
    log.info(
        "report %s delivery: %s (relay %s from %s)",
        report_id, status, smtp.host or "unset", smtp.source,
    )
    return {"status": status, "detail": detail}


def purge_expired() -> int:
    """Delete report files past retention, keeping a metadata tombstone.

    Reports completed before retention became a stored setting have no
    `expires_at`; their expiry is derived from the configured window and the
    completion date first, so the daily job enforces the policy for them too.
    """
    days, _ = report_retention_days()
    with connection() as conn:
        conn.execute(
            text(
                "UPDATE reporting.reports "
                "SET expires_at = completed_at + make_interval(days => :days) "
                "WHERE status = 'completed' AND completed_at IS NOT NULL "
                "  AND expires_at IS NULL"
            ),
            {"days": days},
        )
    expired = fetch_all(
        "SELECT id, file_path FROM reporting.reports "
        "WHERE expires_at IS NOT NULL AND expires_at < now() "
        "AND status = 'completed'"
    )
    removed_ids = []
    for row in expired:
        path = Path(row["file_path"])
        if path.exists():
            try:
                path.unlink()
            except OSError:
                continue
        removed_ids.append(row["id"])
    if removed_ids:
        # One statement rather than one connection per report; a retention
        # sweep can expire many files at once.
        with connection() as conn:
            conn.execute(
                text(
                    "UPDATE reporting.reports SET file_path = '', "
                    "error_summary = 'Purged by retention policy' "
                    "WHERE id = ANY(:ids)"
                ),
                {"ids": removed_ids},
            )
    return len(removed_ids)
