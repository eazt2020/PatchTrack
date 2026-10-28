"""Prometheus exposition: bounded labels and a working histogram."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest

from patchtrack_common.metrics import install, render, reset, set_gauge


@pytest.fixture(autouse=True)
def _clean_registry():
    reset()
    yield
    reset()


def _app() -> FastAPI:
    app = FastAPI()
    install(app, "inventory")

    @app.get("/servers/{server_id}")
    def server(server_id: str) -> dict[str, str]:
        return {"id": server_id}

    return app


def test_metrics_endpoint_is_public_and_renders_the_prometheus_format():
    client = TestClient(_app())
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# TYPE patchtrack_http_requests_total counter" in response.text
    assert 'patchtrack_service_uptime_seconds{service="inventory"}' in response.text


def test_route_label_is_the_template_not_the_raw_path():
    client = TestClient(_app())
    for server_id in ("one", "two", "three"):
        client.get(f"/servers/{server_id}")
    text = client.get("/metrics").text
    assert 'route="/servers/{server_id}"' in text
    assert 'route="/servers/one"' not in text


def test_requests_and_status_codes_are_counted():
    client = TestClient(_app())
    client.get("/servers/a")
    client.get("/servers/b")
    text = client.get("/metrics").text
    assert ('patchtrack_http_requests_total'
            '{method="GET",route="/servers/{server_id}",service="inventory",status="200"} 2'
            in text)


def test_metrics_requests_are_not_counted():
    client = TestClient(_app())
    client.get("/metrics")
    text = client.get("/metrics").text
    assert 'route="/metrics"' not in text


def test_unknown_paths_share_one_series():
    client = TestClient(_app())
    client.get("/nope-1")
    client.get("/nope-2")
    text = client.get("/metrics").text
    assert 'route="unmatched"' in text
    assert "nope-1" not in text


def test_gauges_render_with_labels():
    set_gauge("patchtrack_test_gauge", 7, "A test gauge.", service="inventory")
    text = render("inventory")
    assert "# TYPE patchtrack_test_gauge gauge" in text
    assert 'patchtrack_test_gauge{service="inventory"} 7' in text
