"""The migration runner must find its SQL in the source tree and in the image.

In the image the package lives at `/opt/patchtrack/common/patchtrack_common`,
one level shallower than `services/common/patchtrack_common` in the checkout,
so a hard-coded `parents[n]` resolves outside the installation in one of the
two. That is not a cosmetic difference: the runner found no files, reported
"Applied: nothing (up to date)", and the deployment came up with an empty
schema — every service then failed on its first query with
`relation "identity.users" does not exist`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from patchtrack_common.migrate import DEFAULT_DIR, apply_migrations, find_migrations

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "services" / "common" / "patchtrack_common" / "migrate.py"


def test_finds_the_migrations_in_the_source_tree() -> None:
    found = find_migrations(MODULE)
    assert found == ROOT / "deploy" / "postgres" / "migrations"
    assert found.is_dir()


def test_finds_the_migrations_in_the_image_layout(tmp_path: Path) -> None:
    # The image copies services/common to /opt/patchtrack/common, so the
    # package is one level shallower than in the checkout.
    installed = tmp_path / "opt" / "patchtrack" / "common" / "patchtrack_common"
    installed.mkdir(parents=True)
    module = installed / "migrate.py"
    module.write_text("", encoding="utf-8")
    expected = tmp_path / "opt" / "patchtrack" / "deploy" / "postgres" / "migrations"
    expected.mkdir(parents=True)
    (expected / "001_shared.sql").write_text("SELECT 1;", encoding="utf-8")

    assert find_migrations(module) == expected


def test_prefers_the_nearest_layout_when_both_exist(tmp_path: Path) -> None:
    # /opt/patchtrack/deploy/... and /opt/deploy/... both existing must not
    # matter; the closest ancestor wins and the result is stable.
    package = tmp_path / "a" / "b" / "c" / "patchtrack_common"
    package.mkdir(parents=True)
    module = package / "migrate.py"
    module.write_text("", encoding="utf-8")
    nearest = tmp_path / "a" / "deploy" / "postgres" / "migrations"
    further = tmp_path / "deploy" / "postgres" / "migrations"
    nearest.mkdir(parents=True)
    further.mkdir(parents=True)

    assert find_migrations(module) == nearest


def test_the_default_directory_is_the_shipped_one() -> None:
    assert DEFAULT_DIR == ROOT / "deploy" / "postgres" / "migrations"


def test_a_missing_directory_is_an_error_not_an_empty_success() -> None:
    # This is the failure mode that let a broken deployment look healthy:
    # an empty run still exits 0 and prints "nothing (up to date)".
    with pytest.raises(FileNotFoundError, match="does not exist"):
        apply_migrations(Path("/nonexistent/patchtrack/migrations"))


def test_the_shipped_directory_contains_numbered_sql() -> None:
    files = sorted(p.name for p in DEFAULT_DIR.glob("*.sql"))
    assert files, "no migrations ship with the application"
    assert all(name[:3].isdigit() for name in files)
