"""Bootstrap secrets can be read from files instead of the environment.

``.env`` is convenient but leaves credentials in plain text and in every
process's environment. A ``*_FILE`` variable names a mounted secret instead.
These tests pin the precedence (file over environment, explicit constructor
arguments above both), the file format (trailing newline removed, other
whitespace kept) and the failure mode (a missing file stops start-up with an
actionable message rather than falling back to a weaker value).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from patchtrack_common.config import Settings
from patchtrack_common.secret_files import SECRET_FILE_FIELDS

DATABASE_URL = "postgresql+psycopg://unused@/unused?host=/tmp"
JWT = "j" * 40


@pytest.fixture(autouse=True)
def _clear_secret_files(monkeypatch):
    for variable in SECRET_FILE_FIELDS.values():
        monkeypatch.delenv(variable, raising=False)


def test_jwt_secret_is_read_from_a_file(monkeypatch, tmp_path):
    path = tmp_path / "jwt_secret"
    path.write_text("from-a-file-" + "j" * 30 + "\n")
    monkeypatch.setenv("PATCHTRACK_JWT_SECRET", "from-the-environment-" + "x" * 20)
    monkeypatch.setenv("PATCHTRACK_JWT_SECRET_FILE", str(path))

    settings = Settings(database_url=DATABASE_URL)

    assert settings.jwt_secret == "from-a-file-" + "j" * 30


def test_a_file_supplies_a_required_setting(monkeypatch, tmp_path):
    path = tmp_path / "database_url"
    path.write_text(DATABASE_URL + "\n")
    monkeypatch.setenv("PATCHTRACK_DATABASE_URL_FILE", str(path))

    settings = Settings(jwt_secret=JWT)

    assert settings.database_url == DATABASE_URL


def test_every_supported_field_can_be_file_backed(monkeypatch, tmp_path):
    expectations = {
        "jwt_secret": "jwt-from-file-" + "j" * 20,
        "secrets_key": "secrets-from-file",
        "service_token": "service-token-from-file",
        "smtp_password": "smtp-password-from-file",
    }
    for field in expectations:
        path = tmp_path / field
        path.write_text(expectations[field] + "\n")
        monkeypatch.setenv(SECRET_FILE_FIELDS[field], str(path))

    settings = Settings(database_url=DATABASE_URL)

    for field, expected in expectations.items():
        assert getattr(settings, field) == expected


def test_an_explicit_constructor_value_still_wins(monkeypatch, tmp_path):
    path = tmp_path / "jwt_secret"
    path.write_text("file-value-" + "f" * 30)
    monkeypatch.setenv("PATCHTRACK_JWT_SECRET_FILE", str(path))

    settings = Settings(database_url=DATABASE_URL, jwt_secret=JWT)

    assert settings.jwt_secret == JWT


def test_the_plain_variable_remains_the_fallback(monkeypatch):
    monkeypatch.setenv("PATCHTRACK_SERVICE_TOKEN", "environment-token")

    settings = Settings(database_url=DATABASE_URL, jwt_secret=JWT)

    assert settings.service_token == "environment-token"


def test_only_trailing_line_endings_are_removed(monkeypatch, tmp_path):
    path = tmp_path / "service_token"
    path.write_text("  padded token  \r\n")
    monkeypatch.setenv("PATCHTRACK_SERVICE_TOKEN_FILE", str(path))

    settings = Settings(database_url=DATABASE_URL, jwt_secret=JWT)

    assert settings.service_token == "  padded token  "


def test_an_empty_file_is_an_explicit_empty_value(monkeypatch, tmp_path):
    path = tmp_path / "service_token"
    path.write_text("\n")
    monkeypatch.setenv("PATCHTRACK_SERVICE_TOKEN_FILE", str(path))

    settings = Settings(database_url=DATABASE_URL, jwt_secret=JWT)

    assert settings.service_token == ""


def test_an_empty_file_variable_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("PATCHTRACK_SERVICE_TOKEN_FILE", "")
    monkeypatch.setenv("PATCHTRACK_SERVICE_TOKEN", "environment-token")

    settings = Settings(database_url=DATABASE_URL, jwt_secret=JWT)

    assert settings.service_token == "environment-token"


def test_a_missing_file_stops_startup_without_falling_back(monkeypatch, tmp_path):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("PATCHTRACK_JWT_SECRET_FILE", str(missing))
    monkeypatch.setenv("PATCHTRACK_JWT_SECRET", JWT)

    with pytest.raises(RuntimeError, match="PATCHTRACK_JWT_SECRET_FILE.*cannot be read"):
        Settings(database_url=DATABASE_URL)


def test_a_directory_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("PATCHTRACK_JWT_SECRET_FILE", str(tmp_path))

    with pytest.raises(RuntimeError, match="cannot be read"):
        Settings(database_url=DATABASE_URL, jwt_secret=JWT)


def test_an_oversized_file_is_refused(monkeypatch, tmp_path):
    path = tmp_path / "jwt_secret"
    path.write_bytes(b"x" * (64 * 1024 + 1))
    monkeypatch.setenv("PATCHTRACK_JWT_SECRET_FILE", str(path))

    with pytest.raises(RuntimeError, match="larger than"):
        Settings(database_url=DATABASE_URL, jwt_secret=JWT)


def test_an_exposed_secret_file_is_reported(monkeypatch, tmp_path, caplog):
    path = tmp_path / "jwt_secret"
    path.write_text("j" * 40)
    path.chmod(0o644)
    monkeypatch.setenv("PATCHTRACK_JWT_SECRET_FILE", str(path))

    with caplog.at_level(logging.WARNING, logger="patchtrack_common.secret_files"):
        settings = Settings(database_url=DATABASE_URL)

    assert settings.jwt_secret == "j" * 40
    assert "readable by group or other users" in caplog.text
    assert "j" * 40 not in caplog.text


def test_the_compose_override_mounts_every_application_secret():
    root = Path(__file__).resolve().parents[2]
    override = yaml.safe_load((root / "docker-compose.secrets.yml").read_text())

    assert set(override["secrets"]) == {
        "database_url", "jwt_secret", "service_token", "postgres_password",
    }
    for service in (
        "migrate", "identity", "inventory", "compliance", "reporting", "gateway",
    ):
        environment = override["services"][service]["environment"]
        assert environment["PATCHTRACK_DATABASE_URL_FILE"] == "/run/secrets/database_url"
        assert environment["PATCHTRACK_JWT_SECRET_FILE"] == "/run/secrets/jwt_secret"
        assert environment["PATCHTRACK_SERVICE_TOKEN_FILE"] == "/run/secrets/service_token"
        # The plain values must not be inherited into the container environment.
        assert environment["PATCHTRACK_DATABASE_URL"] == ""
        assert environment["PATCHTRACK_JWT_SECRET"] == ""
        assert environment["PATCHTRACK_SERVICE_TOKEN"] == ""
        assert set(override["services"][service]["secrets"]) == {
            "database_url", "jwt_secret", "service_token",
        }

    postgres = override["services"]["postgres"]
    assert postgres["environment"]["POSTGRES_PASSWORD_FILE"] == (
        "/run/secrets/postgres_password"
    )
    assert postgres["environment"]["POSTGRES_PASSWORD"] == ""
    assert postgres["secrets"] == ["postgres_password"]
