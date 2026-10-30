"""Pure behaviour of the in-app scheduler."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from patchtrack_common import scheduler as scheduler_module
from patchtrack_common.scheduler import ScheduledJob, install


@pytest.mark.parametrize(
    ("result", "failed"),
    [
        ({"status": "failed"}, True),
        ({"status": "partial"}, False),
        ({"status": "skipped"}, False),
        ({"status": "success"}, False),
        ({"processed": []}, False),
        (None, False),
    ],
)
def test_only_an_explicit_failure_counts_as_failure(result, failed):
    assert scheduler_module._failed(result) is failed


def test_the_scheduler_is_not_installed_when_disabled(monkeypatch):
    monkeypatch.setattr(
        scheduler_module, "get_settings",
        lambda: SimpleNamespace(scheduler_enabled=False),
    )

    class FakeApp:
        state = SimpleNamespace()

        @property
        def router(self):
            raise AssertionError("a disabled scheduler must not register handlers")

    install(FakeApp(), [ScheduledJob("x", 60, lambda: None)])


def test_install_wraps_the_lifespan_to_start_and_stop(monkeypatch):
    monkeypatch.setattr(
        scheduler_module, "get_settings",
        lambda: SimpleNamespace(
            scheduler_enabled=True, scheduler_poll_seconds=15,
            scheduler_retry_seconds=300, scheduler_startup_delay_seconds=10,
        ),
    )
    events: list[str] = []

    async def start(_self) -> None:
        events.append("scheduler-start")

    async def stop(_self) -> None:
        events.append("scheduler-stop")

    monkeypatch.setattr(scheduler_module.InAppScheduler, "start", start)
    monkeypatch.setattr(scheduler_module.InAppScheduler, "stop", stop)

    @asynccontextmanager
    async def base_lifespan(_app):
        events.append("base-start")
        yield
        events.append("base-stop")

    class FakeRouter:
        lifespan_context = staticmethod(base_lifespan)

    class FakeApp:
        state = SimpleNamespace()
        router = FakeRouter()

    app = FakeApp()
    install(app, [ScheduledJob("x", 60, lambda: None)])

    scheduler = app.state.patchtrack_scheduler
    assert scheduler.jobs[0].name == "x"
    assert app.router.lifespan_context is not base_lifespan

    async def run() -> None:
        async with app.router.lifespan_context(app):
            events.append("serving")

    asyncio.run(run())

    # The original lifespan still ran, bracketed by the scheduler's start and
    # stop, which is what a real uvicorn startup/shutdown does.
    assert events == [
        "base-start", "scheduler-start", "serving", "scheduler-stop", "base-stop",
    ]
