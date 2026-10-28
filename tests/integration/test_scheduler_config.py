"""The Schedules screen exposes every in-app job, and edits take effect."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from patchtrack_common.scheduler import JOB_DEFINITIONS, due_jobs, ScheduledJob
from patchtrack_common.security import Principal
from reporting.app.main import (
    SchedulerJobUpdate,
    list_scheduler_jobs,
    update_scheduler_job,
)

ADMIN = Principal(
    user_id="00000000-0000-0000-0000-000000000001",
    username="admin",
    role="administrator",
)
REPORTER = Principal(
    user_id="00000000-0000-0000-0000-000000000002",
    username="reporter",
    role="report_manager",
)


class _Request:
    client = SimpleNamespace(host="127.0.0.1")
    headers: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _database(migrated_database):
    return migrated_database


@pytest.fixture
def no_jobs(engine):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM shared.scheduled_jobs"))
    yield


def test_every_registered_job_is_listed_before_it_has_run(no_jobs):
    jobs = list_scheduler_jobs(REPORTER)

    assert {job["name"] for job in jobs} == {
        definition.name for definition in JOB_DEFINITIONS
    }
    assert all(job["enabled"] for job in jobs)
    assert all(job["next_run_at"] is None for job in jobs)
    assert all(job["interval_seconds"] == job["default_interval_seconds"]
               for job in jobs)


def test_changing_the_interval_schedules_the_next_run_from_now(engine, no_jobs):
    job = update_scheduler_job(
        "reporting.tick", SchedulerJobUpdate(interval_seconds=120),
        _Request(), ADMIN,
    )

    assert job["interval_seconds"] == 120
    assert job["enabled"] is True
    assert job["next_run_at"] > datetime.now(UTC) + timedelta(minutes=1)


def test_disabling_a_job_keeps_it_out_of_the_due_set(engine, no_jobs):
    update_scheduler_job(
        "compliance.daily", SchedulerJobUpdate(enabled=False), _Request(), ADMIN,
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE shared.scheduled_jobs SET next_run_at = now() - interval '1 hour' "
                "WHERE name = 'compliance.daily'"
            )
        )

    assert due_jobs([ScheduledJob("compliance.daily", 86400, lambda: None)]) == []


def test_reenabling_a_job_makes_it_wait_for_its_interval(engine, no_jobs):
    update_scheduler_job(
        "compliance.daily", SchedulerJobUpdate(enabled=False), _Request(), ADMIN,
    )
    job = update_scheduler_job(
        "compliance.daily",
        SchedulerJobUpdate(enabled=True, interval_seconds=3600), _Request(), ADMIN,
    )

    assert job["enabled"] is True
    assert job["next_run_at"] > datetime.now(UTC) + timedelta(minutes=50)


def test_an_unknown_job_is_refused(no_jobs):
    with pytest.raises(HTTPException) as excinfo:
        update_scheduler_job(
            "inventory.not_a_job", SchedulerJobUpdate(enabled=False),
            _Request(), ADMIN,
        )
    assert excinfo.value.status_code == 404


def test_an_empty_update_is_refused(no_jobs):
    with pytest.raises(HTTPException) as excinfo:
        update_scheduler_job(
            "reporting.tick", SchedulerJobUpdate(), _Request(), ADMIN,
        )
    assert excinfo.value.status_code == 422
