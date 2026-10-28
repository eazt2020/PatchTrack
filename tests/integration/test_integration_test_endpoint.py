"""The "Test configuration" route against a real database.

The route must not write inventory, assessments or a sync run; it exercises
the source and reports success or a reason. A draft in the request is used
for that test only and is never persisted.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import text

from inventory.app.main import IntegrationTest
from inventory.app.main import test_integration as run_test_integration
from patchtrack_common.db import fetch_one
from patchtrack_common.security import Principal

ADMIN = Principal(
    user_id="00000000-0000-0000-0000-000000000002",
    username="admin",
    role="administrator",
    display_name="Administrator",
)


class _Request:
    client = SimpleNamespace(host="127.0.0.1")
    headers: dict[str, str] = {}


@pytest.fixture
def satellite(engine):
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE inventory.integrations SET endpoint = :e, "
            "auth_method = 'basic', secret_ref = '' "
            "WHERE key = 'satellite'"
        ), {"e": "https://satellite.example.com"})
    return engine


def test_a_successful_probe_reports_success_and_is_audited(
    satellite, monkeypatch,
) -> None:
    seen: dict = {}

    def probe(settings, secret):
        seen.update(settings)
        return "Satellite answered with HTTP 200; 42 host(s) reported"

    monkeypatch.setattr("inventory.app.main._probe_integration", probe)

    # Other integration tests may already have collected; what matters is
    # that this test adds no run of its own.
    before = fetch_one(
        "SELECT count(*) AS n FROM inventory.sync_runs "
        "WHERE integration_key = 'satellite'"
    )["n"]

    result = run_test_integration("satellite", _Request(), ADMIN)

    assert result["status"] == "success"
    assert result["tested"] == "saved"
    assert "42 host" in result["detail"]
    assert isinstance(result["duration_ms"], int)
    assert seen["endpoint"] == "https://satellite.example.com"

    event = fetch_one(
        "SELECT outcome, context FROM shared.audit_events "
        "WHERE event_type = 'integration.tested' "
        "AND target_id = 'satellite' ORDER BY id DESC"
    )
    assert event["outcome"] == "success"
    assert event["context"]["draft"] is False

    # A probe is not a collection: no run row is written.
    after = fetch_one(
        "SELECT count(*) AS n FROM inventory.sync_runs "
        "WHERE integration_key = 'satellite'"
    )["n"]
    assert after == before


def test_a_failure_returns_the_reason_instead_of_raising(
    satellite, monkeypatch,
) -> None:
    from inventory.app.connectors.satellite import SatelliteError

    def probe(settings, secret):
        raise SatelliteError("Satellite authentication failed")

    monkeypatch.setattr("inventory.app.main._probe_integration", probe)

    result = run_test_integration("satellite", _Request(), ADMIN)

    assert result["status"] == "failed"
    assert result["detail"] == "Satellite authentication failed"

    event = fetch_one(
        "SELECT outcome, context FROM shared.audit_events "
        "WHERE event_type = 'integration.tested' "
        "AND target_id = 'satellite' ORDER BY id DESC"
    )
    assert event["outcome"] == "failure"
    assert event["context"]["detail"] == "Satellite authentication failed"


def test_a_draft_is_tested_but_not_persisted(satellite, monkeypatch) -> None:
    seen: dict = {}

    def probe(settings, secret):
        seen.update(settings)
        return "ok"

    monkeypatch.setattr("inventory.app.main._probe_integration", probe)

    result = run_test_integration(
        "satellite", _Request(), ADMIN,
        IntegrationTest(endpoint="https://draft.example.com"),
    )

    assert result["tested"] == "draft"
    assert seen["endpoint"] == "https://draft.example.com"
    stored = fetch_one(
        "SELECT endpoint FROM inventory.integrations WHERE key = 'satellite'"
    )
    assert stored["endpoint"] == "https://satellite.example.com"


def test_an_unconfigured_endpoint_fails_without_calling_the_source(
    engine, monkeypatch,
) -> None:
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE inventory.integrations SET endpoint = '' WHERE key = 'sccm'"
        ))

    def probe(settings, secret):  # pragma: no cover - must not run
        raise AssertionError("probe must not run without an endpoint")

    monkeypatch.setattr("inventory.app.main._probe_integration", probe)

    result = run_test_integration("sccm", _Request(), ADMIN)

    assert result["status"] == "failed"
    assert "Endpoint is not configured" in result["detail"]


def test_a_pasted_credential_is_refused_before_any_request(
    satellite, monkeypatch,
) -> None:
    def probe(settings, secret):  # pragma: no cover - must not run
        raise AssertionError("probe must not run with an invalid reference")

    monkeypatch.setattr("inventory.app.main._probe_integration", probe)

    result = run_test_integration(
        "satellite", _Request(), ADMIN,
        IntegrationTest(secret_ref="a-pasted-secret"),
    )

    assert result["status"] == "failed"
    assert "env:" in result["detail"] or "file:" in result["detail"]
