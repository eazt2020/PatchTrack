"""PatchTrack reporting service: report requests, archives, downloads,
schedules and the background job tick."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from patchtrack_common import timezones as timezone_settings
from patchtrack_common.metrics import install as install_metrics
from patchtrack_common.request_guards import install as install_request_guards
from patchtrack_common.scheduler import (
    JOB_DEFINITIONS,
    SCHEDULER_PRINCIPAL,
    JobDefinition,
    ScheduledJob,
    install as install_scheduler,
)
from patchtrack_common.audit import record_audit
from patchtrack_common.config import get_settings
from patchtrack_common.db import connection, fetch_all, fetch_one
from patchtrack_common.net import client_ip
from patchtrack_common.security import Principal, PrincipalDep, require_permission

from .backups import (
    BACKUP_RETENTION_KEY,
    BACKUP_RETENTION_MAX,
    BACKUP_RETENTION_MIN,
    active_backup,
    archive_path,
    backup_settings_view,
    backup_stats,
    enqueue_backup,
    list_backups,
    queue_scheduled_backup,
    save_backup_retention,
    worker_status,
)
from .scheduler import (
    claim_execution,
    compute_next_run,
    due_schedules,
    finish_execution,
    period_for_schedule,
)
from .mailer import send_test_email
from .smtp_config import (
    SMTP_KEY,
    decrypt_password,
    encrypt_password,
    load_stored,
    resolve_smtp,
)
from .service import (
    REPORT_RETENTION_KEY,
    REPORT_RETENTION_MAX,
    REPORT_RETENTION_MIN,
    REPORT_TITLES,
    ReportError,
    deliver_report,
    generate_report,
    purge_expired,
    report_retention_days,
)

APP = "reporting"
log = logging.getLogger(__name__)
app = FastAPI(title="PatchTrack Reporting", version="1.0.0", docs_url="/docs")
install_metrics(app, APP)
install_request_guards(app)


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


def _report_view(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    path = row.get("file_path") or ""
    row["download_available"] = bool(path) and Path(path).exists()
    row.pop("file_path", None)
    return row


# ------------------------------------------------------------------- retention
class ReportRetentionUpdate(BaseModel):
    days: int = Field(ge=REPORT_RETENTION_MIN, le=REPORT_RETENTION_MAX)


@app.get("/settings/report-retention")
def get_report_retention(
    _: Annotated[Principal, Depends(require_permission("settings:manage"))],
) -> dict[str, Any]:
    days, row = report_retention_days()
    stats = fetch_one(
        "SELECT count(*) FILTER (WHERE status = 'completed') AS completed, "
        "       count(*) FILTER (WHERE status = 'completed' "
        "                        AND expires_at IS NOT NULL "
        "                        AND expires_at < now()) AS expired "
        "FROM reporting.reports"
    ) or {}
    return {
        "days": days,
        "min_days": REPORT_RETENTION_MIN,
        "max_days": REPORT_RETENTION_MAX,
        "updated_at": row["updated_at"] if row else None,
        "updated_by": row["updated_by"] if row else None,
        "completed": int(stats.get("completed") or 0),
        "expired": int(stats.get("expired") or 0),
    }


@app.put("/settings/report-retention")
def set_report_retention(
    payload: ReportRetentionUpdate,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("settings:manage"))],
) -> dict[str, Any]:
    """Save the window and apply it at once: every completed report's expiry is
    recomputed from its completion date, then the purge runs, so lowering the
    window removes already-expired files immediately rather than at the next
    scheduled tick."""
    import json

    with connection() as conn:
        conn.execute(
            text(
                "INSERT INTO shared.app_settings "
                "(key, value, updated_at, updated_by) "
                "VALUES (:key, CAST(:v AS jsonb), now(), :by) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
                "    updated_at = now(), updated_by = EXCLUDED.updated_by"
            ),
            {
                "key": REPORT_RETENTION_KEY,
                "v": json.dumps({"days": payload.days}),
                "by": principal.username,
            },
        )
        conn.execute(
            text(
                "UPDATE reporting.reports SET expires_at = "
                "CASE WHEN status = 'completed' AND completed_at IS NOT NULL "
                "     THEN completed_at + make_interval(days => :days) "
                "     ELSE NULL END"
            ),
            {"days": payload.days},
        )
    purged = purge_expired()
    record_audit(
        event_type="settings.updated", outcome="success", principal=principal,
        target_type="setting", target_id=REPORT_RETENTION_KEY,
        context={"days": payload.days, "purged_reports": purged},
        source_ip=_client_ip(request), service=APP,
    )
    return {"days": payload.days, "purged_reports": purged}


# ------------------------------------------------------------------------ SMTP
class SmtpSettingsUpdate(BaseModel):
    """Saved relay settings. The password is write-only: it is accepted here,
    encrypted at rest, and never returned by any endpoint."""

    host: str = Field("", max_length=255)
    port: int = Field(587, ge=1, le=65535)
    starttls: bool = True
    username: str = Field("", max_length=320)
    password: str | None = Field(None, max_length=1024)
    clear_password: bool = False
    sender: str = Field("", max_length=320)


class SmtpTestRequest(BaseModel):
    recipient: str = Field(min_length=3, max_length=320)


def _smtp_view() -> dict[str, Any]:
    config = resolve_smtp()
    stored = load_stored()
    token = str((stored or {}).get("password") or "")
    unreadable = bool(token) and decrypt_password(token) is None
    if stored is None:
        password_set = bool(config.password)
    else:
        password_set = bool(token) and not unreadable
    return {
        "host": config.host,
        "port": config.port,
        "starttls": config.starttls,
        "username": config.username,
        "sender": config.sender,
        "source": config.source,
        "password_set": password_set,
        "password_unreadable": unreadable,
        "updated_at": None,
        "updated_by": None,
    }


@app.get("/settings/smtp")
def get_smtp_settings(
    _: Annotated[Principal, Depends(require_permission("settings:manage"))],
) -> dict[str, Any]:
    view = _smtp_view()
    row = fetch_one(
        "SELECT updated_at, updated_by FROM shared.app_settings WHERE key = :key",
        key=SMTP_KEY,
    )
    if row:
        view["updated_at"] = row["updated_at"]
        view["updated_by"] = row["updated_by"]
    return view


@app.put("/settings/smtp")
def set_smtp_settings(
    payload: SmtpSettingsUpdate,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("settings:manage"))],
) -> dict[str, Any]:
    """Save the relay. The saved row is authoritative over the environment.

    Omitting the password keeps the one already saved (or, on the first save,
    carries over the environment password when the username is unchanged), so
    editing the host does not silently drop authentication.
    """
    import json

    host = payload.host.strip()
    sender = payload.sender.strip()
    username = payload.username.strip()
    if host and "@" not in sender:
        raise HTTPException(
            status_code=422, detail="A sender address is required"
        )
    if payload.clear_password and payload.password:
        raise HTTPException(
            status_code=422,
            detail="Provide a password or clear it, not both",
        )

    existing = load_stored() or {}
    token = str(existing.get("password") or "")
    password_changed = False
    if payload.clear_password:
        password_changed = bool(token)
        token = ""
    elif payload.password:
        token = encrypt_password(payload.password)
        password_changed = True
    elif not token:
        fallback = get_settings()
        if not host:
            token = ""
        elif fallback.smtp_password and username == fallback.smtp_username:
            # First save: keep the working password rather than quietly
            # switching the relay to unauthenticated delivery.
            token = encrypt_password(fallback.smtp_password)
            password_changed = True

    with connection() as conn:
        conn.execute(
            text(
                "INSERT INTO shared.app_settings "
                "(key, value, updated_at, updated_by) "
                "VALUES (:key, CAST(:v AS jsonb), now(), :by) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
                "    updated_at = now(), updated_by = EXCLUDED.updated_by"
            ),
            {
                "key": SMTP_KEY,
                "v": json.dumps(
                    {
                        "host": host,
                        "port": payload.port,
                        "starttls": payload.starttls,
                        "username": username,
                        "password": token,
                        "sender": sender,
                    }
                ),
                "by": principal.username,
            },
        )
    record_audit(
        event_type="settings.updated", outcome="success", principal=principal,
        target_type="setting", target_id=SMTP_KEY,
        context={
            "host": host,
            "port": payload.port,
            "starttls": payload.starttls,
            "sender": sender,
            "password_changed": password_changed,
        },
        source_ip=_client_ip(request), service=APP,
    )
    return _smtp_view()


@app.post("/settings/smtp/test")
def send_smtp_test(
    payload: SmtpTestRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("settings:manage"))],
) -> dict[str, Any]:
    """Send a test message using the saved (or environment) relay."""
    config = resolve_smtp()
    if not config.host:
        raise HTTPException(status_code=409, detail="SMTP is not configured")
    status, detail = send_test_email(recipient=payload.recipient, config=config)
    record_audit(
        event_type="settings.smtp_tested",
        outcome="success" if status == "sent" else "failure",
        principal=principal,
        target_type="setting", target_id=SMTP_KEY,
        context={
            "status": status,
            "host": config.host,
            "port": config.port,
            "source": config.source,
        },
        source_ip=_client_ip(request), service=APP,
    )
    if status != "sent":
        raise HTTPException(status_code=502, detail=detail or "Delivery failed")
    return {"status": status}


# --------------------------------------------------------------------------- reports
class ReportRequest(BaseModel):
    report_type: str
    reporting_month: str | None = None
    quarter: str | None = None
    snapshot_id: UUID | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    deliver: bool = False
    recipients: list[str] = Field(default_factory=list)


@app.post("/reports", status_code=202)
def request_report(
    payload: ReportRequest,
    background: BackgroundTasks,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("reports:generate"))],
) -> dict[str, Any]:
    if payload.report_type not in REPORT_TITLES:
        raise HTTPException(status_code=422, detail="Unknown report type")

    from .periods import period_bounds

    try:
        start, end = period_bounds(
            payload.reporting_month, payload.quarter,
            tz=timezone_settings.system_timezone())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if payload.snapshot_id:
        snapshot = fetch_one(
            "SELECT id FROM compliance.snapshots WHERE id = :id",
            id=payload.snapshot_id,
        )
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Snapshot not found")
    else:
        snapshot = fetch_one(
            """
            SELECT id FROM compliance.snapshots
            WHERE period_start = :ps ORDER BY
              CASE scope WHEN 'monthly' THEN 0 WHEN 'quarterly' THEN 1
                         WHEN 'on_demand' THEN 2 ELSE 3 END,
              captured_at DESC LIMIT 1
            """,
            # period_start is a calendar date. Passing a UTC datetime compares
            # against local midnight (date -> timestamptz), which is the
            # previous day in Asia/Kuala_Lumpur and matches nothing.
            ps=start.date(),
        )
        if snapshot is None:
            raise HTTPException(
                status_code=409,
                detail="No compliance snapshot exists for that period yet. "
                       "Capture the period snapshot first.",
            )

    import json

    with connection() as conn:
        row = conn.execute(
            text(
                """
                INSERT INTO reporting.reports (
                    report_type, period_start, period_end, filters, snapshot_id,
                    requested_by
                ) VALUES (:t, :ps, :pe, CAST(:f AS jsonb), :snap, :by)
                RETURNING id, status
                """
            ),
            {
                "t": payload.report_type, "ps": start.date(), "pe": end.date(),
                "f": json.dumps(payload.filters), "snap": snapshot["id"],
                "by": principal.username,
            },
        ).mappings().first()
    report_id = str(row["id"])
    record_audit(
        event_type="report.requested", outcome="success", principal=principal,
        target_type="report", target_id=report_id,
        context={"report_type": payload.report_type,
                 "period": str(start.date())},
        source_ip=_client_ip(request), service=APP,
    )
    background.add_task(_background_generate, report_id, payload.deliver,
                        payload.recipients)
    return {"id": report_id, "status": "queued"}


def _background_generate(report_id: str, deliver: bool,
                         recipients: list[str]) -> None:
    result = generate_report(report_id)
    if deliver and result.get("status") == "completed":
        deliver_report(report_id, recipients)


@app.get("/reports")
def list_reports(
    _: Annotated[Principal, Depends(require_permission("reports:view"))],
    report_type: str | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    clauses = ["1=1"]
    params: dict[str, Any] = {"l": limit, "o": offset}
    if report_type:
        clauses.append("report_type = :t")
        params["t"] = report_type
    if status:
        clauses.append("status = :s")
        params["s"] = status
    where = " AND ".join(clauses)
    rows = fetch_all(
        f"SELECT * FROM reporting.reports WHERE {where} "
        "ORDER BY requested_at DESC LIMIT :l OFFSET :o", **params,
    )
    total = fetch_one(
        f"SELECT count(*) AS n FROM reporting.reports WHERE {where}",
        **{k: v for k, v in params.items() if k not in ("l", "o")},
    )
    items = []
    for row in rows:
        view = _report_view(row)
        deliveries = fetch_all(
            "SELECT status, attempted_at, detail FROM reporting.delivery_attempts "
            "WHERE report_id = :id ORDER BY attempted_at DESC LIMIT 5",
            id=row["id"],
        )
        view["deliveries"] = deliveries
        items.append(view)
    return {"items": items, "total": total["n"] if total else 0}


@app.get("/reports/{report_id}")
def get_report(
    report_id: UUID,
    _: Annotated[Principal, Depends(require_permission("reports:view"))],
) -> dict[str, Any]:
    row = fetch_one("SELECT * FROM reporting.reports WHERE id = :id", id=report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Report not found")
    view = _report_view(row)
    view["deliveries"] = fetch_all(
        "SELECT status, attempted_at, detail FROM reporting.delivery_attempts "
        "WHERE report_id = :id ORDER BY attempted_at DESC",
        id=report_id,
    )
    return view


@app.get("/reports/{report_id}/download")
def download_report(
    report_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("reports:download"))],
):
    row = fetch_one(
        "SELECT * FROM reporting.reports WHERE id = :id", id=report_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Report not found")
    if row["status"] != "completed" or not row["file_path"]:
        raise HTTPException(status_code=409, detail="Report is not available")
    path = Path(row["file_path"]).resolve()
    report_root = Path(get_settings().report_dir).resolve()
    if not str(path).startswith(str(report_root) + "/") or not path.is_file():
        raise HTTPException(status_code=404, detail="Report file is unavailable")
    record_audit(
        event_type="report.downloaded", outcome="success", principal=principal,
        target_type="report", target_id=str(report_id),
        source_ip=_client_ip(request), service=APP,
    )
    return FileResponse(
        path, media_type="application/pdf", filename=row["filename"],
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/reports/{report_id}/retry")
def retry_report(
    report_id: UUID,
    background: BackgroundTasks,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("reports:generate"))],
) -> dict[str, str]:
    row = fetch_one("SELECT status FROM reporting.reports WHERE id = :id",
                    id=report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Report not found")
    if row["status"] not in ("failed", "queued"):
        raise HTTPException(status_code=409, detail="Only failed reports can be retried")
    with connection() as conn:
        conn.execute(
            text("UPDATE reporting.reports SET status = 'queued', "
                 "error_summary = '' WHERE id = :id"),
            {"id": report_id},
        )
    record_audit(
        event_type="report.retried", outcome="success", principal=principal,
        target_type="report", target_id=str(report_id),
        source_ip=_client_ip(request), service=APP,
    )
    background.add_task(generate_report, str(report_id))
    return {"status": "queued"}


class ReportDeleteRequest(BaseModel):
    ids: list[UUID] = Field(min_length=1, max_length=200)


def _delete_reports(ids: list[UUID], principal: Principal,
                    request: Request) -> dict[str, Any]:
    """Remove archived reports and their PDFs.

    Only an administrator holds `reports:delete`. The state is deliberately not
    inferred: a report that is still queued or generating is refused rather
    than deleted underneath the worker. The row is removed only after the file
    is gone (or already absent), so a failed unlink never leaves a report whose
    archive is neither downloadable nor deletable.
    """
    wanted = list(dict.fromkeys(ids))
    rows = fetch_all(
        "SELECT id, status, report_type, period_start, filename, file_path, "
        "       requested_by FROM reporting.reports WHERE id = ANY(:ids)",
        ids=wanted,
    )
    found = {str(row["id"]): row for row in rows}
    report_root = Path(get_settings().report_dir).resolve()

    deleted: list[str] = []
    refused: list[dict[str, str]] = []
    missing: list[str] = []
    for report_id in wanted:
        key = str(report_id)
        row = found.get(key)
        if row is None:
            missing.append(key)
            continue
        if row["status"] in ("queued", "generating"):
            refused.append({
                "id": key,
                "reason": "Report is still being generated",
            })
            continue
        path = Path(row["file_path"]).resolve() if row["file_path"] else None
        if path is not None and path.exists():
            # Never unlink outside the archive volume, even if a row was
            # tampered with.
            if not str(path).startswith(str(report_root) + "/"):
                refused.append({
                    "id": key,
                    "reason": "Report file is outside the archive directory",
                })
                continue
            try:
                path.unlink()
            except OSError:
                refused.append({
                    "id": key,
                    "reason": "Could not remove the report file",
                })
                continue
        with connection() as conn:
            conn.execute(
                text("DELETE FROM reporting.reports WHERE id = :id"),
                {"id": report_id},
            )
        deleted.append(key)
        record_audit(
            event_type="report.deleted", outcome="success", principal=principal,
            target_type="report", target_id=key,
            context={
                "report_type": row["report_type"],
                "period_start": str(row["period_start"]),
                "filename": row["filename"],
                "requested_by": row["requested_by"],
            },
            source_ip=_client_ip(request), service=APP,
        )

    outcome = "success" if not refused and not missing else "failure"
    record_audit(
        event_type="report.bulk_deleted", outcome=outcome, principal=principal,
        target_type="report_batch", target_id="",
        context={"requested": len(wanted), "deleted": len(deleted),
                 "refused": len(refused), "not_found": len(missing)},
        source_ip=_client_ip(request), service=APP,
    )
    return {
        "deleted": deleted,
        "refused": refused,
        "not_found": missing,
        "count": len(deleted),
    }


@app.post("/reports/bulk-delete")
def bulk_delete_reports(
    payload: ReportDeleteRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("reports:delete"))],
) -> dict[str, Any]:
    return _delete_reports(payload.ids, principal, request)


@app.delete("/reports/{report_id}")
def delete_report(
    report_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("reports:delete"))],
) -> dict[str, Any]:
    result = _delete_reports([report_id], principal, request)
    if result["not_found"]:
        raise HTTPException(status_code=404, detail="Report not found")
    if result["refused"]:
        raise HTTPException(status_code=409, detail=result["refused"][0]["reason"])
    return result


# --------------------------------------------------------------------------- schedules
class ScheduleCreate(BaseModel):
    name: str = Field(min_length=3, max_length=150)
    report_type: str
    cadence: str
    day_of_month: int = Field(default=1, ge=1, le=28)
    run_hour: int = Field(default=6, ge=0, le=23)
    timezone: str | None = None
    recipients: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


@app.get("/schedules")
def list_schedules(
    _: Annotated[Principal, Depends(require_permission("reports:view"))],
) -> list[dict[str, Any]]:
    return fetch_all("SELECT * FROM reporting.schedules ORDER BY name")


@app.post("/schedules", status_code=201)
def create_schedule(
    payload: ScheduleCreate,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("schedules:manage"))],
) -> dict[str, Any]:
    if payload.report_type not in ("monthly_management", "quarterly_compliance"):
        raise HTTPException(status_code=422, detail="Unsupported scheduled report type")
    if payload.cadence not in ("monthly", "quarterly"):
        raise HTTPException(status_code=422, detail="cadence must be monthly or quarterly")
    try:
        schedule_tz = timezone_settings.validate_timezone(
            payload.timezone or timezone_settings.system_timezone())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    next_run = compute_next_run(
        payload.cadence, payload.day_of_month, payload.run_hour, schedule_tz
    )
    with connection() as conn:
        row = conn.execute(
            text(
                """
                INSERT INTO reporting.schedules (
                    name, report_type, cadence, day_of_month, run_hour, timezone,
                    recipients, filters, enabled, next_run_at, created_by
                ) VALUES (:n, :t, :c, :d, :h, :tz, :rec, CAST(:f AS jsonb), :e,
                          :next, :by)
                RETURNING *
                """
            ),
            {
                "n": payload.name, "t": payload.report_type, "c": payload.cadence,
                "d": payload.day_of_month, "h": payload.run_hour,
                "tz": schedule_tz, "rec": payload.recipients,
                "f": __import__("json").dumps(payload.filters), "e": payload.enabled,
                "next": next_run, "by": principal.username,
            },
        ).mappings().first()
    record_audit(
        event_type="schedule.created", outcome="success", principal=principal,
        target_type="schedule", target_id=str(row["id"]),
        context={"name": payload.name, "cadence": payload.cadence},
        source_ip=_client_ip(request), service=APP,
    )
    return dict(row)


class ScheduleUpdate(BaseModel):
    name: str | None = None
    day_of_month: int | None = Field(default=None, ge=1, le=28)
    run_hour: int | None = Field(default=None, ge=0, le=23)
    timezone: str | None = None
    recipients: list[str] | None = None
    filters: dict[str, Any] | None = None
    enabled: bool | None = None


@app.patch("/schedules/{schedule_id}")
def update_schedule(
    schedule_id: UUID,
    payload: ScheduleUpdate,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("schedules:manage"))],
) -> dict[str, Any]:
    existing = fetch_one(
        "SELECT * FROM reporting.schedules WHERE id = :id", id=schedule_id
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No changes supplied")
    merged = {**existing, **changes}
    changes["next_run_at"] = compute_next_run(
        merged["cadence"], merged["day_of_month"], merged["run_hour"],
        merged["timezone"],
    )
    import json

    assignments = []
    params: dict[str, Any] = {"id": schedule_id}
    for field, value in changes.items():
        if field == "filters":
            assignments.append("filters = CAST(:filters AS jsonb)")
            params["filters"] = json.dumps(value)
        else:
            assignments.append(f"{field} = :{field}")
            params[field] = value
    with connection() as conn:
        row = conn.execute(
            text(
                f"UPDATE reporting.schedules SET {', '.join(assignments)}, "
                "updated_at = now() WHERE id = :id RETURNING *"
            ),
            params,
        ).mappings().first()
    record_audit(
        event_type="schedule.updated", outcome="success", principal=principal,
        target_type="schedule", target_id=str(schedule_id),
        context={"fields": sorted(changes)}, source_ip=_client_ip(request),
        service=APP,
    )
    return dict(row)


@app.delete("/schedules/{schedule_id}")
def delete_schedule(
    schedule_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("schedules:manage"))],
) -> dict[str, str]:
    with connection() as conn:
        removed = conn.execute(
            text("DELETE FROM reporting.schedules WHERE id = :id"),
            {"id": schedule_id},
        ).rowcount
    if not removed:
        raise HTTPException(status_code=404, detail="Schedule not found")
    record_audit(
        event_type="schedule.deleted", outcome="success", principal=principal,
        target_type="schedule", target_id=str(schedule_id),
        source_ip=_client_ip(request), service=APP,
    )
    return {"status": "deleted"}


# --------------------------------------------------------------------------- system jobs
class SchedulerJobUpdate(BaseModel):
    enabled: bool | None = None
    interval_seconds: int | None = Field(None, ge=10, le=31_536_000)


def _job_view(
    definition: JobDefinition | None, row: dict[str, Any] | None,
    default_interval: int,
) -> dict[str, Any]:
    row = row or {}
    return {
        "name": definition.name if definition else row["name"],
        "label": definition.label if definition else row["name"],
        "description": (
            definition.description if definition
            else "This job is not defined by the current release."
        ),
        "interval_seconds": int(row.get("interval_seconds") or default_interval),
        "default_interval_seconds": default_interval,
        "enabled": bool(row["enabled"]) if "enabled" in row else True,
        "next_run_at": row.get("next_run_at"),
        "last_started_at": row.get("last_started_at"),
        "last_finished_at": row.get("last_finished_at"),
        "last_status": row.get("last_status"),
        "last_message": row.get("last_message") or "",
        "updated_at": row.get("updated_at"),
        "updated_by": row.get("updated_by") or "",
    }


def scheduler_job_views() -> list[dict[str, Any]]:
    """Every known job, including ones that have never run.

    The registry supplies the label, description and environment default, so
    the screen is complete immediately after installation. A row left by an
    older release is still shown rather than silently hidden.
    """
    rows = {
        row["name"]: row
        for row in fetch_all("SELECT * FROM shared.scheduled_jobs ORDER BY name")
    }
    settings = get_settings()
    known = {definition.name for definition in JOB_DEFINITIONS}
    views = [
        _job_view(
            definition, rows.get(definition.name),
            int(getattr(settings, definition.interval_setting)),
        )
        for definition in JOB_DEFINITIONS
    ]
    for name, row in rows.items():
        if name not in known:
            views.append(_job_view(None, row, int(row["interval_seconds"])))
    return views


@app.get("/scheduler/jobs")
def list_scheduler_jobs(
    _: Annotated[Principal, Depends(require_permission("reports:view"))],
) -> list[dict[str, Any]]:
    return scheduler_job_views()


@app.patch("/scheduler/jobs/{name}")
def update_scheduler_job(
    name: str,
    payload: SchedulerJobUpdate,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("settings:manage"))],
) -> dict[str, Any]:
    """Change one job's interval or enabled flag.

    The row is the authoritative configuration from then on; the environment
    variable is only the default used before a row exists. A new interval takes
    effect from now, so setting six hours schedules the next run six hours out
    rather than inheriting an old due time.
    """
    definition = next((d for d in JOB_DEFINITIONS if d.name == name), None)
    if definition is None:
        raise HTTPException(status_code=404, detail="Unknown scheduled job")
    if payload.enabled is None and payload.interval_seconds is None:
        raise HTTPException(status_code=422, detail="Nothing to update")

    current = fetch_one(
        "SELECT * FROM shared.scheduled_jobs WHERE name = :name", name=name
    )
    default_interval = int(getattr(get_settings(), definition.interval_setting))
    current_interval = int(
        current["interval_seconds"] if current else default_interval
    )
    current_enabled = bool(current["enabled"]) if current else True
    interval = (
        payload.interval_seconds
        if payload.interval_seconds is not None else current_interval
    )
    enabled = payload.enabled if payload.enabled is not None else current_enabled
    reset_due = interval != current_interval or (enabled and not current_enabled)
    next_run = (
        datetime.now(UTC) + timedelta(seconds=interval)
        if reset_due else (current["next_run_at"] if current else None)
    )

    with connection() as conn:
        conn.execute(
            text(
                """
                INSERT INTO shared.scheduled_jobs (
                    name, interval_seconds, enabled, next_run_at,
                    updated_by, updated_at
                ) VALUES (:name, :interval, :enabled, :next_run, :by, now())
                ON CONFLICT (name) DO UPDATE SET
                    interval_seconds = EXCLUDED.interval_seconds,
                    enabled = EXCLUDED.enabled,
                    next_run_at = EXCLUDED.next_run_at,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = now()
                """
            ),
            {
                "name": name, "interval": interval, "enabled": enabled,
                "next_run": next_run, "by": principal.username,
            },
        )
    record_audit(
        event_type="scheduler.updated", outcome="success", principal=principal,
        target_type="scheduled_job", target_id=name,
        context={"enabled": enabled, "interval_seconds": interval},
        source_ip=_client_ip(request), service=APP,
    )
    row = fetch_one(
        "SELECT * FROM shared.scheduled_jobs WHERE name = :name", name=name
    )
    return _job_view(definition, row, default_interval)


# --------------------------------------------------------------------------- data backups
class BackupRetentionUpdate(BaseModel):
    count: int = Field(ge=BACKUP_RETENTION_MIN, le=BACKUP_RETENTION_MAX)


@app.get("/backups")
def get_backups(
    _: Annotated[Principal, Depends(require_permission("settings:manage"))],
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    worker = worker_status()
    return {
        "items": list_backups(limit),
        "active": active_backup(),
        "worker": worker,
        "stats": backup_stats(),
        "app_version": get_settings().version or worker.get("version") or "unknown",
    }


@app.post("/backups", status_code=202)
def create_backup(
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("settings:manage"))],
) -> dict[str, Any]:
    """Queue an archive now. One active backup at a time is deliberate: two
    dumps of the same database only waste disk and make the newest archive
    ambiguous."""
    queued, active = enqueue_backup("manual", principal.username)
    if queued is None:
        current = active["status"] if active else "active"
        raise HTTPException(
            status_code=409,
            detail=f"A backup is already {current}. Wait for it to finish.",
        )
    record_audit(
        event_type="backup.requested", outcome="success", principal=principal,
        target_type="backup", target_id=str(queued["id"]),
        source_ip=_client_ip(request), service=APP,
    )
    return {"id": str(queued["id"]), "status": "queued"}


@app.get("/backups/{backup_id}/download")
def download_backup(
    backup_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("settings:manage"))],
):
    row = fetch_one(
        "SELECT id, status, filename, sha256 FROM shared.backup_runs "
        "WHERE id = :id", id=backup_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Backup not found")
    if row["status"] != "completed":
        raise HTTPException(status_code=409, detail="Backup is not complete")
    path = archive_path(row["filename"])
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Backup archive is unavailable")
    record_audit(
        event_type="backup.downloaded", outcome="success", principal=principal,
        target_type="backup", target_id=str(backup_id),
        source_ip=_client_ip(request), service=APP,
    )
    headers = {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if row["sha256"]:
        headers["X-PatchTrack-SHA256"] = row["sha256"]
    return FileResponse(
        path, media_type="application/gzip", filename=row["filename"],
        headers=headers,
    )


@app.get("/settings/backup")
def get_backup_settings(
    _: Annotated[Principal, Depends(require_permission("settings:manage"))],
) -> dict[str, Any]:
    return backup_settings_view()


@app.put("/settings/backup")
def set_backup_settings(
    payload: BackupRetentionUpdate,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("settings:manage"))],
) -> dict[str, Any]:
    """Save the archive count. The worker applies it after the next successful
    archive rather than deleting files while it may be writing a new one."""
    save_backup_retention(payload.count, principal.username)
    record_audit(
        event_type="settings.updated", outcome="success", principal=principal,
        target_type="setting", target_id=BACKUP_RETENTION_KEY,
        context={"retention": payload.count},
        source_ip=_client_ip(request), service=APP,
    )
    view = backup_settings_view()
    view["applies_after_next_backup"] = True
    return view


# --------------------------------------------------------------------------- scheduler tick
def execute_due_schedules(
    principal: Principal, *, source_ip: str | None = None
) -> dict[str, Any]:
    """Generate reports for schedules that are due, then apply retention.

    Idempotent per schedule and period, so a second replica, a retried run and
    a manual tick can all call it without producing duplicate reports. Shared
    by the in-app scheduler and ``POST /jobs/tick``.
    """
    processed: list[dict[str, Any]] = []
    now = datetime.now(UTC)
    for schedule in due_schedules(now=now):
        start, end = period_for_schedule(schedule, now)
        next_run = compute_next_run(
            schedule["cadence"], schedule["day_of_month"], schedule["run_hour"],
            schedule["timezone"], now=now,
        )
        if not claim_execution(str(schedule["id"]), start):
            finish_execution(str(schedule["id"]), start, "skipped",
                             "Already executed for this period", next_run)
            processed.append({"schedule": schedule["name"], "status": "skipped"})
            continue
        snapshot = fetch_one(
            "SELECT id FROM compliance.snapshots WHERE period_start = :ps "
            "ORDER BY CASE scope WHEN 'monthly' THEN 0 WHEN 'quarterly' THEN 1 "
            "ELSE 3 END, captured_at DESC LIMIT 1",
            ps=start,
        )
        if snapshot is None:
            finish_execution(str(schedule["id"]), start, "failed",
                             "No snapshot for period", next_run)
            processed.append({"schedule": schedule["name"],
                              "status": "failed", "reason": "no snapshot"})
            continue
        with connection() as conn:
            report_id = str(
                conn.execute(
                    text(
                        """
                        INSERT INTO reporting.reports (
                            report_type, period_start, period_end, filters,
                            snapshot_id, requested_by, schedule_id
                        ) VALUES (:t, :ps, :pe, CAST(:f AS jsonb), :snap,
                                  :by, :sid) RETURNING id
                        """
                    ),
                    {
                        "t": schedule["report_type"], "ps": start,
                        "pe": end,
                        "f": __import__("json").dumps(schedule["filters"]),
                        "snap": snapshot["id"],
                        "by": f"schedule:{schedule['name']}",
                        "sid": schedule["id"],
                    },
                ).first()[0]
            )
        result = generate_report(report_id)
        delivery = (
            deliver_report(report_id, schedule["recipients"])
            if result.get("status") == "completed" else
            {"status": "skipped", "detail": "generation failed"}
        )
        finish_execution(
            str(schedule["id"]), start,
            "success" if result.get("status") == "completed" else "failed",
            f"report={report_id} delivery={delivery['status']}", next_run,
        )
        record_audit(
            event_type="schedule.executed",
            outcome="success" if result.get("status") == "completed" else "failure",
            principal=principal, target_type="schedule",
            target_id=str(schedule["id"]),
            context={"report_id": report_id, "delivery": delivery["status"]},
            source_ip=source_ip, service=APP,
        )
        processed.append({
            "schedule": schedule["name"], "report_id": report_id,
            "status": result.get("status"), "delivery": delivery["status"],
        })
    return {"status": "success", "processed": processed,
            "evaluated_at": now.isoformat()}


@app.post("/jobs/tick")
def scheduler_tick(
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("sync:run"))],
) -> dict[str, Any]:
    """Manual/internal trigger. The in-app scheduler calls the same code."""
    return execute_due_schedules(principal, source_ip=_client_ip(request))


@app.post("/jobs/purge")
def purge(
    principal: Annotated[Principal, Depends(require_permission("settings:manage"))],
) -> dict[str, int]:
    return {"purged_reports": purge_expired()}


# --------------------------------------------------------------------------- in-app scheduler
install_scheduler(app, [
    ScheduledJob(
        name="reporting.tick",
        interval_seconds=get_settings().report_tick_interval,
        run=lambda: execute_due_schedules(SCHEDULER_PRINCIPAL),
    ),
    # Retention is a filesystem sweep, not schedule evaluation. It runs daily
    # rather than on every tick so the report table is not scanned once a
    # minute for work that is almost never there.
    ScheduledJob(
        name="reporting.retention",
        interval_seconds=get_settings().report_purge_interval,
        run=lambda: {
            "status": "success", "purged_reports": purge_expired(),
        },
    ),
    # Queueing is fast and idempotent; the worker container does the dump and
    # applies retention. A backup already in progress is reported as skipped
    # work, not a failed job.
    ScheduledJob(
        name="reporting.backup",
        interval_seconds=get_settings().backup_interval,
        run=queue_scheduled_backup,
    ),
])
