"""SMTP relay configuration.

The relay is normally set by an administrator on the Settings screen and stored
in ``shared.app_settings``. The environment variables remain the fallback for
installations that configure SMTP before first use (and for a native
deployment that manages credentials outside the application).

The password has to be recoverable to authenticate to the relay, so it is
encrypted with Fernet before it is written. The key is derived from
``PATCHTRACK_SECRETS_KEY``, falling back to the JWT secret; it is never stored
beside the ciphertext. Rotating either key makes the saved password unreadable
and it must be entered again — the API reports that as "password not set"
rather than returning a credential that cannot work.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from patchtrack_common.config import get_settings
from patchtrack_common.db import fetch_one

log = logging.getLogger(__name__)

SMTP_KEY = "smtp_relay"


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    username: str
    password: str
    starttls: bool
    sender: str
    source: str


def _fernet() -> Fernet:
    settings = get_settings()
    material = (settings.secrets_key or settings.jwt_secret).encode()
    digest = hashlib.sha256(b"patchtrack-smtp:" + material).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_password(password: str) -> str:
    return _fernet().encrypt(password.encode()).decode()


def decrypt_password(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        log.warning(
            "stored SMTP password cannot be decrypted; re-enter it on the "
            "Settings screen"
        )
        return None


def load_stored() -> dict[str, Any] | None:
    """The saved relay settings, or None when the environment is in use."""
    row = fetch_one(
        "SELECT value FROM shared.app_settings WHERE key = :key", key=SMTP_KEY
    )
    if row is None:
        return None
    value = row["value"]
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def _port(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return 587
    return port if 1 <= port <= 65535 else 587


def resolve_smtp() -> SmtpConfig:
    """Effective relay settings: the saved row, otherwise the environment."""
    stored = load_stored()
    if stored is not None:
        token = str(stored.get("password") or "")
        return SmtpConfig(
            host=str(stored.get("host") or "").strip(),
            port=_port(stored.get("port")),
            username=str(stored.get("username") or ""),
            password=(decrypt_password(token) or "") if token else "",
            starttls=bool(stored.get("starttls", True)),
            sender=str(stored.get("sender") or "").strip(),
            source="database",
        )
    settings = get_settings()
    return SmtpConfig(
        host=settings.smtp_host.strip(),
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password,
        starttls=settings.smtp_starttls,
        sender=settings.smtp_sender,
        source="environment",
    )
