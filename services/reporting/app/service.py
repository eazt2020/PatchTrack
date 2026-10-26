"""Report generation orchestration shared by the API and the scheduler."""

from __future__ import annotations

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

log = logging.getLogger(__name__)

REPORT_TITLES = {
    "monthly_management": "Monthly Patch Management Report",
    "quarterly_compliance": "Quarterly Patch Compliance Report",
    "inventory_register": "Server Inventory Register",
}


class ReportError(RuntimeError):
    pass


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


def build_context(report: dict[str, Any]) -> ReportContext:
    settings = get_settings()
    snapshot = _load_snapshot_for(report)
    groups = _snapshot_groups(str(snapshot["id"]))
    period_label = _period_label(snapshot)
    organization = fetch_one(
        "SELECT value FROM shared.app_settings WHERE key = 'organization'"
    )
    org_name = "PatchTrack"
    logo_path = None
    if organization:
        value = organization["value"]
        if isinstance(value, dict):
            org_name = value.get("name") or org_name
            logo_path = value.get("logo_path")
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
        logo_path=logo_path,
    )


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
        with connection() as conn:
            conn.execute(
                text(
                    """
                    UPDATE reporting.reports SET
                        status = 'completed', completed_at = now(),
                        filename = :f, file_path = :p, sha256 = :h,
                        file_size = :s, page_count = :pages,
                        snapshot_id = :snap
                    WHERE id = :id
                    """
                ),
                {
                    "f": filename, "p": str(path), "h": digest,
                    "s": len(pdf_bytes),
                    "pages": pdf_bytes.count(b"/Type /Page") + 1,
                    "snap": context.snapshot["id"], "id": report_id,
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
    settings = get_settings()
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
    log.info("report %s delivery: %s (%s)", report_id, status, settings.smtp_host)
    return {"status": status, "detail": detail}


def purge_expired() -> int:
    """Delete report files past retention, keeping a metadata tombstone."""
    expired = fetch_all(
        "SELECT id, file_path FROM reporting.reports "
        "WHERE expires_at IS NOT NULL AND expires_at < now() "
        "AND status = 'completed'"
    )
    removed = 0
    for row in expired:
        path = Path(row["file_path"])
        if path.exists():
            try:
                path.unlink()
            except OSError:
                continue
        with connection() as conn:
            conn.execute(
                text("UPDATE reporting.reports SET file_path = '', "
                     "error_summary = 'Purged by retention policy' WHERE id = :id"),
                {"id": row["id"]},
            )
        removed += 1
    return removed
