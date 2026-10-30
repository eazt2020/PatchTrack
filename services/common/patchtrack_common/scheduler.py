"""In-app background scheduler.

Every service owns the jobs that belong to it: inventory collects from its
sources, compliance takes the daily snapshot, reporting evaluates report
schedules and applies retention, and identity purges the audit trail. The
scheduler runs inside the service process, so there is no systemd timer, cron
entry or shell runner to install and keep in step with the application.

Two properties matter for a job runner embedded in a web service:

* **Durability.** The next due time is stored in ``shared.scheduled_jobs``,
  so restarting a service does not replay every long-interval job.
* **Single execution.** A PostgreSQL advisory lock is held for the duration
  of a run. Two replicas may both poll, but only one executes a given job;
  the lock is released automatically if the process dies.

Jobs run in a worker thread because the service's work is synchronous
(connector clients, ReportLab, SMTP). The event loop keeps serving requests.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from .config import get_settings
from .db import get_engine
from .security import Principal

log = logging.getLogger("patchtrack.scheduler")

# Namespace for the two-key advisory lock. Arbitrary but stable across
# releases; changing it would let a new version run concurrently with an old
# one during a rolling deployment.
_LOCK_NAMESPACE = 20564


@dataclass(frozen=True)
class ScheduledJob:
    """One recurring job owned by a service.

    ``interval_seconds`` is the normal cadence. A failure is retried after
    ``retry_seconds`` so a transient source-system outage recovers quickly
    instead of waiting hours for the next scheduled attempt.
    """

    name: str
    interval_seconds: int
    run: Callable[[], Any]
    retry_seconds: int = 300


SCHEDULER_PRINCIPAL = Principal(
    user_id="",
    username="scheduler",
    role="administrator",
    display_name="PatchTrack scheduler",
    auth_method="scheduler",
)


@dataclass(frozen=True)
class JobDefinition:
    """A job the scheduler knows about, including how it is presented.

    The registry is used by the Schedules screen to list every job before it
    has ever run and to show the environment default for its interval. The
    service that owns the job still decides what the job does.
    """

    name: str
    label: str
    description: str
    interval_setting: str


JOB_DEFINITIONS: tuple[JobDefinition, ...] = (
    JobDefinition(
        "inventory.assetexplorer", "AssetExplorer collection",
        "Imports servers and their patch state from AssetExplorer.",
        "sync_assetexplorer_interval",
    ),
    JobDefinition(
        "inventory.satellite", "Satellite collection",
        "Imports patch assessments from Satellite.",
        "sync_satellite_interval",
    ),
    JobDefinition(
        "inventory.sccm", "SCCM collection",
        "Imports patch assessments from SCCM.",
        "sync_sccm_interval",
    ),
    JobDefinition(
        "inventory.solaris", "Solaris collection",
        "Processes approved Solaris assessment files from the inbox.",
        "sync_solaris_interval",
    ),
    JobDefinition(
        "compliance.daily", "Daily compliance snapshot",
        "Captures the daily snapshot and refreshes every asset assessment.",
        "daily_snapshot_interval",
    ),
    JobDefinition(
        "identity.audit_purge", "Audit retention purge",
        "Deletes audit events older than the configured retention window.",
        "audit_purge_interval",
    ),
    JobDefinition(
        "reporting.tick", "Report schedules",
        "Generates and delivers reports whose schedule is due.",
        "report_tick_interval",
    ),
    JobDefinition(
        "reporting.retention", "Report file retention",
        "Deletes archived report files past their retention date.",
        "report_purge_interval",
    ),
    JobDefinition(
        "reporting.backup", "Data backup archive",
        "Queues a database and report-file archive for disaster recovery. "
        "The worker applies the retention count after each archive.",
        "backup_interval",
    ),
)


def _is_due(conn: Any, name: str) -> bool:
    row = conn.execute(
        text(
            "SELECT next_run_at IS NULL OR next_run_at <= now() AS due "
            "FROM shared.scheduled_jobs WHERE name = :name"
        ),
        {"name": name},
    ).first()
    # No row means this job has never run on this installation.
    return row is None or bool(row[0])


def _configuration(conn: Any, job: ScheduledJob) -> tuple[bool, int]:
    """The enabled flag and interval currently configured for a job.

    The database row is authoritative once it exists: the Schedules screen
    edits it, and the environment value is only the initial default.
    """
    row = conn.execute(
        text(
            "SELECT enabled, interval_seconds FROM shared.scheduled_jobs "
            "WHERE name = :name"
        ),
        {"name": job.name},
    ).first()
    if row is None:
        return True, job.interval_seconds
    return bool(row[0]), int(row[1])


def _mark_started(conn: Any, job: ScheduledJob) -> None:
    conn.execute(
        text(
            """
            INSERT INTO shared.scheduled_jobs
                (name, interval_seconds, last_started_at, updated_at)
            VALUES (:name, :interval, now(), now())
            ON CONFLICT (name) DO UPDATE SET
                last_started_at = now(),
                updated_at = now()
            """
        ),
        {"name": job.name, "interval": job.interval_seconds},
    )


def _mark_finished(conn: Any, job: ScheduledJob, *, status: str,
                   message: str, next_run: datetime) -> None:
    conn.execute(
        text(
            """
            UPDATE shared.scheduled_jobs SET
                next_run_at = :next_run,
                last_finished_at = now(),
                last_status = :status,
                last_message = :message,
                updated_at = now()
            WHERE name = :name
            """
        ),
        {
            "name": job.name,
            "next_run": next_run,
            "status": status,
            "message": message[:1000],
        },
    )


def _message_for(result: Any) -> str:
    if isinstance(result, dict):
        try:
            return json.dumps(result, default=str)[:1000]
        except (TypeError, ValueError):
            return str(result)[:1000]
    return str(result or "")[:1000]


def _failed(result: Any) -> bool:
    """Whether a returned result reports failure.

    Jobs return a dict in the same shape the old HTTP job endpoints used. The
    scheduler treats only an explicit ``status == "failed"`` as failure;
    ``partial`` and ``skipped`` are completed runs, as they were for the
    shell runner.
    """
    return isinstance(result, dict) and result.get("status") == "failed"


def due_jobs(jobs: Iterable[ScheduledJob]) -> list[ScheduledJob]:
    """Return the jobs whose persisted next-run time has passed.

    One query for the whole service, rather than one connection per job on
    every poll. A job with no row has never run and is due.
    """
    jobs = list(jobs)
    if not jobs:
        return []
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT name, enabled, "
                "       next_run_at IS NULL OR next_run_at <= now() AS due "
                "FROM shared.scheduled_jobs WHERE name = ANY(:names)"
            ),
            {"names": [job.name for job in jobs]},
        ).all()
    state = {row[0]: (bool(row[1]), bool(row[2])) for row in rows}
    # A job with no row has never run and is enabled by default.
    return [
        job for job in jobs
        if state.get(job.name, (True, True)) == (True, True)
    ]


def run_scheduled_job(job: ScheduledJob, *,
                      retry_seconds: int = 300) -> dict[str, Any]:
    """Run one job if it is due and no other instance holds it.

    Returns a small result dictionary for logs and tests. This function is
    synchronous: the async loop calls it in a worker thread.
    """
    engine = get_engine()
    with engine.connect() as conn:
        if not _is_due(conn, job.name):
            return {"job": job.name, "status": "not_due"}

        acquired = conn.execute(
            text(
                "SELECT pg_try_advisory_lock(:namespace, hashtext(:name))"
            ),
            {"namespace": _LOCK_NAMESPACE, "name": job.name},
        ).scalar()
        if not acquired:
            return {"job": job.name, "status": "locked"}

        try:
            # Another replica may have finished while we waited for the lock.
            if not _is_due(conn, job.name):
                return {"job": job.name, "status": "not_due"}
            enabled, interval = _configuration(conn, job)
            if not enabled:
                return {"job": job.name, "status": "disabled"}
            _mark_started(conn, job)
            conn.commit()

            try:
                result = job.run()
            except Exception as exc:  # noqa: BLE001 - one job must not kill the loop
                log.exception("scheduled job %s failed", job.name)
                next_run = datetime.now(UTC) + timedelta(seconds=retry_seconds)
                _mark_finished(
                    conn, job, status="failure",
                    message=f"{type(exc).__name__}: {exc}", next_run=next_run,
                )
                conn.commit()
                return {"job": job.name, "status": "failure", "error": str(exc)}

            status = "failure" if _failed(result) else "success"
            delay = retry_seconds if status == "failure" else interval
            next_run = datetime.now(UTC) + timedelta(seconds=delay)
            _mark_finished(
                conn, job, status=status, message=_message_for(result),
                next_run=next_run,
            )
            conn.commit()
            return {"job": job.name, "status": status, "result": result}
        finally:
            conn.execute(
                text(
                    "SELECT pg_advisory_unlock(:namespace, hashtext(:name))"
                ),
                {"namespace": _LOCK_NAMESPACE, "name": job.name},
            )
            conn.commit()


class InAppScheduler:
    """Polls this service's jobs and runs whatever is due."""

    def __init__(self, jobs: Iterable[ScheduledJob], *,
                 poll_seconds: int = 15, retry_seconds: int = 300,
                 startup_delay_seconds: int = 10) -> None:
        self.jobs = list(jobs)
        self.poll_seconds = poll_seconds
        self.retry_seconds = retry_seconds
        self.startup_delay_seconds = startup_delay_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def _run_forever(self) -> None:
        try:
            await asyncio.wait_for(
                self._stop.wait(), timeout=self.startup_delay_seconds
            )
            return  # stopped before the first pass
        except TimeoutError:
            pass

        while not self._stop.is_set():
            try:
                ready = await asyncio.to_thread(due_jobs, self.jobs)
            except Exception:  # noqa: BLE001 - the loop must survive
                log.exception("scheduler could not read due state")
                ready = []
            for job in ready:
                if self._stop.is_set():
                    return
                try:
                    outcome = await asyncio.to_thread(
                        run_scheduled_job, job,
                        retry_seconds=self.retry_seconds,
                    )
                except Exception:  # noqa: BLE001 - the loop must survive
                    log.exception("scheduler could not evaluate %s", job.name)
                    continue
                if outcome["status"] not in ("not_due", "locked"):
                    log.info("scheduled job %s: %s", job.name, outcome["status"])
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.poll_seconds
                )
            except TimeoutError:
                pass

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(
                self._run_forever(), name="patchtrack-scheduler"
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            # A connector run may outlive the shutdown window. Do not hold the
            # process open for it; the job's next_run_at is still pending, so
            # it is retried after the next start.
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except TimeoutError:
                self._task.cancel()


def install(app: Any, jobs: Iterable[ScheduledJob]) -> None:
    """Attach the service's scheduler to a FastAPI application's lifespan."""
    settings = get_settings()
    if not settings.scheduler_enabled:
        log.info("in-app scheduler disabled by configuration")
        return
    if getattr(app.state, "patchtrack_scheduler", None) is not None:
        return
    scheduler = InAppScheduler(
        jobs,
        poll_seconds=settings.scheduler_poll_seconds,
        retry_seconds=settings.scheduler_retry_seconds,
        startup_delay_seconds=settings.scheduler_startup_delay_seconds,
    )
    app.state.patchtrack_scheduler = scheduler

    # Compose the scheduler into the application's lifespan. Starlette 1.0
    # removed the per-event ``add_event_handler`` API, and FastAPI's
    # ``on_event`` is deprecated; wrapping the existing lifespan works with
    # both, and preserves any lifespan FastAPI installed itself.
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def _lifespan(app_: Any):
        async with previous(app_):
            await scheduler.start()
            try:
                yield
            finally:
                await scheduler.stop()

    app.router.lifespan_context = _lifespan
