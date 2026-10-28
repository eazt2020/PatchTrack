"""Pure behaviour of the in-app scheduler."""

from __future__ import annotations

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

        def add_event_handler(self, *_args):
            raise AssertionError("a disabled scheduler must not register handlers")

    install(FakeApp(), [ScheduledJob("x", 60, lambda: None)])


def test_install_registers_startup_and_shutdown(monkeypatch):
    monkeypatch.setattr(
        scheduler_module, "get_settings",
        lambda: SimpleNamespace(
            scheduler_enabled=True, scheduler_poll_seconds=15,
            scheduler_retry_seconds=300, scheduler_startup_delay_seconds=10,
        ),
    )
    handlers: dict[str, object] = {}

    class FakeApp:
        state = SimpleNamespace()

        def add_event_handler(self, event, handler):
            handlers[event] = handler

    app = FakeApp()
    install(app, [ScheduledJob("x", 60, lambda: None)])

    assert set(handlers) == {"startup", "shutdown"}
    assert app.state.patchtrack_scheduler.jobs[0].name == "x"
