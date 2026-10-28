"""PatchTrack identity service: authentication, users, roles, audit queries."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID, uuid4

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from patchtrack_common.audit import record_audit
from patchtrack_common.config import get_settings
from patchtrack_common.db import connection, execute, fetch_all, fetch_one
from patchtrack_common.net import client_ip
from patchtrack_common.security import (
    PERMISSIONS,
    ROLES,
    Principal,
    PrincipalDep,
    create_access_token,
    require_permission,
)

APP = "identity"
app = FastAPI(title="PatchTrack Identity", version="1.0.0", docs_url="/docs")

_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


# --------------------------------------------------------------------------- models
class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=512)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_token: str
    user: dict[str, Any]


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=150, pattern=r"^[A-Za-z0-9._-]+$")
    display_name: str = Field(default="", max_length=200)
    email: EmailStr | None = None
    role: str
    password: str = Field(min_length=12, max_length=512)


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    email: EmailStr | None = None
    role: str | None = None
    is_active: bool | None = None


class PasswordReset(BaseModel):
    password: str = Field(min_length=12, max_length=512)


def _client_ip(request: Request) -> str | None:
    """The caller's address, or None when it cannot be established.

    The ASGI server has already applied ``--forwarded-allow-ips``; nothing is
    taken from the raw header here. See ``patchtrack_common.net``.
    """
    return client_ip(request)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _as_uuid(value: str) -> UUID | None:
    """Parse a principal's user id, or return None when it is not one.

    Service-token callers (the scheduler) authenticate without a user id, so
    a route that needs an account must say so rather than passing "" to
    PostgreSQL as a UUID.
    """
    try:
        return UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


def _issue_tokens(user: dict[str, Any], request: Request) -> TokenResponse:
    settings = get_settings()
    access, expires_in = create_access_token(
        user_id=str(user["id"]),
        username=user["username"],
        role=user["role"],
        display_name=user.get("display_name") or "",
        must_change_password=bool(user.get("must_change_password")),
    )
    refresh = secrets.token_urlsafe(48)
    with connection() as conn:
        conn.execute(
            text(
                """
                INSERT INTO identity.refresh_tokens
                    (user_id, token_hash, expires_at, user_agent, source_ip)
                VALUES (:user_id, :hash, :expires,
                        :agent, CAST(:ip AS inet))
                """
            ),
            {
                "user_id": user["id"],
                "hash": _hash_token(refresh),
                "expires": datetime.now(UTC)
                + timedelta(hours=settings.refresh_token_hours),
                "agent": request.headers.get("user-agent", "")[:400],
                "ip": _client_ip(request),
            },
        )
    return TokenResponse(
        access_token=access,
        expires_in=expires_in,
        refresh_token=refresh,
        user=_public_user(user),
    )


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(user["id"]),
        "username": user["username"],
        "display_name": user.get("display_name") or "",
        "email": user.get("email"),
        "role": user["role"],
        "is_active": user.get("is_active", True),
        "auth_source": user.get("auth_source", "local"),
        "last_login_at": user.get("last_login_at"),
        "must_change_password": user.get("must_change_password", False),
        "permissions": sorted(PERMISSIONS.get(user["role"], set())),
    }


# --------------------------------------------------------------------------- health
@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": APP}


@app.get("/readyz")
def readyz() -> dict[str, str]:
    try:
        fetch_one("SELECT 1 AS ok")
    except Exception as exc:  # pragma: no cover - depends on DB availability
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ready", "service": APP}


# --------------------------------------------------------------------------- auth
@app.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request) -> TokenResponse:
    username = payload.username.strip()
    ip = _client_ip(request)
    user = fetch_one(
        "SELECT * FROM identity.users WHERE username = :u", u=username
    )

    def _fail(reason: str) -> None:
        with connection() as conn:
            conn.execute(
                text(
                    "INSERT INTO identity.login_attempts "
                    "(username, succeeded, source_ip, detail) VALUES "
                    "(:u, false, CAST(:ip AS inet), :d)"
                ),
                {"u": username, "ip": ip, "d": reason},
            )
        record_audit(
            event_type="auth.login",
            outcome="failure",
            actor=username,
            actor_role="anonymous",
            target_type="user",
            target_id=username,
            context={"reason": reason},
            source_ip=ip,
            service=APP,
        )

    if user is None or not user["is_active"]:
        _fail("invalid_credentials")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )
    if user["auth_source"] != "local":
        _fail("external_identity")
        raise HTTPException(
            status_code=400,
            detail="This account authenticates through the enterprise identity provider",
        )
    if user.get("locked_until") and user["locked_until"] > datetime.now(UTC):
        _fail("locked")
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="Account temporarily locked after repeated failed sign-in",
        )
    try:
        _hasher.verify(user["password_hash"] or "", payload.password)
    except (VerifyMismatchError, Exception):  # noqa: B014 - argon2 raises varied errors
        attempts = user.get("failed_attempts", 0) + 1
        lock_until = (
            datetime.now(UTC) + timedelta(minutes=LOCKOUT_MINUTES)
            if attempts >= MAX_FAILED_ATTEMPTS
            else None
        )
        with connection() as conn:
            conn.execute(
                text(
                    "UPDATE identity.users SET failed_attempts = :a, locked_until = :l "
                    "WHERE id = :id"
                ),
                {"a": attempts, "l": lock_until, "id": user["id"]},
            )
        _fail("invalid_credentials")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        ) from None

    with connection() as conn:
        conn.execute(
            text(
                "UPDATE identity.users SET failed_attempts = 0, locked_until = NULL, "
                "last_login_at = now() WHERE id = :id"
            ),
            {"id": user["id"]},
        )
        conn.execute(
            text(
                "INSERT INTO identity.login_attempts (username, succeeded, source_ip) "
                "VALUES (:u, true, CAST(:ip AS inet))"
            ),
            {"u": username, "ip": ip},
        )
    record_audit(
        event_type="auth.login",
        outcome="success",
        actor=username,
        actor_role=user["role"],
        target_type="user",
        target_id=str(user["id"]),
        source_ip=ip,
        service=APP,
    )
    return _issue_tokens(user, request)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=20)


@app.post("/auth/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, request: Request) -> TokenResponse:
    row = fetch_one(
        """
        SELECT u.*, rt.id AS token_id FROM identity.refresh_tokens rt
        JOIN identity.users u ON u.id = rt.user_id
        WHERE rt.token_hash = :hash AND rt.revoked_at IS NULL
          AND rt.expires_at > now() AND u.is_active
        """,
        hash=_hash_token(payload.refresh_token),
    )
    if row is None:
        record_audit(
            event_type="auth.refresh",
            outcome="failure",
            actor="unknown",
            actor_role="anonymous",
            context={"reason": "invalid_refresh_token"},
            source_ip=_client_ip(request),
            service=APP,
        )
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    with connection() as conn:
        conn.execute(
            text("UPDATE identity.refresh_tokens SET revoked_at = now() WHERE id = :id"),
            {"id": row["token_id"]},
        )
    return _issue_tokens(row, request)


@app.post("/auth/logout")
def logout(payload: RefreshRequest) -> dict[str, str]:
    with connection() as conn:
        conn.execute(
            text(
                "UPDATE identity.refresh_tokens SET revoked_at = now() "
                "WHERE token_hash = :h AND revoked_at IS NULL"
            ),
            {"h": _hash_token(payload.refresh_token)},
        )
    return {"status": "logged_out"}


@app.get("/auth/me")
def me(principal: PrincipalDep) -> dict[str, Any]:
    user = fetch_one(
        "SELECT * FROM identity.users WHERE id = :id", id=principal.user_id
    )
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return _public_user(user)


@app.get("/roles")
def list_roles(_: Annotated[Principal, Depends(require_permission("users:manage"))]) -> list[dict[str, Any]]:
    return [
        {"role": role, "permissions": sorted(PERMISSIONS[role])} for role in ROLES
    ]


# --------------------------------------------------------------------------- users
@app.get("/users")
def list_users(
    _: Annotated[Principal, Depends(require_permission("users:manage"))],
) -> list[dict[str, Any]]:
    rows = fetch_all(
        "SELECT * FROM identity.users ORDER BY username"
    )
    return [_public_user(r) for r in rows]


@app.post("/users", status_code=201)
def create_user(
    payload: UserCreate,
    principal: Annotated[Principal, Depends(require_permission("users:manage"))],
    request: Request,
) -> dict[str, Any]:
    if payload.role not in ROLES:
        raise HTTPException(status_code=422, detail="Unknown role")
    try:
        with connection() as conn:
            row = conn.execute(
                text(
                    """
                    INSERT INTO identity.users
                        (username, display_name, email, password_hash, role,
                         auth_source, must_change_password, created_by)
                    VALUES (:u, :d, :e, :p, :r, 'local', true, :by)
                    RETURNING *
                    """
                ),
                {
                    "u": payload.username,
                    "d": payload.display_name,
                    "e": str(payload.email) if payload.email else None,
                    "p": _hasher.hash(payload.password),
                    "r": payload.role,
                    "by": principal.username,
                },
            ).mappings().first()
    except IntegrityError:
        raise HTTPException(status_code=409, detail="Username or email already exists") from None
    record_audit(
        event_type="user.created",
        outcome="success",
        principal=principal,
        target_type="user",
        target_id=str(row["id"]),
        context={"username": payload.username, "role": payload.role},
        source_ip=_client_ip(request),
        service=APP,
    )
    return _public_user(dict(row))


@app.patch("/users/{user_id}")
def update_user(
    user_id: UUID,
    payload: UserUpdate,
    principal: Annotated[Principal, Depends(require_permission("users:manage"))],
    request: Request,
) -> dict[str, Any]:
    changes = payload.model_dump(exclude_unset=True)
    if "role" in changes and changes["role"] not in ROLES:
        raise HTTPException(status_code=422, detail="Unknown role")
    if not changes:
        raise HTTPException(status_code=400, detail="No changes supplied")
    existing = fetch_one("SELECT * FROM identity.users WHERE id = :id", id=user_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="User not found")
    if (
        existing["role"] == "administrator"
        and (changes.get("role") not in (None, "administrator")
             or changes.get("is_active") is False)
    ):
        admins = fetch_one(
            "SELECT count(*) AS n FROM identity.users "
            "WHERE role = 'administrator' AND is_active"
        )
        if admins and admins["n"] <= 1:
            raise HTTPException(
                status_code=409,
                detail="Cannot remove the last active administrator",
            )
    assignments = ", ".join(f"{key} = :{key}" for key in changes)
    with connection() as conn:
        row = conn.execute(
            text(
                f"UPDATE identity.users SET {assignments}, updated_at = now() "
                f"WHERE id = :id RETURNING *"
            ),
            {**changes, "id": user_id},
        ).mappings().first()
    record_audit(
        event_type="user.updated",
        outcome="success",
        principal=principal,
        target_type="user",
        target_id=str(user_id),
        context={"fields": sorted(changes)},
        source_ip=_client_ip(request),
        service=APP,
    )
    return _public_user(dict(row))


@app.post("/users/{user_id}/password")
def reset_password(
    user_id: UUID,
    payload: PasswordReset,
    principal: Annotated[Principal, Depends(require_permission("users:manage"))],
    request: Request,
) -> dict[str, str]:
    updated = execute(
        "UPDATE identity.users SET password_hash = :p, must_change_password = true, "
        "failed_attempts = 0, locked_until = NULL, updated_at = now() WHERE id = :id",
        p=_hasher.hash(payload.password),
        id=user_id,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")
    record_audit(
        event_type="user.password_reset",
        outcome="success",
        principal=principal,
        target_type="user",
        target_id=str(user_id),
        source_ip=_client_ip(request),
        service=APP,
    )
    return {"status": "password_reset"}


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=12, max_length=512)


@app.post("/auth/change-password")
def change_password(
    payload: PasswordChange,
    principal: PrincipalDep,
    request: Request,
) -> dict[str, str]:
    """Change one's own password. This is how a forced rotation is satisfied.

    The current password is required even though the caller is already
    authenticated: a stolen session cookie must not be enough to take over the
    account by changing its password.
    """
    user_id = _as_uuid(principal.user_id)
    if user_id is None:
        # A service-token caller carries no user identity, so there is no
        # account here to change; answer rather than let PostgreSQL reject the
        # empty string as a malformed UUID.
        raise HTTPException(status_code=403, detail="No user account in this session")
    user = fetch_one("SELECT * FROM identity.users WHERE id = :id", id=user_id)
    if user is None or not user["is_active"]:
        raise HTTPException(status_code=404, detail="User not found")
    if user["auth_source"] != "local":
        raise HTTPException(
            status_code=400,
            detail="This account authenticates through the enterprise identity provider",
        )
    try:
        _hasher.verify(user["password_hash"] or "", payload.current_password)
    except (VerifyMismatchError, Exception):  # noqa: B014 - argon2 raises varied errors
        record_audit(
            event_type="user.password_changed", outcome="failure",
            principal=principal, target_type="user",
            target_id=str(user["id"]),
            context={"reason": "current_password_mismatch"},
            source_ip=_client_ip(request), service=APP,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect",
        ) from None
    if payload.new_password == payload.current_password:
        raise HTTPException(
            status_code=422, detail="The new password must be different"
        )
    with connection() as conn:
        conn.execute(
            text(
                "UPDATE identity.users SET password_hash = :p, "
                "must_change_password = false, failed_attempts = 0, "
                "locked_until = NULL, updated_at = now() WHERE id = :id"
            ),
            {"p": _hasher.hash(payload.new_password), "id": user["id"]},
        )
        # Every other session was opened with credentials that no longer
        # exist; revoking the refresh tokens makes the change take effect for
        # anyone else holding a session.
        conn.execute(
            text(
                "UPDATE identity.refresh_tokens SET revoked_at = now() "
                "WHERE user_id = :id AND revoked_at IS NULL"
            ),
            {"id": user["id"]},
        )
    record_audit(
        event_type="user.password_changed", outcome="success",
        principal=principal, target_type="user", target_id=str(user["id"]),
        source_ip=_client_ip(request), service=APP,
    )
    return {"status": "password_changed"}


# --------------------------------------------------------------------------- audit
@app.get("/audit")
def list_audit(
    _: Annotated[Principal, Depends(require_permission("audit:view"))],
    limit: int = 100,
    offset: int = 0,
    event_type: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    where = ["1=1"]
    params: dict[str, Any] = {"limit": limit, "offset": max(0, offset)}
    if event_type:
        where.append("event_type = :event_type")
        params["event_type"] = event_type
    if actor:
        where.append("actor = :actor")
        params["actor"] = actor
    clause = " AND ".join(where)
    rows = fetch_all(
        f"SELECT id, occurred_at, actor, actor_role, event_type, target_type, "
        f"target_id, outcome, context, host(source_ip) AS source_ip, service "
        f"FROM shared.audit_events WHERE {clause} "
        "ORDER BY occurred_at DESC LIMIT :limit OFFSET :offset",
        **params,
    )
    total = fetch_one(
        f"SELECT count(*) AS n FROM shared.audit_events WHERE {clause}",
        **{k: v for k, v in params.items() if k not in ("limit", "offset")},
    )
    return {"items": rows, "total": total["n"] if total else 0}


# --------------------------------------------------------------------------- audit retention
# The trail is append-only; retention is the one sanctioned deletion. The
# window lives in shared.app_settings so it can be changed without a restart,
# and only the purge function in migration 012 acts on it. Keeping the policy
# in the database also means every instance reads the same value.
AUDIT_RETENTION_KEY = "audit_retention_days"
AUDIT_RETENTION_DEFAULT = 2555
AUDIT_RETENTION_MIN = 30
AUDIT_RETENTION_MAX = 36500


class AuditRetentionUpdate(BaseModel):
    days: int = Field(ge=AUDIT_RETENTION_MIN, le=AUDIT_RETENTION_MAX)


def _stored_retention_days() -> tuple[int, dict[str, Any] | None]:
    row = fetch_one(
        "SELECT value, updated_at, updated_by FROM shared.app_settings "
        "WHERE key = :key",
        key=AUDIT_RETENTION_KEY,
    )
    if row is None:
        return AUDIT_RETENTION_DEFAULT, None
    value = row["value"]
    if isinstance(value, str):
        value = json.loads(value)
    stored = value.get("days") if isinstance(value, dict) else value
    try:
        days = int(stored)
    except (TypeError, ValueError):
        days = AUDIT_RETENTION_DEFAULT
    return days, row


@app.get("/settings/audit-retention")
def get_audit_retention(
    _: Annotated[Principal, Depends(require_permission("settings:manage"))],
) -> dict[str, Any]:
    days, row = _stored_retention_days()
    events = fetch_one(
        "SELECT count(*) AS total, min(occurred_at) AS oldest "
        "FROM shared.audit_events"
    ) or {}
    return {
        "days": days,
        "min_days": AUDIT_RETENTION_MIN,
        "max_days": AUDIT_RETENTION_MAX,
        "updated_at": row["updated_at"] if row else None,
        "updated_by": row["updated_by"] if row else None,
        "events": events.get("total", 0),
        "oldest": events.get("oldest"),
    }


@app.put("/settings/audit-retention")
def set_audit_retention(
    payload: AuditRetentionUpdate,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("settings:manage"))],
) -> dict[str, Any]:
    """Change the retention window; the next purge run applies it."""
    value = json.dumps({"days": payload.days})
    execute(
        """
        INSERT INTO shared.app_settings (key, value, updated_at, updated_by)
        VALUES (:key, CAST(:v AS jsonb), now(), :by)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
            updated_at = now(), updated_by = EXCLUDED.updated_by
        """,
        key=AUDIT_RETENTION_KEY, v=value, by=principal.username,
    )
    record_audit(
        event_type="settings.updated", outcome="success", principal=principal,
        target_type="setting", target_id=AUDIT_RETENTION_KEY,
        context={"days": payload.days}, source_ip=_client_ip(request), service=APP,
    )
    return {"days": payload.days}


@app.post("/jobs/audit-purge")
def purge_audit_trail(
    principal: Annotated[Principal, Depends(require_permission("settings:manage"))],
) -> dict[str, Any]:
    """Apply the retention window. Safe to run repeatedly; it is the scheduler
    that decides how often. The purge itself is audited after the delete, so
    the record of it cannot be removed by the same run."""
    row = fetch_one("SELECT shared.purge_audit_events() AS result")
    result = (row or {}).get("result") or {}
    if isinstance(result, str):
        result = json.loads(result)
    record_audit(
        event_type="audit.purged", outcome="success", principal=principal,
        target_type="audit_trail", target_id="shared.audit_events",
        context={
            "purged": result.get("purged", 0),
            "retention_days": result.get("retention_days"),
            "cutoff": result.get("cutoff"),
            "trigger": "scheduler",
        },
        service=APP,
    )
    return {
        "status": "ok",
        "purged": result.get("purged", 0),
        "retention_days": result.get("retention_days"),
        "cutoff": result.get("cutoff"),
    }


@app.get("/bootstrap/status")
def bootstrap_status() -> dict[str, Any]:
    """Public endpoint used by the gateway to prompt first-run setup."""
    row = fetch_one("SELECT count(*) AS n FROM identity.users")
    return {"needs_admin": bool(row and row["n"] == 0)}
