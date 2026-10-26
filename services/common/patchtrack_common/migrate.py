"""Deterministic SQL migration runner.

Migrations are plain SQL files in ``deploy/postgres/migrations`` named
``NNN_description.sql``. Each file runs inside a transaction and its SHA-256
is recorded in ``shared.schema_migrations``; modifying an applied file is an
error rather than a silent re-run.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from sqlalchemy import text

from .db import get_engine


def find_migrations(start: Path) -> Path:
    """Locate ``deploy/postgres/migrations`` from a module location.

    The package sits at a different depth in the image
    (``/opt/patchtrack/common/patchtrack_common``) than in the source tree
    (``services/common/patchtrack_common``), so a fixed ``parents[n]`` points
    outside the installation in one of them. Walking up until the directory is
    found works in both, and a miss is still reported by the caller rather than
    silently applying nothing — which is what happened in the image: the runner
    found no files, reported "nothing (up to date)", and every service then
    failed with "relation does not exist".
    """
    parents = start.resolve().parents
    for parent in parents:
        candidate = parent / "deploy" / "postgres" / "migrations"
        if candidate.is_dir():
            return candidate
    # Nothing found: return the traditional location so the caller's
    # "directory does not exist" error names a plausible path.
    fallback = parents[min(3, len(parents) - 1)] if parents else start.resolve()
    return fallback / "deploy" / "postgres" / "migrations"


DEFAULT_DIR = find_migrations(Path(__file__))

BOOTSTRAP = """
CREATE SCHEMA IF NOT EXISTS shared;
CREATE TABLE IF NOT EXISTS shared.schema_migrations (
    version      text PRIMARY KEY,
    description  text NOT NULL,
    applied_at   timestamptz NOT NULL DEFAULT now(),
    checksum     text NOT NULL
);
"""


def migration_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("*.sql") if p.is_file())


def apply_migrations(directory: Path = DEFAULT_DIR) -> list[str]:
    if not Path(directory).is_dir():
        # An absent directory is not "everything already applied". Reporting it
        # as up to date lets a deployment finish successfully with an empty
        # schema, and every service then fails on its first query.
        raise FileNotFoundError(
            f"Migration directory {directory} does not exist. Refusing to "
            "report an empty schema as up to date."
        )
    applied: list[str] = []
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(BOOTSTRAP))
        known = {
            row.version: row.checksum
            for row in conn.execute(
                text("SELECT version, checksum FROM shared.schema_migrations")
            )
        }
        for path in migration_files(directory):
            version = path.stem
            sql = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            if version in known:
                if known[version] != checksum:
                    raise RuntimeError(
                        f"Migration {version} was modified after being applied. "
                        "Create a new migration instead."
                    )
                continue
            conn.execute(text(sql))
            conn.execute(
                text(
                    "INSERT INTO shared.schema_migrations (version, description, checksum) "
                    "VALUES (:v, :d, :c)"
                ),
                {"v": version, "d": path.stem, "c": checksum},
            )
            applied.append(version)
    return applied


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DIR
    done = apply_migrations(target)
    print("Applied:", ", ".join(done) if done else "nothing (up to date)")
