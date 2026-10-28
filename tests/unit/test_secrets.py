"""Integration secrets are stored as references, never as credentials."""

from __future__ import annotations

import os

import pytest

from patchtrack_common.secrets import SecretError, is_reference, resolve, validate_reference


def test_reference_forms_are_accepted():
    assert is_reference("")
    assert is_reference("env:PATCHTRACK_SATELLITE_TOKEN")
    assert is_reference("file:/etc/patchtrack/satellite.token")
    assert validate_reference("env:X") == "env:X"


@pytest.mark.parametrize("literal", [
    "hunter2",
    "s3cr3t-Token-Value",
    "Basic dXNlcjpwYXNz",
    " env:X",              # leading space would not resolve; reject rather than guess
    "ENV:X",
])
def test_literal_credentials_are_rejected(literal):
    assert not is_reference(literal)
    with pytest.raises(SecretError):
        validate_reference(literal)


@pytest.mark.parametrize("empty_reference", ["env:", "file:"])
def test_empty_reference_is_rejected(empty_reference):
    with pytest.raises(SecretError):
        validate_reference(empty_reference)


def test_empty_reference_resolves_to_empty_string():
    assert resolve("") == ""


def test_env_reference_resolves(monkeypatch):
    monkeypatch.setenv("PATCHTRACK_TEST_TOKEN", "from-environment")
    assert resolve("env:PATCHTRACK_TEST_TOKEN") == "from-environment"


def test_missing_env_reference_resolves_empty(monkeypatch):
    monkeypatch.delenv("PATCHTRACK_TEST_MISSING", raising=False)
    assert resolve("env:PATCHTRACK_TEST_MISSING") == ""


def test_file_reference_reads_first_line(tmp_path):
    path = tmp_path / "token"
    path.write_text("the-token\nignored-second-line\n", encoding="utf-8")
    os.chmod(path, 0o600)
    assert resolve(f"file:{path}") == "the-token"


def test_unreadable_file_reference_raises(tmp_path):
    with pytest.raises(SecretError):
        resolve(f"file:{tmp_path / 'does-not-exist'}")


def test_empty_file_reference_raises(tmp_path):
    path = tmp_path / "empty"
    path.write_text("", encoding="utf-8")
    with pytest.raises(SecretError):
        resolve(f"file:{path}")


def test_unknown_scheme_raises():
    with pytest.raises(SecretError):
        resolve("vault:secret/data/satellite")
