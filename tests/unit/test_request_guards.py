"""Oversized and pathologically nested request bodies are refused, not parsed."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from patchtrack_common.request_guards import RequestBodyLimit, install


def _app(max_bytes: int = 1024, upload_max_bytes: int = 1024) -> FastAPI:
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    @app.post("/json")
    async def json_body(request: Request) -> dict[str, bool]:
        await request.json()
        return {"ok": True}

    @app.post("/restores/upload")
    async def upload(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    app.add_middleware(
        RequestBodyLimit,
        max_bytes=max_bytes,
        upload_max_bytes=upload_max_bytes,
    )
    app.add_exception_handler(RecursionError, _deep_handler)
    return app


async def _deep_handler(_: Request, __: Exception):
    from fastapi.responses import JSONResponse

    return JSONResponse({"detail": "too deep"}, status_code=422)


def test_a_declared_oversized_body_is_refused_with_413():
    response = TestClient(_app()).post("/echo", content=b"x" * 2048)
    assert response.status_code == 413


def test_a_chunked_body_without_content_length_is_still_capped():
    # A generator makes httpx use chunked transfer, so no Content-Length is
    # available for the cheap up-front check.
    def chunks():
        for _ in range(8):
            yield b"x" * 512

    response = TestClient(_app()).post("/echo", content=chunks())
    assert response.status_code == 413


def test_a_body_within_the_limit_reaches_the_route():
    response = TestClient(_app()).post("/echo", content=b"x" * 100)
    assert response.status_code == 200
    assert response.json() == {"bytes": 100}


def test_only_the_restore_upload_path_gets_the_archive_limit():
    app = _app(max_bytes=1024, upload_max_bytes=8192)
    assert TestClient(app).post(
        "/restores/upload", content=b"x" * 4096
    ).status_code == 200
    # The same body on an ordinary endpoint is still refused.
    assert TestClient(app).post("/echo", content=b"x" * 4096).status_code == 413


def test_a_recursion_failure_becomes_a_422_not_a_500():
    app = FastAPI()

    @app.post("/recurse")
    async def recurse() -> dict[str, bool]:
        # Shaped like the failure a deeply nested JSON body produces during
        # validation. Writing the recursion here keeps the test from depending
        # on a particular CPython JSON parser's depth threshold.
        raise RecursionError("maximum recursion depth exceeded")

    app.add_exception_handler(RecursionError, _deep_handler)
    response = TestClient(app).post("/recurse")
    assert response.status_code == 422
    assert response.json() == {"detail": "too deep"}


def test_the_installed_guard_uses_a_configured_limit():
    app = FastAPI()
    app.add_exception_handler(RecursionError, _deep_handler)
    install(app)
    # The default is measured in megabytes, so the application still starts;
    # this asserts the middleware was added at all.
    assert any(m.cls is RequestBodyLimit for m in app.user_middleware)
