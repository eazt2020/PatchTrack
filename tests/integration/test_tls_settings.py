"""The gateway certificate can be replaced from Settings.

Only an administrator whose session identity still confirms may upload. The
files are validated before anything is written, the response and audit event
carry public metadata only, and no private key is ever returned.
"""

from __future__ import annotations

import asyncio
import json
import stat
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from cryptography.x509.oid import NameOID

import gateway.app.main as gateway_main
from gateway.app.main import TLSUploadBody, tls_status, upload_tls_certificate
from patchtrack_common import tls
from patchtrack_common.config import get_settings
from patchtrack_common.db import fetch_one

# Captured before the autouse fixture substitutes an authenticated admin.
_REAL_TLS_ADMIN = gateway_main._tls_admin


class _Request:
    client = SimpleNamespace(host="127.0.0.1")
    headers: dict[str, str] = {}


def pair(common_name: str = "patchtrack.example.test") -> tuple[str, str]:
    now = datetime.now(UTC)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return cert_pem, key_pem


@pytest.fixture(autouse=True)
def _database(migrated_database):
    return migrated_database


@pytest.fixture(autouse=True)
def _tls_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PATCHTRACK_TLS_ENABLED", "true")
    monkeypatch.setenv("PATCHTRACK_TLS_DIR", str(tmp_path / "tls"))
    monkeypatch.setenv("PATCHTRACK_TLS_HOSTNAME", "patchtrack.example.test")
    monkeypatch.setattr(tls, "_ACTIVE", None)
    get_settings.cache_clear()

    async def _admin(_request):
        return {"username": "admin", "role": "administrator"}

    monkeypatch.setattr(gateway_main, "_tls_admin", _admin)
    yield
    get_settings.cache_clear()


def _upload(cert_pem: str, key_pem: str):
    return asyncio.run(
        upload_tls_certificate(
            TLSUploadBody(certificate=cert_pem, private_key=key_pem), _Request()
        )
    )


def test_status_reports_enabled_without_a_certificate_first(tmp_path):
    value = asyncio.run(tls_status(_Request()))

    assert value["enabled"] is True
    assert value["serving"] is False
    assert value["certificate"] is None
    assert value["error"] is None


def test_an_administrator_can_install_a_certificate_and_key(tmp_path):
    cert_pem, key_pem = pair("patchtrack.internal")

    value = _upload(cert_pem, key_pem)

    assert value["applied"] is False  # no live listener in the test process
    assert value["enabled"] is True
    assert value["certificate"]["common_name"] == "patchtrack.internal"
    assert "private_key" not in json.dumps(value)
    assert "PRIVATE KEY" not in json.dumps(value)

    key_file = tmp_path / "tls" / tls.KEY_NAME
    assert key_file.read_text().strip() == key_pem.strip()
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600


def test_the_audit_event_records_metadata_not_the_key(tmp_path):
    cert_pem, key_pem = pair("patchtrack.audit")

    _upload(cert_pem, key_pem)

    event = fetch_one(
        "SELECT event_type, outcome, target_id, context "
        "FROM shared.audit_events WHERE target_id = 'tls_certificate' "
        "ORDER BY id DESC"
    )
    assert event["event_type"] == "settings.tls_updated"
    assert event["outcome"] == "success"
    assert event["context"]["subject"] == "CN=patchtrack.audit"
    assert len(event["context"]["sha256"]) == 64
    assert "PRIVATE KEY" not in json.dumps(event["context"])
    assert key_pem.strip() not in json.dumps(event["context"])


def test_a_mismatched_key_writes_nothing(tmp_path):
    cert_pem, _ = pair()
    _, other_key = pair()

    with pytest.raises(HTTPException) as excinfo:
        _upload(cert_pem, other_key)

    assert excinfo.value.status_code == 422
    assert "does not match" in str(excinfo.value.detail)
    assert not (tmp_path / "tls" / tls.CERT_NAME).exists()


def test_an_upload_is_refused_when_the_gateway_does_not_terminate_tls(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("PATCHTRACK_TLS_ENABLED", "false")
    get_settings.cache_clear()
    cert_pem, key_pem = pair()

    with pytest.raises(HTTPException) as excinfo:
        _upload(cert_pem, key_pem)

    assert excinfo.value.status_code == 409
    assert not (tmp_path / "tls" / tls.CERT_NAME).exists()


def test_a_second_upload_replaces_the_first(tmp_path):
    first_cert, first_key = pair("first.example.test")
    second_cert, second_key = pair("second.example.test")

    _upload(first_cert, first_key)
    value = _upload(second_cert, second_key)

    assert value["certificate"]["common_name"] == "second.example.test"
    assert "first.example.test" not in json.dumps(value)
    assert (tmp_path / "tls" / tls.KEY_NAME).read_text().strip() == second_key.strip()


def test_a_non_administrator_is_refused_before_any_upload():
    from patchtrack_common.security import create_access_token

    token, _ = create_access_token(
        user_id="00000000-0000-0000-0000-000000000003",
        username="viewer",
        role="viewer",
        token_version=0,
    )
    request = SimpleNamespace(
        cookies={gateway_main.COOKIE: token}, headers={}
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(_REAL_TLS_ADMIN(request))
    assert excinfo.value.status_code == 403
