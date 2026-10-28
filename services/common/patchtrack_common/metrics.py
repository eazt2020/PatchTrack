"""Prometheus metrics for every PatchTrack service.

The runtime health endpoints answer "is this process up?"; they cannot answer
"how often does collection fail?" or "how many reports are overdue?". This
module exposes those signals in the Prometheus text format without adding a
client dependency.

Cardinality is bounded deliberately: labels use the *route template*
(``/servers``), never the raw path, so an attacker cannot create a new series
per request. ``/metrics`` itself is not counted.
"""

from __future__ import annotations

import math
import threading
import time
from collections import defaultdict

from fastapi import FastAPI, Request, Response

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

_lock = threading.Lock()
_started = time.monotonic()
_requests: dict[tuple[str, str, str, str], int] = defaultdict(int)
_durations: dict[tuple[str, str, str, str], list[float]] = defaultdict(lambda: [0.0] * (len(BUCKETS) + 2))
_gauges: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[str, float]] = {}


def reset() -> None:
    """Clear collected series. Intended for tests."""
    with _lock:
        _requests.clear()
        _durations.clear()
        _gauges.clear()


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**labels: str) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{key}="{_label(str(value))}"' for key, value in sorted(labels.items()))
    return "{" + inner + "}"


def set_gauge(name: str, value: float, help_text: str, **labels: str) -> None:
    """Publish a point-in-time value, e.g. overdue remediation count."""
    with _lock:
        _gauges[(name, tuple(sorted(labels.items())))] = (help_text, float(value))


def _route(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


def _observe(service: str, method: str, route: str, status: int, seconds: float) -> None:
    key = (service, method, route, str(status))
    with _lock:
        _requests[key] += 1
        row = _durations[key]
        row[-2] += seconds
        row[-1] += 1
        for index, bucket in enumerate(BUCKETS):
            if seconds <= bucket:
                row[index] += 1


def render(service: str) -> str:
    with _lock:
        requests = dict(_requests)
        durations = {key: list(value) for key, value in _durations.items()}
        gauges = dict(_gauges)

    lines = [
        "# HELP patchtrack_http_requests_total HTTP requests handled.",
        "# TYPE patchtrack_http_requests_total counter",
    ]
    for (svc, method, route, status), count in sorted(requests.items()):
        if svc != service:
            continue
        lines.append(
            "patchtrack_http_requests_total"
            + _labels(service=svc, method=method, route=route, status=status)
            + f" {count}"
        )

    lines += [
        "# HELP patchtrack_http_request_duration_seconds Request duration.",
        "# TYPE patchtrack_http_request_duration_seconds histogram",
    ]
    for (svc, method, route, status), row in sorted(durations.items()):
        if svc != service:
            continue
        labels = _labels(service=svc, method=method, route=route, status=status)
        for index, bucket in enumerate(BUCKETS):
            lines.append(
                "patchtrack_http_request_duration_seconds_bucket"
                + _labels(service=svc, method=method, route=route, status=status,
                          le=str(bucket))
                + f" {row[index]}"
            )
        lines.append("patchtrack_http_request_duration_seconds_bucket"
                     + _labels(service=svc, method=method, route=route,
                               status=status, le="+Inf") + f" {row[-1]}")
        lines.append("patchtrack_http_request_duration_seconds_sum" + labels
                     + f" {row[-2]:.6f}")
        lines.append("patchtrack_http_request_duration_seconds_count" + labels
                     + f" {row[-1]}")

    lines += [
        "# HELP patchtrack_service_uptime_seconds Seconds since process start.",
        "# TYPE patchtrack_service_uptime_seconds gauge",
        "patchtrack_service_uptime_seconds" + _labels(service=service)
        + f" {time.monotonic() - _started:.3f}",
    ]
    for (name, labels), (help_text, value) in sorted(gauges.items()):
        if not math.isfinite(value):
            continue
        lines += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge",
                  name + _labels(**dict(labels)) + f" {value}"]

    return "\n".join(lines) + "\n"


def install(app: FastAPI, service: str) -> None:
    """Attach the observer middleware and an unauthenticated ``/metrics`` route.

    The endpoint must be reachable by the scraper, which has no user session.
    Bind backend services to the internal network only; never publish this
    route through the gateway.
    """

    @app.middleware("http")
    async def _timed(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path == "/metrics":
            return await call_next(request)
        started = time.monotonic()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            _observe(service, request.method, _route(request), status,
                     time.monotonic() - started)

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(render(service), media_type=CONTENT_TYPE)
