"""Audit-trail retention is a dynamic setting, not a deployment constant.

The trail stays append-only except for the retention purge:

- the window is stored in shared.app_settings, so changing it needs no
  restart and every service sees the same value;
- the purge function enforces the window itself and accepts no cutoff, so a
  caller cannot delete more than the configured policy allows;
- UPDATE and an ordinary DELETE are still refused by the trigger;
- the purge is itself audited after the delete.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from identity.app.main import (
    AUDIT_RETENTION_DEFAULT,
    AuditRetentionUpdate,
    get_audit_retention,
    purge_audit_trail,
    set_audit_retention,
)
from patchtrack_common.db import fetch_one
from patchtrack_common.security import Principal

ADMIN = Principal(
    user_id="00000000-0000-0000-0000-000000000002",
    username="admin",
    role="administrator",
    display_name="Administrator",
)


class _Request:
    """Only `request.client` is read, via patchtrack_common.net.client_ip."""

    client = SimpleNamespace(host="127.0.0.1")
    headers: dict[str, str] = {}


def insert_event(engine, event_type: str, occurred_at: datetime) -> int:
    with engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO shared.audit_events "
                "(occurred_at, actor, actor_role, event_type, outcome) "
                "VALUES (:at, 'tester', 'administrator', :type, 'success') "
                "RETURNING id"
            ),
            {"at": occurred_at, "type": event_type},
        ).scalar_one()


def event_exists(engine, event_id: int) -> bool:
    with engine.begin() as conn:
        return conn.execute(
            text("SELECT 1 FROM shared.audit_events WHERE id = :id"),
            {"id": event_id},
        ).first() is not None


@pytest.fixture
def default_retention(engine):
    with engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM shared.app_settings WHERE key = 'audit_retention_days'"
        ))
    yield


def test_the_default_window_is_used_until_an_administrator_changes_it(
    default_retention,
) -> None:
    value = get_audit_retention(ADMIN)
    assert value["days"] == AUDIT_RETENTION_DEFAULT == 2555
    assert value["min_days"] == 30
    assert value["max_days"] == 36500
    assert value["updated_at"] is None


def test_the_window_is_stored_and_read_back(engine) -> None:
    assert set_audit_retention(
        AuditRetentionUpdate(days=730), _Request(), ADMIN
    ) == {"days": 730}

    value = get_audit_retention(ADMIN)
    assert value["days"] == 730
    assert value["updated_by"] == "admin"

    stored = fetch_one(
        "SELECT value FROM shared.app_settings WHERE key = 'audit_retention_days'"
    )
    assert stored["value"] == {"days": 730}

    # Changing the setting is itself an audit event.
    changed = fetch_one(
        "SELECT context FROM shared.audit_events "
        "WHERE event_type = 'settings.updated' AND target_id = 'audit_retention_days' "
        "ORDER BY id DESC"
    )
    assert changed["context"] == {"days": 730}


@pytest.mark.parametrize("days", [0, 29, 36501])
def test_a_window_outside_the_safe_range_is_refused(days: int) -> None:
    with pytest.raises(ValidationError):
        AuditRetentionUpdate(days=days)


def test_purge_removes_only_events_older_than_the_window(engine) -> None:
    set_audit_retention(AuditRetentionUpdate(days=365), _Request(), ADMIN)
    now = datetime.now(UTC)
    expired = insert_event(engine, "test.retention.expired", now - timedelta(days=400))
    kept = insert_event(engine, "test.retention.kept", now - timedelta(days=10))

    result = purge_audit_trail(ADMIN)

    assert result["status"] == "ok"
    assert result["retention_days"] == 365
    assert result["purged"] >= 1
    assert not event_exists(engine, expired)
    assert event_exists(engine, kept)

    purged = fetch_one(
        "SELECT context FROM shared.audit_events "
        "WHERE event_type = 'audit.purged' ORDER BY id DESC"
    )
    assert purged["context"]["retention_days"] == 365


def test_the_trail_is_still_append_only_outside_the_purge(engine) -> None:
    event_id = insert_event(engine, "test.retention.immutable", datetime.now(UTC))

    with pytest.raises(DBAPIError, match="append-only"):
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE shared.audit_events SET actor = 'someone-else' "
                     "WHERE id = :id"),
                {"id": event_id},
            )

    with pytest.raises(DBAPIError, match="append-only"):
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM shared.audit_events WHERE id = :id"),
                {"id": event_id},
            )

    assert event_exists(engine, event_id)


def test_the_purge_function_refuses_an_unsafe_stored_window(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO shared.app_settings (key, value) "
            "VALUES ('audit_retention_days', '{\"days\": 5}'::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        ))

    try:
        with pytest.raises(DBAPIError, match="between 30 and 36500"):
            fetch_one("SELECT shared.purge_audit_events()")
    finally:
        with engine.begin() as conn:
            conn.execute(text(
                "DELETE FROM shared.app_settings "
                "WHERE key = 'audit_retention_days'"
            ))
