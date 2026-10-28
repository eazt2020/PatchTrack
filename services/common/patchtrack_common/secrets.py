"""Secret references.

The database never stores an integration credential. ``inventory.integrations
.secret_ref`` holds a *reference* that is resolved at collection time:

* ``env:NAME``   — read environment variable ``NAME``
* ``file:/path`` — read the first line of a file (mode should be 0600)
* ``""``         — no credential configured

A bare value that does not look like a reference is rejected so an operator
cannot accidentally paste a password into the database through the API.
"""

from __future__ import annotations

import os
from pathlib import Path


class SecretError(RuntimeError):
    pass


def is_reference(value: str) -> bool:
    return value == "" or value.startswith(("env:", "file:"))


def validate_reference(value: str) -> str:
    if not is_reference(value):
        raise SecretError(
            "secret_ref must be a reference such as env:PATCHTRACK_SATELLITE_TOKEN "
            "or file:/etc/patchtrack/satellite.token — not the credential itself"
        )
    if value.startswith(("env:", "file:")) and not value.split(":", 1)[1]:
        raise SecretError("secret_ref reference is empty")
    return value


def resolve(value: str) -> str:
    """Resolve a stored reference to the credential. Never logged."""
    if not value:
        return ""
    scheme, _, target = value.partition(":")
    if scheme == "env":
        return os.environ.get(target, "")
    if scheme == "file":
        path = Path(target)
        try:
            return path.read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError) as exc:
            raise SecretError(f"cannot read credential file {target}") from exc
    raise SecretError("unrecognised secret reference")
