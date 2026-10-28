"""The service launcher wraps uvicorn and terminates TLS for the gateway.

The deployment artifacts use this module instead of the uvicorn CLI, so a
certificate uploaded later can be reloaded into the live listener. Only the
gateway may build a TLS context, and only when TLS is enabled.
"""

from __future__ import annotations

from pathlib import Path

import uvicorn

from patchtrack_common import serve, tls
from patchtrack_common.config import get_settings


class _RecordingServer:
    instance: "_RecordingServer | None" = None

    def __init__(self, config: uvicorn.Config) -> None:
        self.config = config
        _RecordingServer.instance = self

    def run(self) -> None:  # pragma: no cover - the test only inspects config
        pass


def _launch(monkeypatch, tmp_path: Path, *, service: str, enabled: bool):
    monkeypatch.setattr(uvicorn, "Server", _RecordingServer)
    monkeypatch.setattr(tls, "_ACTIVE", None)
    monkeypatch.setenv("PATCHTRACK_TLS_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("PATCHTRACK_TLS_DIR", str(tmp_path / "tls"))
    get_settings.cache_clear()
    serve.main([
        "gateway.app.main:app", "--host", "127.0.0.1", "--port", "8010",
        "--log-level", "info", "--service", service,
    ])
    get_settings.cache_clear()
    server = _RecordingServer.instance
    assert server is not None
    return server.config


def test_the_gateway_serves_tls_when_enabled(monkeypatch, tmp_path):
    config = _launch(monkeypatch, tmp_path, service="gateway", enabled=True)

    assert getattr(config, "ssl", None) is not None
    assert (tmp_path / "tls" / tls.CERT_NAME).exists()
    assert (tmp_path / "tls" / tls.KEY_NAME).exists()


def test_other_services_never_build_a_tls_context(monkeypatch, tmp_path):
    config = _launch(monkeypatch, tmp_path, service="reporting", enabled=True)

    assert getattr(config, "ssl", None) is None
    assert not (tmp_path / "tls").exists()


def test_tls_disabled_serves_plain_http(monkeypatch, tmp_path):
    config = _launch(monkeypatch, tmp_path, service="gateway", enabled=False)

    assert getattr(config, "ssl", None) is None
    assert not (tmp_path / "tls").exists()


def test_the_launcher_defaults_to_the_environment_service(monkeypatch):
    monkeypatch.setenv("PATCHTRACK_SERVICE", "inventory")
    args = serve._arguments(["app.main:app"])
    assert args.service == "inventory"
    assert args.port == 8000
