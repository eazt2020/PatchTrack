"""Role model and token handling.

Authorization must be enforceable server-side from the role alone; the UI is
convenience only. These tests pin the permission matrix so a role can never
silently gain a capability it was not granted.
"""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi import HTTPException

from patchtrack_common.config import get_settings
from patchtrack_common.security import (
    PERMISSIONS,
    ROLES,
    Principal,
    create_access_token,
    decode_token,
)

# Capabilities that must never be granted to a non-administrator role.
ADMIN_ONLY = {
    "users:manage",
    "policies:manage",
    "audit:view",
    "settings:manage",
    "integrations:manage",
}


def principal(role: str) -> Principal:
    return Principal(user_id="u1", username="someone", role=role)


def test_every_role_is_defined():
    assert set(PERMISSIONS) == set(ROLES)


def test_administrator_has_the_wildcard():
    assert PERMISSIONS["administrator"] == {"*"}
    assert principal("administrator").can("anything:at:all")


@pytest.mark.parametrize("role", ["viewer", "operations", "report_manager"])
@pytest.mark.parametrize("permission", sorted(ADMIN_ONLY))
def test_admin_only_capabilities_are_withheld(role, permission):
    assert not principal(role).can(permission)


@pytest.mark.parametrize("role", ["viewer", "operations", "report_manager"])
def test_every_non_admin_can_read_the_dashboard(role):
    assert principal(role).can("dashboard:view")


def test_viewer_cannot_run_a_sync_or_generate_a_report():
    viewer = principal("viewer")
    assert not viewer.can("sync:run")
    assert not viewer.can("reports:generate")
    assert not viewer.can("schedules:manage")


def test_operations_can_run_a_sync_but_not_generate_reports():
    operations = principal("operations")
    assert operations.can("sync:run")
    assert not operations.can("reports:generate")


def test_report_manager_can_generate_and_schedule_but_not_sync():
    manager = principal("report_manager")
    assert manager.can("reports:generate")
    assert manager.can("schedules:manage")
    assert not manager.can("sync:run")


def test_unknown_role_has_no_capabilities():
    assert not principal("superuser").can("dashboard:view")


def test_access_token_round_trip():
    token, expires_in = create_access_token(
        user_id="u1", username="someone", role="viewer", display_name="Some One")
    claims = decode_token(token)
    assert claims["sub"] == "u1"
    assert claims["role"] == "viewer"
    assert claims["username"] == "someone"
    assert expires_in == get_settings().access_token_minutes * 60


def test_token_does_not_demand_a_password_change_by_default():
    token, _ = create_access_token(
        user_id="u1", username="someone", role="viewer")
    assert decode_token(token)["pwd_change_required"] is False


def test_forced_rotation_is_carried_in_the_token():
    token, _ = create_access_token(
        user_id="u1", username="someone", role="viewer",
        must_change_password=True)
    # The gateway refuses every other route while this claim is true, so it
    # must survive encode/decode — a claim the token shape silently dropped
    # would leave forced rotation unenforced.
    assert decode_token(token)["pwd_change_required"] is True


def test_token_signed_with_another_key_is_rejected():
    forged = jwt.encode(
        {"sub": "u1", "username": "admin", "role": "administrator",
         "iat": int(time.time()), "exp": int(time.time()) + 600},
        "a-different-key-that-is-long-enough-to-sign",
        algorithm="HS256",
    )
    with pytest.raises(HTTPException) as excinfo:
        decode_token(forged)
    assert excinfo.value.status_code == 401


def test_expired_token_is_rejected():
    now = int(time.time())
    # Well-formed in every respect except expiry, so the failure is genuinely
    # the expiry check rather than a missing-claim rejection.
    expired = jwt.encode(
        {"sub": "u1", "username": "someone", "role": "viewer",
         "iat": now - 7200, "exp": now - 3600,
         "iss": "patchtrack-identity", "aud": "patchtrack"},
        get_settings().jwt_secret,
        algorithm=get_settings().jwt_algorithm,
    )
    with pytest.raises(HTTPException) as excinfo:
        decode_token(expired)
    assert excinfo.value.status_code == 401
    assert "expired" in excinfo.value.detail.lower()


def test_unsigned_token_is_rejected():
    unsigned = jwt.encode({"sub": "u1", "role": "administrator"},
                          key="", algorithm="HS256")
    with pytest.raises(HTTPException) as excinfo:
        decode_token(unsigned)
    assert excinfo.value.status_code == 401


def test_token_without_required_claims_is_rejected():
    settings = get_settings()
    partial = jwt.encode({"sub": "u1", "role": "administrator"},
                         settings.jwt_secret, algorithm=settings.jwt_algorithm)
    with pytest.raises(HTTPException) as excinfo:
        decode_token(partial)
    assert excinfo.value.status_code == 401
