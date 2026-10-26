"""Audit trail writer shared by all services.

Audit records deliberately exclude passwords, tokens and request payload
bodies. Only an explicit, caller-provided context dictionary is stored.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from .db import connection
from .security import Principal

SENSITIVE_KEYS = {
    "password",
    "password_hash",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "private_key",
    "authorization",
}


def scrub(context: dict[str, Any] | None) -> dict[str, Any]:
    if not context:
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in context.items():
        if key.lower() in SENSITIVE_KEYS:
            cleaned[key] = "[redacted]"
        elif isinstance(value, dict):
            cleaned[key] = scrub(value)
        else:
            cleaned[key] = value
    return cleaned


def record_audit(
    *,
    event_type: str,
    outcome: str,
    principal: Principal | None = None,
    actor: str | None = None,
    actor_role: str | None = None,
    target_type: str = "",
    target_id: str = "",
    context: dict[str, Any] | None = None,
    source_ip: str | None = None,
    service: str = "",
) -> None:
    actor_name = actor or (principal.username if principal else "system")
    role = actor_role or (principal.role if principal else "system")
    with connection() as conn:
        conn.execute(
            text(
                """
                INSERT INTO shared.audit_events
                    (occurred_at, actor, actor_role, event_type, target_type,
                     target_id, outcome, context, source_ip, service)
                VALUES (now(), :actor, :role, :event_type, :target_type,
                        :target_id, :outcome, CAST(:context AS jsonb),
                        CAST(:source_ip AS inet), :service)
                """
            ),
            {
                "actor": actor_name,
                "role": role,
                "event_type": event_type,
                "target_type": target_type,
                "target_id": target_id,
                "outcome": outcome,
                "context": _json(scrub(context)),
                "source_ip": source_ip,
                "service": service,
            },
        )


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, default=str)
