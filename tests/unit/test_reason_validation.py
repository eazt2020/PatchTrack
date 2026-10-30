"""Reasons are justification text, not padding.

Both the override model and the exclusion route used to validate length before
trimming, so a value made only of spaces was accepted and stored as an empty
reason. These tests pin the trimmed rule.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from inventory.app.main import PlacementOverride


def test_override_reason_is_trimmed():
    payload = PlacementOverride(environment="Prod", reason="  wrong owner  ")
    assert payload.reason == "wrong owner"


@pytest.mark.parametrize("reason", ["   ", "  a  ", "\t\n "])
def test_override_reason_must_have_content_after_trimming(reason):
    with pytest.raises(ValidationError):
        PlacementOverride(environment="Prod", reason=reason)
