"""File-backed values for bootstrap secrets.

A deployment that does not want credentials in ``.env`` (and therefore in the
process environment, ``docker inspect`` and ``/proc/<pid>/environ``) can point
any of the supported variables at a file instead::

    PATCHTRACK_JWT_SECRET_FILE=/run/secrets/jwt_secret

The file is read once at start-up. Its content, with a trailing newline
removed, becomes the setting. The ``_FILE`` variable takes precedence over the
plain variable, matching the convention used by the PostgreSQL and Grafana
images; the plain variable remains supported as a fallback.

This module only moves where the secret is stored. The file still has to be
protected (mode 0600, owned by the service account) and, for encryption at
rest, supplied by a systemd credential or a secret manager rather than being
readable from the same disk as the application. See ``docs/security.md``.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Only settings that are credentials (or contain one) are file-backed. Ordinary
# configuration stays in the environment, where it is easier to inspect.
SECRET_FILE_FIELDS: dict[str, str] = {
    # The full SQLAlchemy URL, because it carries the database password.
    "database_url": "PATCHTRACK_DATABASE_URL_FILE",
    "jwt_secret": "PATCHTRACK_JWT_SECRET_FILE",
    "secrets_key": "PATCHTRACK_SECRETS_KEY_FILE",
    "service_token": "PATCHTRACK_SERVICE_TOKEN_FILE",
    # Environment fallback for the SMTP relay; the preferred path is the
    # encrypted value saved in Settings.
    "smtp_password": "PATCHTRACK_SMTP_PASSWORD_FILE",
}

MAX_SECRET_BYTES = 64 * 1024


def _read(path: Path, variable: str) -> str:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RuntimeError(
            f"{variable} is set but its file cannot be read: {path} ({exc.strerror})"
        ) from exc
    if len(data) > MAX_SECRET_BYTES:
        raise RuntimeError(
            f"{variable} points to a file larger than {MAX_SECRET_BYTES} bytes; "
            "this does not look like a credential"
        )
    try:
        value = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"{variable} points to a file that is not UTF-8") from exc
    _warn_if_exposed(path, variable)
    # Docker and systemd secret files normally end with a newline from the
    # command that created them. Remove line endings only, so a password that
    # contains other whitespace is not silently altered.
    return value.rstrip("\r\n")


def _warn_if_exposed(path: Path, variable: str) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:  # pragma: no cover - the read above already succeeded
        return
    if mode & 0o077:
        log.warning(
            "%s is readable by group or other users (mode %04o); restrict it "
            "to the service account, e.g. chmod 0600",
            variable, mode,
        )


def secret_file_settings() -> dict[str, Any]:
    """A pydantic-settings source returning values from ``*_FILE`` paths."""
    values: dict[str, Any] = {}
    for field, variable in SECRET_FILE_FIELDS.items():
        location = os.environ.get(variable)
        # An empty value means "not configured", so a templated environment
        # file cannot make the process fail just because the option is unused.
        if location is None or not location.strip():
            continue
        values[field] = _read(Path(location.strip()), variable)
    return values
