"""Sorting the server list must never splice request text into SQL.

The console sends a sort key; the service resolves it through a fixed table of
column expressions. An unknown key is a 422, not a silently ignored sort — an
operator who asked for a re-order should either get it or be told it is not
available.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

COMPLIANCE_DIR = Path(__file__).resolve().parents[2] / "services" / "compliance"


@pytest.fixture(scope="module")
def compliance():
    for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
        del sys.modules[name]
    sys.path.insert(0, str(COMPLIANCE_DIR))
    try:
        return importlib.import_module("app.main")
    finally:
        sys.path.remove(str(COMPLIANCE_DIR))


def test_known_keys_resolve_to_column_expressions(compliance):
    for key, column in compliance.SERVER_SORT_COLUMNS.items():
        clause = compliance.server_order_clause(key, "asc")
        assert clause.startswith(f"{column} ASC")


def test_fields_the_console_offers_are_all_sortable(compliance):
    # These are the options in the server list's "Sort by" control.
    offered = {
        "total_patches", "critical_count", "hostname", "last_assessed_at",
        "environment", "business_group", "os_name", "state",
    }
    assert offered <= set(compliance.SERVER_SORT_COLUMNS)


def test_unknown_sort_is_refused(compliance):
    with pytest.raises(HTTPException) as caught:
        compliance.server_order_clause("hostname; DROP TABLE assets", "asc")
    assert caught.value.status_code == 422


def test_descending_is_requested_explicitly(compliance):
    assert "DESC" in compliance.server_order_clause("total_patches", "desc")
    assert "ASC" in compliance.server_order_clause("total_patches", "asc")


def test_rows_without_a_value_sort_last_in_both_directions(compliance):
    # "never assessed" must not masquerade as "assessed earliest".
    for direction in ("asc", "desc"):
        assert "NULLS LAST" in compliance.server_order_clause("last_assessed_at", direction)


def test_the_clause_is_tied_to_a_hostname_for_stable_paging(compliance):
    # Without a deterministic tie-break, two pages can repeat or skip a server.
    clause = compliance.server_order_clause("environment", "asc")
    assert clause.endswith("a.hostname, a.id")
