"""The deployment-wide timezone.

Schedules, snapshot periods and generated reports all name calendar
boundaries. Those boundaries must come from one place, or a report can cover a
different day than the dashboard that feeds it. The value lives in
``shared.app_settings`` so a change applies to every service without a restart.

Instants are still stored in UTC. The timezone only decides which local
calendar day, month or quarter an instant belongs to, and how timestamps are
displayed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, available_timezones

from .db import fetch_one

KEY = "system_timezone"
DEFAULT = "UTC"

# A short list for the UI first; the full IANA database is offered after it.
COMMON = (
    "UTC",
    "Asia/Kuala_Lumpur",
    "Asia/Singapore",
    "Asia/Hong_Kong",
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Asia/Kolkata",
    "Asia/Dubai",
    "Australia/Sydney",
    "Australia/Perth",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
)


def validate_timezone(value: str | None) -> str:
    """Return a canonical IANA name, or raise ValueError.

    Accepting an arbitrary string would make every period calculation fall
    back silently, so an unknown zone is a validation error at the boundary.
    """
    name = (value or "").strip()
    if not name:
        raise ValueError("A timezone is required")
    if name.upper() in {"UTC", "ETC/UTC", "GMT", "Z"}:
        return "UTC"
    if name not in available_timezones():
        raise ValueError(f"Unknown timezone: {name}")
    return name


def system_timezone() -> str:
    """The configured timezone, or UTC when unset/unreadable."""
    row = fetch_one(
        "SELECT value FROM shared.app_settings WHERE key = :key", key=KEY
    )
    if row is None:
        return DEFAULT
    value = row["value"]
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return DEFAULT
    name = value.get("name") if isinstance(value, dict) else value
    try:
        return validate_timezone(name)
    except ValueError:
        return DEFAULT


def system_zone() -> ZoneInfo:
    return ZoneInfo(system_timezone())


def local_now() -> datetime:
    return datetime.now(UTC).astimezone(system_zone())


def available() -> list[str]:
    """Common zones first, then the rest of the IANA database, sorted."""
    rest = sorted(available_timezones() - set(COMMON))
    return [*COMMON, *rest]
