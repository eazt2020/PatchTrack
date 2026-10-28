"""PatchTrack gateway (single entry point).

Responsibilities:

* Serve the authenticated single-page UI shell.
* Authenticate a browser session with an HttpOnly, SameSite=Strict cookie
  holding the JWT issued by the identity service.
* Proxy authorised calls to the backend services, injecting a short-lived
  bearer token (browser never sees internal service topology).
* Convert the authenticated session into trusted gateway headers consumed by
  backend services via ``PATCHTRACK_SERVICE_TOKEN``.
* Add security headers and CSRF defence (origin check + custom header).

The gateway holds no business logic. It never contacts the database.
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from patchtrack_common.config import get_settings
from patchtrack_common.net import client_ip
from patchtrack_common.security import decode_token

APP = "gateway"
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR.parent / "static"

app = FastAPI(title="PatchTrack Gateway", version="1.0.0", docs_url=None,
              redoc_url=None, openapi_url=None)

COOKIE = "patchtrack_session"
CSRF_HEADER = "x-patchtrack-csrf"
CSRF_COOKIE = "patchtrack_csrf"
PUBLIC_PATHS = {"/login", "/api/session/login", "/api/session/bootstrap",
                "/healthz", "/readyz", "/api/session/status"}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self' "
        "'unsafe-inline'; script-src 'self'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}

SERVICE_MAP = {
    "identity": "identity_url",
    "inventory": "inventory_url",
    "compliance": "compliance_url",
    "reporting": "reporting_url",
}


def _client_ip(request: Request) -> str | None:
    """The browser's address as established by the ASGI server.

    The raw header is deliberately not read: a client can put anything in
    ``X-Forwarded-For``, and this value is forwarded to the backend services
    and written to their audit trail. Uvicorn honours the header only for
    peers listed in ``PATCHTRACK_FORWARDED_IPS``.
    """
    return client_ip(request)


def _setting(key: str) -> str:
    return getattr(get_settings(), SERVICE_MAP[key])


def _session(request: Request) -> dict[str, Any]:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        return decode_token(token)
    except HTTPException:
        raise HTTPException(status_code=401, detail="Session expired") from None


def _set_security(response: Response) -> Response:
    for key, value in SECURITY_HEADERS.items():
        response.headers[key] = value
    return response


# Pre-session endpoints: no CSRF token can exist yet, so they are protected by
# the origin check below instead of double-submit.
CSRF_EXEMPT = {"/api/session/login", "/api/session/bootstrap"}


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        path = request.url.path
        if path.startswith("/api/"):
            origin = request.headers.get("origin")
            host = request.headers.get("host", "")
            allowed = request.headers.get("x-forwarded-host") or host
            if origin and not _origin_matches(origin, allowed):
                return _set_security(JSONResponse(
                    {"detail": "Cross-origin request rejected"}, status_code=403))
            if path not in CSRF_EXEMPT:
                cookie = request.cookies.get(CSRF_COOKIE)
                if not cookie:
                    return _set_security(JSONResponse(
                        {"detail": "Missing CSRF cookie"}, status_code=403))
                if request.headers.get(CSRF_HEADER) != cookie:
                    return _set_security(JSONResponse(
                        {"detail": "CSRF validation failed"}, status_code=403))
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        # The console is a module SPA: app.js is fetched once and its render
        # functions stay in memory for the life of the tab. Without this,
        # browsers apply heuristic freshness (10% of the file's age) and keep
        # serving a stale module after a deploy, so a normal reload would not
        # pick up a changed screen. Revalidation is cheap: ETag/Last-Modified
        # still answer 304 when nothing changed.
        response.headers.setdefault("Cache-Control", "no-cache")
    return _set_security(response)


def _origin_matches(origin: str, allowed: str) -> bool:
    """Compare scheme+host+port, so ``evil-127.0.0.1`` cannot suffix-match."""
    origin = origin.rstrip("/").lower()
    allowed = allowed.strip().lower()
    if not allowed:
        return False
    for scheme in ("http://", "https://"):
        if origin == f"{scheme}{allowed}":
            return True
    return False


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": APP}


@app.get("/readyz")
async def readyz() -> dict[str, Any]:
    services: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=3.0) as client:
        for name in SERVICE_MAP:
            try:
                response = await client.get(f"{_setting(name)}/healthz")
                services[name] = "ok" if response.status_code == 200 else "unhealthy"
            except httpx.HTTPError:
                services[name] = "unreachable"
    state = "ready" if all(v == "ok" for v in services.values()) else "degraded"
    return {"status": state, "services": services}


# --------------------------------------------------------------------------- session
class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=512)


class BootstrapBody(BaseModel):
    username: str = Field(min_length=3, max_length=150,
                          pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(min_length=12, max_length=512)
    display_name: str = Field(default="", max_length=200)
    email: str | None = None


class ChangePasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=12, max_length=512)


@app.get("/api/session/status")
async def session_status(request: Request) -> dict[str, Any]:
    try:
        payload = _session(request)
    except HTTPException:
        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                response = await client.get(f"{_setting('identity')}/bootstrap/status")
                needs_admin = response.json().get("needs_admin", False)
            except (httpx.HTTPError, ValueError):
                needs_admin = False
        return {"authenticated": False, "needs_admin": needs_admin}
    return {
        "authenticated": True,
        "needs_admin": False,
        "user": {
            "id": payload["sub"],
            "username": payload.get("username", ""),
            "display_name": payload.get("name", ""),
            "role": payload.get("role", "viewer"),
        },
    }


@app.post("/api/session/bootstrap")
async def bootstrap(payload: BootstrapBody, response: Response,
                    request: Request) -> dict[str, Any]:
    """First-run administrator creation. Only works while zero users exist."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        status_response = await client.get(f"{_setting('identity')}/bootstrap/status")
        if not status_response.json().get("needs_admin"):
            raise HTTPException(status_code=409, detail="Platform is already initialised")
        created = await client.post(
            f"{_setting('identity')}/users",
            json={
                "username": payload.username, "password": payload.password,
                "display_name": payload.display_name,
                "email": payload.email, "role": "administrator",
            },
            headers={
                "x-patchtrack-internal": get_settings().service_token,
                "x-patchtrack-user": "bootstrap",
                "x-patchtrack-role": "administrator",
                "x-patchtrack-user-id": "",
            },
        )
        if created.status_code >= 400:
            raise HTTPException(status_code=created.status_code,
                                detail=created.json().get("detail", "Bootstrap failed"))
        login = await client.post(
            f"{_setting('identity')}/auth/login",
            json={"username": payload.username, "password": payload.password},
        )
    if login.status_code >= 400:
        raise HTTPException(status_code=502, detail="Account created but sign-in failed")
    return _establish_session(login.json(), response, request)


@app.post("/api/session/login")
async def login(payload: LoginBody, response: Response,
                request: Request) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        upstream = await client.post(
            f"{_setting('identity')}/auth/login",
            json={"username": payload.username, "password": payload.password},
            headers={"x-forwarded-for": _client_ip(request) or ""},
        )
    if upstream.status_code >= 400:
        detail = "Invalid username or password"
        try:
            detail = upstream.json().get("detail", detail)
        except ValueError:
            pass
        raise HTTPException(status_code=upstream.status_code, detail=detail)
    return _establish_session(upstream.json(), response, request)


def _establish_session(data: dict[str, Any], response: Response,
                       request: Request) -> dict[str, Any]:
    settings = get_settings()
    token = data["access_token"]
    csrf = secrets.token_urlsafe(32)
    secure = _cookie_secure(request)
    response.set_cookie(
        COOKIE, token, httponly=True, secure=secure,
        samesite="strict", max_age=settings.access_token_minutes * 60,
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE, csrf, httponly=False, secure=secure,
        samesite="strict", max_age=settings.access_token_minutes * 60, path="/",
    )
    return {"user": data["user"], "csrf_token": csrf}


def _cookie_secure(request: Request) -> bool:
    """Whether the session cookies may travel only over TLS.

    The default, 'auto', requires TLS for every host that is not loopback: a
    cookie marked Secure is dropped by the browser over plain HTTP, which is
    the correct failure for an unencrypted deployment. 'always' forces it even
    on loopback. 'never' exists only for a development instance browsed by IP
    over HTTP; the Settings validator refuses it when
    PATCHTRACK_ENVIRONMENT=production.
    """
    policy = get_settings().session_cookie_secure
    if policy == "always":
        return True
    if policy == "never":
        return False
    return not _is_loopback(request)


def _is_loopback(request: Request) -> bool:
    host = request.headers.get("host") or ""
    if host.startswith("["):
        # An IPv6 literal arrives bracketed, "[::1]:8005". Splitting on ":"
        # would yield "[" and the address would be treated as remote.
        host = host[1:host.index("]")] if "]" in host else host
    else:
        host = host.split(":")[0]
    return host in ("127.0.0.1", "localhost", "::1", "testserver")


@app.post("/api/session/logout")
def logout(response: Response) -> dict[str, str]:
    response.delete_cookie(COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"status": "logged_out"}


@app.get("/api/me")
def me(request: Request) -> dict[str, Any]:
    payload = _session(request)
    from patchtrack_common.security import PERMISSIONS

    role = payload.get("role", "viewer")
    return {
        "id": payload["sub"], "username": payload.get("username", ""),
        "display_name": payload.get("name", ""), "role": role,
        "permissions": sorted(PERMISSIONS.get(role, set())),
        "must_change_password": bool(payload.get("pwd_change_required")),
    }


# --------------------------------------------------------------------------- proxy
def _internal_headers(request: Request, payload: dict[str, Any]) -> dict[str, str]:
    settings = get_settings()
    return {
        "x-patchtrack-internal": settings.service_token,
        "x-patchtrack-user": str(payload.get("username", "")),
        "x-patchtrack-role": str(payload.get("role", "viewer")),
        "x-patchtrack-user-id": str(payload.get("sub", "")),
        "x-patchtrack-display-name": str(payload.get("name", "")),
        "x-forwarded-for": _client_ip(request) or "",
    }


async def _proxy(service: str, path: str, request: Request,
                 authenticated: bool = True) -> Response:
    headers: dict[str, str] = {}
    if authenticated:
        payload = _session(request)
        if payload.get("pwd_change_required"):
            # A forced rotation is not advisory: until the user sets a new
            # password, their session may reach nothing but the change-password
            # endpoint and the sign-out path.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Password change required before continuing",
            )
        headers.update(_internal_headers(request, payload))
        headers["authorization"] = f"Bearer {request.cookies.get(COOKIE)}"
    for header in ("accept", "content-type", "if-none-match"):
        if header in request.headers:
            headers[header] = request.headers[header]
    url = f"{_setting(service)}/{path.lstrip('/')}"
    body = await request.body()
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=False) as client:
        try:
            upstream = await client.request(
                request.method, url, headers=headers, params=request.query_params,
                content=body or None,
            )
        except httpx.HTTPError as exc:
            log.warning("upstream %s unavailable: %s", service, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"{service} service is temporarily unavailable",
            ) from exc
    response_headers = {
        key: value for key, value in upstream.headers.items()
        if key.lower() in ("content-type", "content-disposition", "cache-control",
                           "etag", "content-length")
    }
    return Response(
        content=upstream.content, status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


# Registered before the generic proxy: FastAPI matches routes in declaration
# order, and "/api/{service}/{path}" would otherwise capture these paths.
@app.post("/api/session/change-password")
async def change_password(payload: ChangePasswordBody, request: Request,
                          response: Response) -> dict[str, Any]:
    """Satisfy a forced password rotation.

    Deliberately not behind the normal proxy guard: this is the one call a
    user whose token says `pwd_change_required` is allowed to make. The
    identity service still requires the current password.
    """
    if not request.cookies.get(COOKIE):
        raise HTTPException(status_code=401, detail="Not authenticated")
    session = _session(request)
    async with httpx.AsyncClient(timeout=10.0) as client:
        upstream = await client.post(
            f"{_setting('identity')}/auth/change-password",
            json=payload.model_dump(),
            headers={"authorization": f"Bearer {request.cookies.get(COOKIE)}",
                     "x-forwarded-for": _client_ip(request) or ""},
        )
    if upstream.status_code >= 400:
        detail = "Password change failed"
        try:
            detail = upstream.json().get("detail", detail)
        except ValueError:
            pass
        raise HTTPException(status_code=upstream.status_code, detail=detail)
    # The old token still says a change is required, so issue a fresh session
    # from a clean sign-in rather than trying to patch the claim.
    async with httpx.AsyncClient(timeout=10.0) as client:
        login = await client.post(
            f"{_setting('identity')}/auth/login",
            json={
                "username": str(session.get("username", "")),
                "password": payload.new_password,
            },
        )
    if login.status_code >= 400:
        return {"status": "password_changed", "reauthenticate": True}
    return {
        **_establish_session(login.json(), response, request),
        "status": "password_changed",
    }


@app.api_route("/api/identity/{path:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def identity_proxy(path: str, request: Request):
    """User, role and audit administration. Session endpoints stay closed."""
    if path.startswith(("auth/", "bootstrap/")):
        raise HTTPException(status_code=404, detail="Not found")
    return await _proxy("identity", path, request)


@app.api_route("/api/{service}/{path:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(service: str, path: str, request: Request):
    if service not in SERVICE_MAP or service == "identity":
        # Identity administration is exposed under its own route above;
        # nothing may reach the identity service through the generic proxy.
        raise HTTPException(status_code=404, detail="Not found")
    return await _proxy(service, path, request)


# --------------------------------------------------------------------------- UI
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not request.cookies.get(COOKIE):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC_DIR / "app.html", media_type="text/html")


@app.get("/login", response_class=HTMLResponse)
async def login_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "login.html", media_type="text/html")


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
