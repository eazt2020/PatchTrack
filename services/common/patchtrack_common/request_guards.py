"""Limits applied before a request body is parsed.

Two denial-of-service shapes are cheap for a client and expensive for the
process: an enormous body, and a small body whose JSON nests deeply enough to
exhaust the interpreter stack. Both are refused with 413/422 rather than
allowed to become an unhandled 500.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import get_settings

# Evidence attachments are submitted base64 inside a JSON justification
# request: five files at five megabytes each is roughly 33 MB once encoded.
# The limit leaves headroom for that without accepting unbounded uploads.
MAX_REQUEST_BYTES = 64 * 1024 * 1024
DEFAULT_RESTORE_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024
RESTORE_UPLOAD_SUFFIX = "/restores/upload"


class _BodyTooLarge(Exception):
    """Raised from the receive channel when the stream exceeds the limit."""


class RequestBodyLimit:
    """An ASGI middleware that caps the request body, declared or streamed.

    Content-Length alone is not trusted: a client may omit it or lie. The
    bytes actually received are therefore counted as well, so a chunked upload
    cannot bypass the limit.
    """

    def __init__(
        self,
        app: Any,
        max_bytes: int = MAX_REQUEST_BYTES,
        upload_max_bytes: int = DEFAULT_RESTORE_UPLOAD_BYTES,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.upload_max_bytes = max(upload_max_bytes, max_bytes)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # A downloaded archive can be larger than any JSON request. Only the
        # restore-upload endpoint gets the archive-sized cap; every other path
        # keeps the ordinary limit, so this does not widen the general surface.
        limit = (
            self.upload_max_bytes
            if scope.get("path", "").endswith(RESTORE_UPLOAD_SUFFIX)
            else self.max_bytes
        )

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    await _reject(send, 400, "Invalid Content-Length")
                    return
                if declared > limit:
                    await _reject(
                        send, 413,
                        f"Request body exceeds {limit} bytes",
                    )
                    return

        received = 0
        response_started = False

        async def limited_receive() -> dict[str, Any]:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not response_started:
                await _reject(
                    send, 413, f"Request body exceeds {self.max_bytes} bytes"
                )


async def _reject(send: Any, status_code: int, detail: str) -> None:
    response = JSONResponse({"detail": detail}, status_code=status_code)
    await response(
        {"type": "http", "method": "POST", "path": "/", "headers": []},
        None,
        send,
    )


async def _deeply_nested(_: Request, __: Exception) -> JSONResponse:
    return JSONResponse(
        {"detail": "Request payload is too deeply nested"}, status_code=422
    )


def install(app: FastAPI) -> None:
    """Apply the body limit and the deep-nesting guard to a FastAPI app."""
    max_bytes = int(getattr(
        get_settings(), "max_request_bytes", MAX_REQUEST_BYTES
    ))
    upload_max_bytes = int(getattr(
        get_settings(), "restore_upload_max_bytes", DEFAULT_RESTORE_UPLOAD_BYTES
    ))
    app.add_middleware(
        RequestBodyLimit,
        max_bytes=max_bytes,
        upload_max_bytes=upload_max_bytes,
    )
    app.add_exception_handler(RecursionError, _deeply_nested)
