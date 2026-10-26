"""Client address handling shared by all services.

The address ends up in ``inet`` columns (``shared.audit_events.source_ip``,
``identity.login_attempts.source_ip``), so it must never be arbitrary text
from a request header. A source that does not provide an attribute must not
have one fabricated: when nothing parseable is available the answer is
``None`` — an empty column, not an invented address.

``X-Forwarded-For`` is not read here. Uvicorn already rewrites
``request.client`` from that header for peers listed in
``--forwarded-allow-ips`` (``PATCHTRACK_FORWARDED_IPS``), which is the only
place the trust decision belongs; re-reading the raw header in application
code would let any caller who can reach a service port choose the address it
is credited with.
"""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import Request


def parse_ip(value: str | None) -> str | None:
    """Return the address in canonical text form, or None if it is not one."""
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def client_ip(request: "Request") -> str | None:
    """The caller's address as established by the ASGI server, or None."""
    return parse_ip(request.client.host) if request.client else None
