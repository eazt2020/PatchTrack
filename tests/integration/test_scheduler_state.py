"""The in-app scheduler's durable due state and single-execution guarantee."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from patchtrack_common.scheduler import ScheduledJob, due_jobs, run_scheduled_job


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


def test_the_configured_interval_wins_over_the_code_default(engine):
    name = "test.scheduler.configured"
    clear_job(engine, name)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO shared.scheduled_jobs "
                "(name, interval_seconds, next_run_at) "
                "VALUES (:n, 120, now() - interval '1 minute')"
            ),
            {"n": name},
        )

    run_scheduled_job(
        ScheduledJob(name=name, interval_seconds=86400,
                     run=lambda: {"status": "success"})
    )

    state = job_state(engine, name)
    assert state["interval_seconds"] == 120
    assert state["next_run_at"] < datetime.now(UTC) + timedelta(minutes=5)


def test_a_disabled_job_is_not_run_even_when_due(engine):
    name = "test.scheduler.disabled"
    clear_job(engine, name)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO shared.scheduled_jobs "
                "(name, interval_seconds, enabled, next_run_at) "
                "VALUES (:n, 60, false, now() - interval '1 hour')"
            ),
            {"n": name},
        )
    calls: list[int] = []

    outcome = run_scheduled_job(
        ScheduledJob(name=name, interval_seconds=60,
                     run=lambda: calls.append(1) or {"status": "success"})
    )

    assert outcome["status"] == "disabled"
    assert calls == []


def test_due_jobs_selects_missing_and_past_in_one_query(engine):
    missing = ScheduledJob("test.scheduler.due.missing", 60, lambda: None)
    future = ScheduledJob("test.scheduler.due.future", 60, lambda: None)
    past = ScheduledJob("test.scheduler.due.past", 60, lambda: None)
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM shared.scheduled_jobs WHERE name = ANY(:names)"),
            {"names": [missing.name, future.name, past.name]},
        )
        conn.execute(
            text(
                "INSERT INTO shared.scheduled_jobs (name, interval_seconds, next_run_at) "
                "VALUES (:f, 60, now() + interval '1 hour'), "
                "       (:p, 60, now() - interval '1 hour')"
            ),
            {"f": future.name, "p": past.name},
        )

    ready = due_jobs([missing, future, past])

    assert {job.name for job in ready} == {missing.name, past.name}


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
