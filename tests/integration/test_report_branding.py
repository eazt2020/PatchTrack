"""Organisation branding for generated reports.

The cover and sign-off page are only as good as the settings behind them, so
these tests exercise the real settings row: a logo survives a name-only save,
a removal is explicit, and a file that is not really a PNG or JPEG is rejected
before it can be embedded in an evidence document.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from inventory.app.main import (
    OrganizationSetting,
    get_organization,
    set_organization,
)
from patchtrack_common.security import Principal

ADMIN = Principal(
    user_id="00000000-0000-0000-0000-000000000002",
    username="admin",
    role="administrator",
    display_name="Administrator",
)

# A real 1×1 PNG: the validator checks the signature, not the dimensions.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_URI = "data:image/png;base64," + base64.b64encode(PNG).decode()


@pytest.fixture(autouse=True)
def _database(migrated_database):
    return migrated_database


class _Request:
    client = SimpleNamespace(host="127.0.0.1")
    headers: dict[str, str] = {}


def save(payload: OrganizationSetting) -> dict:
    return set_organization(payload, _Request(), ADMIN)


def test_branding_round_trips_through_the_settings_row():
    saved = save(OrganizationSetting(
        name="Example Bank",
        logo_data=PNG_URI,
        logo_filename="logo.png",
        preparer_name="Alex Morgan",
        preparer_title="Security Analyst",
        concur_name="Robin Lee",
        concur_title="Head of IT Operations",
    ))
    assert saved["name"] == "Example Bank"
    assert saved["logo_data"].startswith("data:image/png;base64,")
    assert saved["preparer_name"] == "Alex Morgan"
    assert saved["concur_name"] == "Robin Lee"

    stored = get_organization(ADMIN)
    assert stored["logo_filename"] == "logo.png"
    assert stored["preparer_title"] == "Security Analyst"
    assert stored["concur_title"] == "Head of IT Operations"


def test_a_name_only_save_keeps_the_uploaded_logo():
    save(OrganizationSetting(name="Example Bank", logo_data=PNG_URI,
                             logo_filename="logo.png"))
    saved = save(OrganizationSetting(name="Example Bank Limited"))
    assert saved["name"] == "Example Bank Limited"
    assert saved["logo_data"].startswith("data:image/png;base64,"), (
        "an omitted logo field must mean 'unchanged', not 'remove'"
    )


def test_logo_removal_is_explicit():
    save(OrganizationSetting(name="Example Bank", logo_data=PNG_URI))
    saved = save(OrganizationSetting(remove_logo=True))
    assert saved["logo_data"] == ""
    assert saved["logo_filename"] == ""
    assert saved["name"] == "Example Bank"


def test_a_non_image_data_uri_is_rejected():
    with pytest.raises(HTTPException) as excinfo:
        save(OrganizationSetting(logo_data="data:text/plain;base64,aGVsbG8="))
    assert excinfo.value.status_code == 422


def test_content_must_match_the_declared_type():
    # Valid base64, declares PNG, but the bytes are a JPEG-free text string.
    with pytest.raises(HTTPException) as excinfo:
        save(OrganizationSetting(
            logo_data="data:image/png;base64," + base64.b64encode(b"not an image").decode()
        ))
    assert excinfo.value.status_code == 422
    assert "does not match" in excinfo.value.detail


def test_an_oversized_logo_is_rejected():
    oversized = b"\x89PNG\r\n\x1a\n" + b"0" * 1_100_000
    with pytest.raises(HTTPException) as excinfo:
        save(OrganizationSetting(
            logo_data="data:image/png;base64,"
            + base64.b64encode(oversized).decode()
        ))
    assert excinfo.value.status_code == 422
    assert "larger" in excinfo.value.detail
