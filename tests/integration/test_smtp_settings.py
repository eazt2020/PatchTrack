"""The SMTP relay is configured on the Settings screen, not in the environment.

The saved row is authoritative and applies to the next delivery without a
restart. The password is accepted write-only, encrypted at rest, and never
included in a response or an audit event. The environment remains the fallback
for an installation that has never saved settings.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from patchtrack_common.config import get_settings
from patchtrack_common.db import fetch_one
from patchtrack_common.security import Principal
from reporting.app.main import (
    SmtpSettingsUpdate,
    SmtpTestRequest,
    get_smtp_settings,
    set_smtp_settings,
    send_smtp_test,
)
from reporting.app.smtp_config import (
    decrypt_password,
    encrypt_password,
    load_stored,
    resolve_smtp,
)

ADMIN = Principal(
    user_id="00000000-0000-0000-0000-000000000002",
    username="admin",
    role="administrator",
    display_name="Administrator",
)


class _Request:
    client = SimpleNamespace(host="127.0.0.1")
    headers: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _database(migrated_database):
    return migrated_database


@pytest.fixture(autouse=True)
def _clean_settings(engine, monkeypatch):
    with engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM shared.app_settings WHERE key = 'smtp_relay'"
        ))
    monkeypatch.setenv("PATCHTRACK_SECRETS_KEY", "test-secrets-key-0123456789abcdef")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    with engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM shared.app_settings WHERE key = 'smtp_relay'"
        ))


def _save(**overrides):
    payload = {
        "host": "relay.example.com",
        "port": 587,
        "starttls": True,
        "username": "reports",
        "sender": "patchtrack@example.com",
    }
    payload.update(overrides)
    return set_smtp_settings(SmtpSettingsUpdate(**payload), _Request(), ADMIN)


def test_encryption_round_trip_and_a_rotated_key_is_detected(monkeypatch):
    token = encrypt_password("s3cret")
    assert token != "s3cret"
    assert decrypt_password(token) == "s3cret"

    monkeypatch.setenv("PATCHTRACK_SECRETS_KEY", "a-different-key-0123456789abcdef")
    get_settings.cache_clear()
    assert decrypt_password(token) is None


def test_the_environment_is_used_until_settings_are_saved(engine, monkeypatch):
    monkeypatch.setenv("PATCHTRACK_SMTP_HOST", "env-relay.example.com")
    monkeypatch.setenv("PATCHTRACK_SMTP_PORT", "2525")
    monkeypatch.setenv("PATCHTRACK_SMTP_PASSWORD", "env-password")
    monkeypatch.setenv("PATCHTRACK_SMTP_SENDER", "env@example.com")
    get_settings.cache_clear()

    config = resolve_smtp()
    assert config.host == "env-relay.example.com"
    assert config.port == 2525
    assert config.password == "env-password"
    assert config.source == "environment"

    view = get_smtp_settings(ADMIN)
    assert view["source"] == "environment"
    assert view["password_set"] is True
    assert "password" not in view
    assert view["updated_at"] is None


def test_saving_encrypts_the_password_and_never_returns_it(engine):
    view = _save(password="relay-secret")

    assert view["source"] == "database"
    assert view["host"] == "relay.example.com"
    assert view["password_set"] is True
    assert "password" not in view

    stored = fetch_one(
        "SELECT value FROM shared.app_settings WHERE key = 'smtp_relay'"
    )["value"]
    assert stored["password"] != "relay-secret"
    assert "relay-secret" not in json.dumps(stored)
    assert decrypt_password(stored["password"]) == "relay-secret"

    assert resolve_smtp().password == "relay-secret"


def test_saving_without_a_password_keeps_the_saved_one(engine, monkeypatch):
    _save(password="first-secret")
    monkeypatch.setenv("PATCHTRACK_SMTP_PASSWORD", "environment-secret")
    get_settings.cache_clear()

    _save(host="relay2.example.com", password=None)

    stored = load_stored()
    assert stored["host"] == "relay2.example.com"
    assert decrypt_password(stored["password"]) == "first-secret"
    assert resolve_smtp().password == "first-secret"


def test_the_first_save_carries_over_an_unchanged_environment_password(
    engine, monkeypatch,
):
    monkeypatch.setenv("PATCHTRACK_SMTP_HOST", "env-relay.example.com")
    monkeypatch.setenv("PATCHTRACK_SMTP_USERNAME", "reports")
    monkeypatch.setenv("PATCHTRACK_SMTP_PASSWORD", "environment-secret")
    monkeypatch.setenv("PATCHTRACK_SMTP_SENDER", "env@example.com")
    get_settings.cache_clear()

    _save(username="reports", password=None)

    assert resolve_smtp().password == "environment-secret"
    assert resolve_smtp().source == "database"


def test_clearing_the_password_removes_auth_without_a_restart(engine):
    _save(password="relay-secret")

    view = _save(clear_password=True)

    assert view["password_set"] is False
    assert resolve_smtp().password == ""
    assert load_stored()["password"] == ""


def test_a_password_and_a_clear_request_cannot_be_combined(engine):
    with pytest.raises(HTTPException) as excinfo:
        _save(password="relay-secret", clear_password=True)
    assert excinfo.value.status_code == 422


def test_a_sender_is_required_when_a_relay_is_set(engine):
    with pytest.raises(HTTPException) as excinfo:
        _save(sender="not-an-address")
    assert excinfo.value.status_code == 422


def test_audit_records_the_change_without_the_password(engine):
    _save(password="relay-secret")

    events = fetch_one(
        "SELECT context FROM shared.audit_events "
        "WHERE event_type = 'settings.updated' AND target_id = 'smtp_relay' "
        "ORDER BY id DESC"
    )
    context = events["context"]
    assert context["host"] == "relay.example.com"
    assert context["password_changed"] is True
    assert "relay-secret" not in json.dumps(context)
    assert "password" not in context


def test_a_server_without_a_relay_can_still_be_saved(engine):
    view = _save(host="", username="", sender="")
    assert view["host"] == ""
    assert resolve_smtp().host == ""


def _test_email(recipient="owner@example.com"):
    return send_smtp_test(
        SmtpTestRequest(recipient=recipient), _Request(), ADMIN
    )


def test_a_successful_test_records_success_without_the_message(
    engine, monkeypatch,
):
    import reporting.app.main as reporting_main

    _save(password="relay-secret")
    monkeypatch.setattr(
        reporting_main, "send_test_email", lambda **_: ("sent", "")
    )

    assert _test_email() == {"status": "sent"}
    event = fetch_one(
        "SELECT outcome, context FROM shared.audit_events "
        "WHERE event_type = 'settings.smtp_tested' ORDER BY id DESC"
    )
    assert event["outcome"] == "success"
    assert event["context"]["host"] == "relay.example.com"
    assert "relay-secret" not in json.dumps(event["context"])


def test_a_failed_test_is_a_502_and_an_audited_failure(engine, monkeypatch):
    import reporting.app.main as reporting_main

    _save(password="relay-secret")
    monkeypatch.setattr(
        reporting_main, "send_test_email", lambda **_: ("failed", "connection refused")
    )

    with pytest.raises(HTTPException) as excinfo:
        _test_email()
    assert excinfo.value.status_code == 502
    assert "connection refused" in str(excinfo.value.detail)
    event = fetch_one(
        "SELECT outcome, context FROM shared.audit_events "
        "WHERE event_type = 'settings.smtp_tested' ORDER BY id DESC"
    )
    assert event["outcome"] == "failure"
    assert event["context"]["status"] == "failed"


def test_a_test_without_a_relay_is_refused_before_sending(engine, monkeypatch):
    import reporting.app.main as reporting_main

    def _unexpected(**_):
        raise AssertionError("send_test_email must not be called")

    monkeypatch.setattr(reporting_main, "send_test_email", _unexpected)
    monkeypatch.setenv("PATCHTRACK_SMTP_HOST", "")
    get_settings.cache_clear()
    with pytest.raises(HTTPException) as excinfo:
        _test_email()
    assert excinfo.value.status_code == 409
