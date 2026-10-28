"""Fixtures for tests that need a real PostgreSQL database.

These tests are skipped when no database is reachable, so the unit suite still
runs in a build environment without PostgreSQL. Point them at a throwaway
database:

    PATCHTRACK_TEST_DATABASE_URL=postgresql+psycopg://patchtrack@/patchtrack_test?host=/tmp \
        pytest tests/integration

The database is created, migrated and torn down by these fixtures; never point
them at a database whose data you care about.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

TEST_URL = os.environ.get(
    "PATCHTRACK_TEST_DATABASE_URL",
    "postgresql+psycopg://patchtrack@/patchtrack_test?host=/tmp",
)


def _server_url(url: str, database: str = "postgres") -> str:
    """Same URL with the database name replaced, for CREATE DATABASE.

    The query string carries the socket directory, so it is preserved; a naive
    rpartition("/") would drop it and connect to a default TCP port instead.
    """
    base, separator, query = url.partition("?")
    head, _, _ = base.rpartition("/")
    return f"{head}/{database}" + (f"{separator}{query}" if separator else "")


def _database_name(url: str) -> str:
    base = url.partition("?")[0]
    return base.rpartition("/")[2]


@pytest.fixture(scope="session")
def migrated_database() -> str:
    """A freshly created, fully migrated database. Skips if unavailable."""
    try:
        admin = create_engine(_server_url(TEST_URL), isolation_level="AUTOCOMMIT")
        name = _database_name(TEST_URL)
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # noqa: BLE001 - any connection problem means skip
        pytest.skip(f"PostgreSQL is not available for integration tests: {exc}")

    os.environ["PATCHTRACK_DATABASE_URL"] = TEST_URL

    # get_settings and get_engine are cached; clear them so the new URL is used.
    from patchtrack_common import db as db_module
    from patchtrack_common.config import get_settings

    get_settings.cache_clear()
    db_module._engine = None

    from patchtrack_common.migrate import apply_migrations

    apply_migrations()
    yield TEST_URL

    db_module.get_engine().dispose()
    db_module._engine = None
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def engine(migrated_database: str) -> Engine:
    from patchtrack_common.db import get_engine

    return get_engine()


@pytest.fixture
def clean_tables(engine: Engine):
    """Empty the mutable tables before each test, keeping migrations intact."""
    tables = [
        "compliance.findings",
        "compliance.asset_compliance",
        "compliance.asset_latest_assessment",
        "compliance.assessments",
        "compliance.snapshot_groups",
        "compliance.snapshots",
        "inventory.asset_source_links",
        "inventory.asset_aliases",
        "inventory.asset_overrides",
        "inventory.reconciliation_conflicts",
        "inventory.assets",
        "inventory.sync_runs",
        "inventory.placement_aliases",
        "inventory.placement_options",
    ]
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(tables) + " CASCADE"))
    yield
