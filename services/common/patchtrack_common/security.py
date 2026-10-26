"""Authentication primitives and role-based authorization.

Tokens are signed JWTs shared between services. Authorization is always
enforced in the backend by :func:`require_permission`, never by the UI.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import get_settings

ROLES: tuple[str, ...] = ("viewer", "operations", "report_manager", "administrator")

PERMISSIONS: dict[str, set[str]] = {
    "viewer": {
        "dashboard:view",
        "inventory:view",
        "inventory:detail",
        "compliance:view",
        "reports:view",
        "reports:download",
    },
    "operations": {
        "dashboard:view",
        "inventory:view",
        "inventory:detail",
        # The operators who run collection are the ones who see a wrong
        # environment or business service in AssetExplorer and know the right
        # one. The override is attributed and audited, and never writes the
        # source value.
        "inventory:manage",
        "compliance:view",
        "findings:view",
        "integrations:view",
        "sync:run",
        "reports:view",
        "reports:download",
    },
    "report_manager": {
        "dashboard:view",
        "inventory:view",
        "compliance:view",
        "reports:view",
        "reports:generate",
        "reports:download",
        "schedules:manage",
    },
    "administrator": {"*"},
}

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    role: str
    display_name: str = ""
    auth_method: str = "password"

    def can(self, permission: str) -> bool:
        granted = PERMISSIONS.get(self.role, set())
        return "*" in granted or permission in granted


def create_access_token(
    *, user_id: str, username: str, role: str, display_name: str = "",
    must_change_password: bool = False,
) -> tuple[str, int]:
    settings = get_settings()
    expires_in = settings.access_token_minutes * 60
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": user_id,
        "username": username,
        "role": role,
        "name": display_name,
        # Carried in the token so the gateway can refuse everything else until
        # the forced rotation is satisfied, without a database call per request.
        "pwd_change_required": bool(must_change_password),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
        "iss": "patchtrack-identity",
        "aud": "patchtrack",
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_in


def decode_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience="patchtrack",
            issuer="patchtrack-identity",
            options={"require": ["exp", "sub", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired"
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        ) from exc


def principal_from_request(request: Request) -> Principal:
    """Resolve the caller from a bearer token or the trusted gateway headers.

    Gateway headers are only honoured when the request arrives from the
    gateway and carries the internal service token; direct clients cannot
    spoof a role.
    """
    settings = get_settings()
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        payload = decode_token(auth_header.split(" ", 1)[1])
        role = str(payload.get("role", "viewer"))
        if role not in ROLES:
            raise HTTPException(status_code=403, detail="Unknown role")
        return Principal(
            user_id=str(payload["sub"]),
            username=str(payload.get("username", "")),
            role=role,
            display_name=str(payload.get("name", "")),
        )

    internal_token = request.headers.get("x-patchtrack-internal")
    gateway_user = request.headers.get("x-patchtrack-user")
    gateway_role = request.headers.get("x-patchtrack-role")
    if (
        settings.service_token
        and internal_token
        and gateway_user
        and gateway_role in ROLES
        and _constant_time_equal(internal_token, settings.service_token)
    ):
        return Principal(
            user_id=request.headers.get("x-patchtrack-user-id", ""),
            username=gateway_user,
            role=gateway_role,
            display_name=request.headers.get("x-patchtrack-display-name", ""),
            auth_method="gateway",
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _constant_time_equal(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode(), b.encode())


def get_principal(request: Request) -> Principal:
    return principal_from_request(request)


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def require_permission(permission: str):
    def _dep(principal: PrincipalDep) -> Principal:
        if not principal.can(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing permission: {permission}",
            )
        return principal

    return _dep


def optional_bearer(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> dict[str, Any] | None:
    if credentials is None:
        return None
    return decode_token(credentials.credentials)
