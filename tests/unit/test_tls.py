"""Certificate generation and validation for gateway TLS.

The gateway can serve HTTPS with a certificate it generates on first start, and
an administrator can upload the organisation's own later. These tests pin the
material handling: a generated pair is usable, a mismatched or expired pair is
refused before anything is written, and the private key is never world-readable.
"""

from __future__ import annotations

import socket
import ssl
import stat
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from patchtrack_common import tls
from patchtrack_common.config import Settings


@pytest.fixture(autouse=True)
def _no_live_listener(monkeypatch):
    # _ACTIVE is process-global; a context built by an earlier test must not
    # make a later upload look applied.
    monkeypatch.setattr(tls, "_ACTIVE", None)


def settings_for(directory: Path, **overrides) -> Settings:
    values = {
        "database_url": "postgresql+psycopg://unused@/unused?host=/tmp",
        "jwt_secret": "x" * 40,
        "tls_enabled": True,
        "tls_dir": directory,
        "tls_hostname": "patchtrack.example.test",
    }
    values.update(overrides)
    return Settings(**values)


def pair(*, not_before: datetime | None = None,
         not_after: datetime | None = None,
         common_name: str = "patchtrack.example.test") -> tuple[str, str]:
    now = datetime.now(UTC)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or now - timedelta(minutes=5))
        .not_valid_after(not_after or now + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return cert_pem, key_pem


def test_first_start_generates_a_usable_self_signed_certificate(tmp_path):
    settings = settings_for(tmp_path / "tls")

    info = tls.generate_self_signed(settings)

    cert_path = tmp_path / "tls" / tls.CERT_NAME
    key_path = tmp_path / "tls" / tls.KEY_NAME
    assert cert_path.exists() and key_path.exists()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(cert_path.stat().st_mode) == 0o644
    assert info.self_signed is True
    assert info.common_name == "patchtrack.example.test"
    assert "localhost" in info.san and "127.0.0.1" in info.san
    assert len(info.sha256) == 64

    # The generated pair passes the same validation as an uploaded one.
    validated = tls.validate_material(
        cert_path.read_text(), key_path.read_text()
    )
    assert validated.sha256 == info.sha256


def test_a_context_is_built_with_a_sane_tls_floor(tmp_path):
    settings = settings_for(tmp_path / "tls")

    context = tls.load_context(settings)

    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert tls.status("gateway", settings)["serving"] is True
    assert tls.status("gateway", settings)["certificate"]["self_signed"] is True


def test_existing_material_is_never_replaced_by_a_restart(tmp_path):
    settings = settings_for(tmp_path / "tls")
    cert_pem, key_pem = pair()
    tls.install_material(cert_pem, key_pem, settings)
    before = (tmp_path / "tls" / tls.CERT_NAME).read_text()

    tls.load_context(settings)
    tls.load_context(settings)

    assert (tmp_path / "tls" / tls.CERT_NAME).read_text() == before


def test_incomplete_material_is_refused_rather_than_regenerated(tmp_path):
    settings = settings_for(tmp_path / "tls")
    tls.generate_self_signed(settings)
    (tmp_path / "tls" / tls.KEY_NAME).unlink()

    with pytest.raises(tls.TLSConfigError, match="incomplete"):
        tls.load_context(settings)
    assert (tmp_path / "tls" / tls.CERT_NAME).exists()


def test_a_mismatched_key_is_refused(tmp_path):
    settings = settings_for(tmp_path / "tls")
    cert_pem, _ = pair()
    _, other_key = pair()

    with pytest.raises(tls.TLSConfigError, match="does not match"):
        tls.install_material(cert_pem, other_key, settings)
    assert not (tmp_path / "tls" / tls.CERT_NAME).exists()


def test_an_expired_certificate_is_refused(tmp_path):
    settings = settings_for(tmp_path / "tls")
    now = datetime.now(UTC)
    cert_pem, key_pem = pair(
        not_before=now - timedelta(days=400), not_after=now - timedelta(days=1)
    )

    with pytest.raises(tls.TLSConfigError, match="expired"):
        tls.install_material(cert_pem, key_pem, settings)


def test_a_passphrase_protected_key_is_refused(tmp_path):
    settings = settings_for(tmp_path / "tls")
    cert_pem, _ = pair()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    encrypted = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"passphrase"),
    ).decode()

    with pytest.raises(tls.TLSConfigError, match="passphrase"):
        tls.install_material(cert_pem, encrypted, settings)


def test_malformed_material_is_refused(tmp_path):
    settings = settings_for(tmp_path / "tls")

    with pytest.raises(tls.TLSConfigError, match="not valid PEM"):
        tls.install_material("not a certificate", "not a key", settings)


def test_an_uploaded_certificate_becomes_the_current_one(tmp_path):
    settings = settings_for(tmp_path / "tls")
    cert_pem, key_pem = pair(common_name="patchtrack.internal")

    info, applied = tls.install_material(cert_pem, key_pem, settings)

    assert applied is False  # no live listener in this test process
    assert info.common_name == "patchtrack.internal"
    assert info.self_signed is True
    current = tls.current_info(settings)
    assert current is not None and current.sha256 == info.sha256
    assert stat.S_IMODE((tmp_path / "tls" / tls.KEY_NAME).stat().st_mode) == 0o600


def test_an_installed_certificate_is_served_by_the_live_context(tmp_path):
    settings = settings_for(tmp_path / "tls")
    context = tls.load_context(settings)  # the "running gateway"

    # A listening socket already exists (as on the real server); the
    # replacement is installed before the handshake and must win.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    cert_pem, key_pem = pair(common_name="rotated.example.test")
    _, applied = tls.install_material(cert_pem, key_pem, settings)
    assert applied is True

    def serve_one() -> None:
        connection, _ = listener.accept()
        try:
            context.wrap_socket(connection, server_side=True).close()
        except OSError:
            pass
        finally:
            listener.close()

    worker = threading.Thread(target=serve_one, daemon=True)
    worker.start()
    client = ssl.create_default_context()
    client.check_hostname = False
    client.verify_mode = ssl.CERT_NONE
    with socket.create_connection(listener.getsockname()) as raw:
        with client.wrap_socket(raw, server_hostname="rotated.example.test") as tls_socket:
            der = tls_socket.getpeercert(binary_form=True)
    worker.join(timeout=5)

    served = x509.load_der_x509_certificate(der)
    assert served.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == (
        "rotated.example.test"
    )


def test_tls_is_only_active_for_the_gateway(tmp_path):
    settings = settings_for(tmp_path / "tls")

    assert tls.status("gateway", settings)["enabled"] is True
    assert tls.status("reporting", settings)["enabled"] is False


def test_disabled_tls_does_not_touch_the_filesystem(tmp_path):
    settings = settings_for(tmp_path / "does-not-exist", tls_enabled=False)

    assert tls.status("gateway", settings)["enabled"] is False
    assert not (tmp_path / "does-not-exist").exists()
