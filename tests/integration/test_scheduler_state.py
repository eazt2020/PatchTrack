"""The in-app scheduler's durable due state and single-execution guarantee."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from patchtrack_common.scheduler import ScheduledJob, run_scheduled_job


def clear_job(engine, name: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM shared.scheduled_jobs WHERE name = :n"), {"n": name}
        )


def job_state(engine, name: str):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT * FROM shared.scheduled_jobs WHERE name = :n"),
            {"n": name},
        ).mappings().first()


def test_a_new_job_runs_and_its_next_run_is_persisted(engine):
    name = "test.scheduler.new"
    clear_job(engine, name)
    calls: list[int] = []

    outcome = run_scheduled_job(
        ScheduledJob(name=name, interval_seconds=3600,
                     run=lambda: calls.append(1) or {"status": "success"})
    )

    assert outcome["status"] == "success"
    assert calls == [1]
    state = job_state(engine, name)
    assert state["last_status"] == "success"
    assert state["next_run_at"] > datetime.now(UTC) + timedelta(minutes=50)
    assert state["last_started_at"] is not None
    assert state["last_finished_at"] is not None


def test_a_job_is_not_run_again_before_it_is_due(engine):
    name = "test.scheduler.not_due"
    calls: list[int] = []
    job = ScheduledJob(name=name, interval_seconds=3600,
                       run=lambda: calls.append(1) or {"status": "success"})

    assert run_scheduled_job(job)["status"] == "success"
    assert run_scheduled_job(job)["status"] == "not_due"
    assert calls == [1]


def test_a_failed_run_is_retried_on_the_retry_interval(engine):
    name = "test.scheduler.failure"
    clear_job(engine, name)

    def fail() -> dict[str, str]:
        raise RuntimeError("source is down")

    outcome = run_scheduled_job(
        ScheduledJob(name=name, interval_seconds=86400, run=fail),
        retry_seconds=120,
    )

    assert outcome["status"] == "failure"
    state = job_state(engine, name)
    assert state["last_status"] == "failure"
    assert "source is down" in state["last_message"]
    # Well before the daily interval, so a transient outage recovers quickly.
    assert state["next_run_at"] < datetime.now(UTC) + timedelta(minutes=5)


def test_a_reported_failure_is_not_a_success(engine):
    name = "test.scheduler.reported_failure"
    clear_job(engine, name)

    run_scheduled_job(
        ScheduledJob(name=name, interval_seconds=86400,
                     run=lambda: {"status": "failed", "detail": "no snapshot"}),
        retry_seconds=60,
    )

    assert job_state(engine, name)["last_status"] == "failure"


def test_a_second_runner_cannot_execute_the_same_due_job(engine):
    name = "test.scheduler.locked"
    clear_job(engine, name)
    with engine.connect() as holder:
        holder.execute(
            text("SELECT pg_advisory_lock(20564, hashtext(:n))"), {"n": name}
        )
        try:
            outcome = run_scheduled_job(
                ScheduledJob(name=name, interval_seconds=60,
                             run=lambda: {"status": "success"})
            )
        finally:
            holder.execute(
                text("SELECT pg_advisory_unlock(20564, hashtext(:n))"),
                {"n": name},
            )
            holder.commit()

    assert outcome["status"] == "locked"
    assert job_state(engine, name) is None
