"""TLS termination and certificate management for the gateway.

The gateway can serve HTTPS itself, so a first installation is encrypted
without a separate proxy. On first start it generates a self-signed certificate
for the host; an administrator can later upload the organisation's own
certificate and key from **Settings → HTTPS certificate**, which replaces the
files and reloads the live listener without dropping the process (new
connections use the new certificate; existing keep-alive connections finish on
the old one).

The private key never enters the database and is never returned by the API. It
is written to ``PATCHTRACK_TLS_DIR`` with mode 0600, beside the certificate.
Certificates that chain to an internal CA work; only the leaf needs to match
the key, and the full chain may be uploaded.

TLS is applied only when the gateway process was started through
``patchtrack_common.serve`` (the entrypoint and systemd unit do this) and
``PATCHTRACK_TLS_ENABLED`` is true. A process started another way still exposes
the management API, reporting ``serving: false`` so the UI can say a restart is
required.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import ssl
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .config import Settings, get_settings

log = logging.getLogger(__name__)

CERT_NAME = "server.crt"
KEY_NAME = "server.key"
SELF_SIGNED_DAYS = 825

# Set by serve.main() when the process terminates its own TLS. Mutating the
# SSLContext ("load_cert_chain") affects every subsequent handshake, because
# asyncio wraps each accepted connection with the context at handshake time.
_ACTIVE: tuple[ssl.SSLContext, Path, Path] | None = None


class TLSConfigError(RuntimeError):
    """The supplied or stored certificate material cannot be used."""


@dataclass(frozen=True)
class CertInfo:
    subject: str
    common_name: str
    issuer: str
    self_signed: bool
    not_before: str
    not_after: str
    san: tuple[str, ...]
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "common_name": self.common_name,
            "issuer": self.issuer,
            "self_signed": self.self_signed,
            "not_before": self.not_before,
            "not_after": self.not_after,
            "san": list(self.san),
            "sha256": self.sha256,
        }


def cert_path(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    name = Path(settings.tls_cert_file or CERT_NAME)
    return name if name.is_absolute() else Path(settings.tls_dir) / name


def key_path(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    name = Path(settings.tls_key_file or KEY_NAME)
    return name if name.is_absolute() else Path(settings.tls_dir) / name


def _ensure_directory(settings: Settings) -> Path:
    directory = Path(settings.tls_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    return directory


def _write_atomic(path: Path, data: bytes, mode: int) -> None:
    """Write and fsync a file before replacing the target.

    A power loss or crash midway leaves the previous certificate in place
    rather than a truncated key, which would make the gateway unable to start.
    """
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _certificate_info(certificate: x509.Certificate) -> CertInfo:
    try:
        san = tuple(
            str(name.value)
            for name in certificate.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value
        )
    except x509.ExtensionNotFound:
        san = ()
    common_name = ""
    for attribute in certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME):
        common_name = str(attribute.value)
        break
    return CertInfo(
        subject=certificate.subject.rfc4514_string(),
        common_name=common_name,
        issuer=certificate.issuer.rfc4514_string(),
        self_signed=certificate.issuer == certificate.subject,
        not_before=certificate.not_valid_before_utc.isoformat(),
        not_after=certificate.not_valid_after_utc.isoformat(),
        san=san,
        sha256=certificate.fingerprint(hashes.SHA256()).hex(),
    )


def validate_material(
    certificate_pem: str | bytes, private_key_pem: str | bytes,
) -> CertInfo:
    """Check an uploaded pair before anything is written.

    Returns the leaf certificate's metadata. Raises ``TLSConfigError`` with a
    message safe to show an administrator: a mismatch, an expired
    certificate, a passphrase-protected key and malformed PEM are all
    distinguished.
    """
    cert_bytes = certificate_pem.encode() if isinstance(certificate_pem, str) else certificate_pem
    key_bytes = private_key_pem.encode() if isinstance(private_key_pem, str) else private_key_pem
    try:
        chain = x509.load_pem_x509_certificates(cert_bytes)
    except ValueError as exc:
        raise TLSConfigError(
            "The certificate is not valid PEM. Paste the server certificate, "
            "including any intermediate chain, in PEM form."
        ) from exc
    if not chain:
        raise TLSConfigError("No certificate found in the supplied PEM")
    leaf = chain[0]
    try:
        key = serialization.load_pem_private_key(key_bytes, password=None)
    except TypeError as exc:
        raise TLSConfigError(
            "The private key is passphrase-protected. Upload an unencrypted "
            "key; PatchTrack keeps it readable only by the service account."
        ) from exc
    except ValueError as exc:
        raise TLSConfigError("The private key is not valid PEM") from exc
    cert_public = leaf.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    key_public = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if cert_public != key_public:
        raise TLSConfigError("The private key does not match the certificate")
    now = datetime.now(UTC)
    if leaf.not_valid_after_utc <= now:
        raise TLSConfigError(
            f"The certificate expired on {leaf.not_valid_after_utc:%Y-%m-%d}"
        )
    if leaf.not_valid_before_utc > now + timedelta(minutes=10):
        raise TLSConfigError(
            f"The certificate is not valid until "
            f"{leaf.not_valid_before_utc:%Y-%m-%d %H:%M} UTC"
        )
    return _certificate_info(leaf)


def current_info(settings: Settings | None = None) -> CertInfo | None:
    settings = settings or get_settings()
    path = cert_path(settings)
    if not path.exists():
        return None
    try:
        chain = x509.load_pem_x509_certificates(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise TLSConfigError(f"The stored certificate cannot be read: {exc}") from exc
    return _certificate_info(chain[0])


def generate_self_signed(settings: Settings | None = None) -> CertInfo:
    """Create a host certificate when no material exists yet.

    Browsers warn because no CA signed it; it exists so the first installation
    serves HTTPS and the administrator can enrol the organisation's
    certificate from Settings without first editing files on the host.
    """
    settings = settings or get_settings()
    hostname = (settings.tls_hostname or socket.getfqdn() or "localhost").strip()
    names: list[x509.GeneralName] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        if not value or value in seen:
            return
        seen.add(value)
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(value)))
        except ValueError:
            names.append(x509.DNSName(value))

    for value in (hostname, "localhost", "127.0.0.1", "::1"):
        add(value)

    now = datetime.now(UTC)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, hostname[:64] or "patchtrack")]
    )
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=SELF_SIGNED_DAYS))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        # A server certificate only. The key stays on the gateway, but there is
        # no reason for it to be able to sign other certificates.
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
    )
    certificate = builder.sign(key, hashes.SHA256())
    _ensure_directory(settings)
    _write_atomic(
        key_path(settings),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        0o600,
    )
    _write_atomic(
        cert_path(settings), certificate.public_bytes(serialization.Encoding.PEM),
        0o644,
    )
    info = _certificate_info(certificate)
    log.warning(
        "generated a self-signed TLS certificate for %s (sha256 %s); upload the "
        "organisation's certificate in Settings once available",
        hostname, info.sha256,
    )
    return info


def _attach(context: ssl.SSLContext, settings: Settings) -> None:
    global _ACTIVE
    _ACTIVE = (context, cert_path(settings), key_path(settings))


def load_context(settings: Settings | None = None) -> ssl.SSLContext:
    """Build the listener context, generating self-signed material if absent."""
    settings = settings or get_settings()
    cert, key = cert_path(settings), key_path(settings)
    if not cert.exists() and not key.exists():
        generate_self_signed(settings)
    elif not cert.exists() or not key.exists():
        raise TLSConfigError(
            f"TLS material is incomplete: {cert} and {key} must both exist"
        )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.options |= ssl.OP_NO_COMPRESSION
    try:
        context.load_cert_chain(str(cert), str(key))
    except (OSError, ssl.SSLError) as exc:
        raise TLSConfigError(f"TLS material cannot be loaded: {exc}") from exc
    _attach(context, settings)
    return context


def reload_active() -> bool:
    """Load the current files into the live listener. False when not serving."""
    if _ACTIVE is None:
        return False
    context, cert, key = _ACTIVE
    context.load_cert_chain(str(cert), str(key))
    return True


def install_material(
    certificate_pem: str, private_key_pem: str,
    settings: Settings | None = None,
) -> tuple[CertInfo, bool]:
    """Validate, persist and apply an uploaded certificate pair."""
    settings = settings or get_settings()
    info = validate_material(certificate_pem, private_key_pem)
    _ensure_directory(settings)
    cert_bytes = certificate_pem.encode() if isinstance(certificate_pem, str) else certificate_pem
    key_bytes = private_key_pem.encode() if isinstance(private_key_pem, str) else private_key_pem
    _write_atomic(cert_path(settings), cert_bytes.strip() + b"\n", 0o644)
    _write_atomic(key_path(settings), key_bytes.strip() + b"\n", 0o600)
    return info, reload_active()


def status(service: str, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    enabled = bool(settings.tls_enabled and service == "gateway")
    certificate = None
    error = None
    if enabled:
        try:
            certificate = current_info(settings)
        except TLSConfigError as exc:
            error = str(exc)
    return {
        "enabled": enabled,
        "serving": _ACTIVE is not None,
        "certificate": certificate.as_dict() if certificate else None,
        "error": error,
    }
