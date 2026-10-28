"""Verification of a failing compliance state.

A justification records why a person believes a non-compliant, stale or
unknown asset is in that state. The rules here are the boundary: the reasons
are a fixed vocabulary, the statement must be substantive, and evidence is
limited in count, size and type.
"""

from __future__ import annotations

import base64

import pytest

from inventory.app.justifications import (
    MAX_EVIDENCE_BYTES,
    MAX_EVIDENCE_PER_JUSTIFICATION,
    REASONS,
    effective_state,
    normalise_evidence,
    validate_plan,
    validate_reason,
    validate_statement,
)


def encoded(content: bytes) -> str:
    return base64.b64encode(content).decode()


@pytest.mark.parametrize("code", list(REASONS))
def test_every_offered_reason_is_accepted(code):
    assert validate_reason(code) == code


@pytest.mark.parametrize("bad", ["", "other", "ACCEPTED_RISK", "made_up"])
def test_unknown_reason_is_rejected(bad):
    with pytest.raises(ValueError):
        validate_reason(bad)


def test_missing_state_is_unknown():
    assert effective_state(None) == "unknown"
    assert effective_state("stale") == "stale"


def test_statement_must_be_substantive():
    with pytest.raises(ValueError):
        validate_statement("too short")
    assert validate_statement("  Verified against change record CHG-1042.  ") == (
        "Verified against change record CHG-1042.")
    with pytest.raises(ValueError):
        validate_statement("x" * 4001)


def test_plan_length_is_bounded():
    assert validate_plan(None) == ""
    with pytest.raises(ValueError):
        validate_plan("x" * 2001)


def test_evidence_is_hashed_and_sized():
    result = normalise_evidence([{
        "filename": "change.txt", "content_type": "text/plain",
        "data": encoded(b"CHG-1042 approved"),
    }])
    assert result[0].filename == "change.txt"
    assert result[0].size == len(b"CHG-1042 approved")
    assert len(result[0].sha256) == 64


def test_data_uri_evidence_is_accepted():
    payload = "data:text/plain;base64," + encoded(b"proof")
    result = normalise_evidence([{
        "filename": "proof.txt", "content_type": "text/plain", "data": payload,
    }])
    assert result[0].content == b"proof"


def test_evidence_type_is_restricted():
    with pytest.raises(ValueError):
        normalise_evidence([{
            "filename": "payload.exe", "content_type": "application/octet-stream",
            "data": encoded(b"x"),
        }])


def test_empty_or_invalid_evidence_is_rejected():
    with pytest.raises(ValueError):
        normalise_evidence([{
            "filename": "empty.txt", "content_type": "text/plain", "data": "",
        }])
    with pytest.raises(ValueError):
        normalise_evidence([{
            "filename": "bad.txt", "content_type": "text/plain",
            "data": "not base64!!",
        }])


def test_evidence_size_and_count_are_limited():
    with pytest.raises(ValueError):
        normalise_evidence([{
            "filename": "big.txt", "content_type": "text/plain",
            "data": encoded(b"x" * (MAX_EVIDENCE_BYTES + 1)),
        }])
    too_many = [{
        "filename": f"{i}.txt", "content_type": "text/plain",
        "data": encoded(b"x"),
    } for i in range(MAX_EVIDENCE_PER_JUSTIFICATION + 1)]
    with pytest.raises(ValueError):
        normalise_evidence(too_many)


def test_filename_is_required():
    with pytest.raises(ValueError):
        normalise_evidence([{
            "filename": "  ", "content_type": "text/plain", "data": encoded(b"x"),
        }])
