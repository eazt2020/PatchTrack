"""PatchTrack reporting service: report requests, archives, downloads,
schedules and the background job tick."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from patchtrack_common.audit import record_audit
from patchtrack_common.config import get_settings
from patchtrack_common.db import connection, fetch_all, fetch_one
from patchtrack_common.net import client_ip
from patchtrack_common.security import Principal, PrincipalDep, require_permission

from .scheduler import (
    claim_execution,
    compute_next_run,
    due_schedules,
    finish_execution,
    period_for_schedule,
)
from .service import (
    REPORT_TITLES,
    ReportError,
    deliver_report,
    generate_report,
    purge_expired,
)

APP = "reporting"
log = logging.getLogger(__name__)
app = FastAPI(title="PatchTrack Reporting", version="1.0.0", docs_url="/docs")


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
        start, end = period_bounds(payload.reporting_month, payload.quarter)
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


# --------------------------------------------------------------------------- schedules
class ScheduleCreate(BaseModel):
    name: str = Field(min_length=3, max_length=150)
    report_type: str
    cadence: str
    day_of_month: int = Field(default=1, ge=1, le=28)
    run_hour: int = Field(default=6, ge=0, le=23)
    timezone: str = "UTC"
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
    next_run = compute_next_run(
        payload.cadence, payload.day_of_month, payload.run_hour, payload.timezone
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
                "tz": payload.timezone, "rec": payload.recipients,
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


# --------------------------------------------------------------------------- scheduler tick
@app.post("/jobs/tick")
def scheduler_tick(
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("sync:run"))],
    background: BackgroundTasks,
) -> dict[str, Any]:
    """Called by the platform scheduler. Idempotent per schedule and period."""
    processed: list[dict[str, Any]] = []
    now = datetime.now(UTC)
    for schedule in due_schedules(now=now):
        start, end = period_for_schedule(schedule, now)
        next_run = compute_next_run(
            schedule["cadence"], schedule["day_of_month"], schedule["run_hour"],
            schedule["timezone"], now=now,
        )
        if not claim_execution(str(schedule["id"]), start.date()):
            finish_execution(str(schedule["id"]), start.date(), "skipped",
                             "Already executed for this period", next_run)
            processed.append({"schedule": schedule["name"], "status": "skipped"})
            continue
        snapshot = fetch_one(
            "SELECT id FROM compliance.snapshots WHERE period_start = :ps "
            "ORDER BY CASE scope WHEN 'monthly' THEN 0 WHEN 'quarterly' THEN 1 "
            "ELSE 3 END, captured_at DESC LIMIT 1",
            ps=start.date(),
        )
        if snapshot is None:
            finish_execution(str(schedule["id"]), start.date(), "failed",
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
                        "t": schedule["report_type"], "ps": start.date(),
                        "pe": end.date(),
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
            str(schedule["id"]), start.date(),
            "success" if result.get("status") == "completed" else "failed",
            f"report={report_id} delivery={delivery['status']}", next_run,
        )
        record_audit(
            event_type="schedule.executed",
            outcome="success" if result.get("status") == "completed" else "failure",
            principal=principal, target_type="schedule",
            target_id=str(schedule["id"]),
            context={"report_id": report_id, "delivery": delivery["status"]},
            source_ip=_client_ip(request), service=APP,
        )
        processed.append({
            "schedule": schedule["name"], "report_id": report_id,
            "status": result.get("status"), "delivery": delivery["status"],
        })
    purged = purge_expired()
    return {"processed": processed, "purged_reports": purged,
            "evaluated_at": now.isoformat()}


@app.post("/jobs/purge")
def purge(
    principal: Annotated[Principal, Depends(require_permission("settings:manage"))],
) -> dict[str, int]:
    return {"purged_reports": purge_expired()}
