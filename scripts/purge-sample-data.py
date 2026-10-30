#!/usr/bin/env python3
"""Remove the SYNTHETIC sample data loaded by ``scripts/seed-demo.py``.

The installer can seed a demonstration estate so the console, drill-downs,
snapshots and PDF reports can be exercised before the integrations are pointed
at real systems. Every row it writes is marked:

* assets carry ``raw.demo_data = true``;
* snapshots are written by ``seed-demo`` or carry ``methodology.demo_data``;
* sync runs carry ``details.demo_data``.

This script deletes only those marked rows. The application, the configuration,
the container images and every non-sample row are left untouched, so a host
that was evaluated with sample data can be handed over for real use without
reinstalling.

Usage::

    PYTHONPATH=services/common:services python3 scripts/purge-sample-data.py \
        [--dry-run] [--report-dir /var/lib/patchtrack/reports]

Reports generated from a sample snapshot are removed with their PDF files; a
file outside the configured report directory is never unlinked.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from sqlalchemy import text

from patchtrack_common.db import get_engine

DEMO_ASSET = "raw->>'demo_data' = 'true'"
DEMO_SNAPSHOT = (
    "(methodology->>'demo_data' = 'true' "
    "OR methodology->>'synthetic_history' = 'true' "
    "OR generated_by = 'seed-demo')"
)
DEMO_SYNC_RUN = "details->>'demo_data' = 'true'"


def _scalar(conn, sql: str) -> int:
    value = conn.execute(text(sql)).scalar()
    return int(value or 0)


def _rows(conn, sql: str) -> list[dict]:
    return [dict(row) for row in conn.execute(text(sql)).mappings().all()]


def _remove_report_files(report_dir: Path, paths: list[str]) -> int:
    root = report_dir.resolve()
    removed = 0
    for value in paths:
        if not value:
            continue
        candidate = Path(value)
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved.is_file() and resolved.parent == root:
            try:
                resolved.unlink()
                removed += 1
            except OSError as exc:
                print(f"  could not remove {resolved}: {exc}")
    return removed


def counts(conn) -> dict[str, int]:
    return {
        "assets": _scalar(conn, f"SELECT count(*) FROM inventory.assets WHERE {DEMO_ASSET}"),
        "snapshots": _scalar(
            conn, f"SELECT count(*) FROM compliance.snapshots WHERE {DEMO_SNAPSHOT}"
        ),
        "reports": _scalar(
            conn,
            "SELECT count(*) FROM reporting.reports WHERE snapshot_id IN "
            f"(SELECT id FROM compliance.snapshots WHERE {DEMO_SNAPSHOT})",
        ),
        "sync_runs": _scalar(
            conn, f"SELECT count(*) FROM inventory.sync_runs WHERE {DEMO_SYNC_RUN}"
        ),
    }


def purge(conn, report_dir: Path, *, dry_run: bool) -> dict[str, int]:
    before = counts(conn)
    reports = _rows(
        conn,
        "SELECT id, file_path, schedule_id, period_start FROM reporting.reports "
        "WHERE snapshot_id IN "
        f"(SELECT id FROM compliance.snapshots WHERE {DEMO_SNAPSHOT})",
    )

    # The deletes run even in a dry run and are rolled back by the caller, so
    # a dry run also proves the foreign-key order rather than only counting.
    # Reports reference snapshots without ON DELETE CASCADE, so they go
    # first. Delivery attempts cascade with the report row.
    conn.execute(
        text(
            "DELETE FROM reporting.reports WHERE snapshot_id IN "
            f"(SELECT id FROM compliance.snapshots WHERE {DEMO_SNAPSHOT})"
        )
    )
    conn.execute(
        text(f"DELETE FROM compliance.snapshots WHERE {DEMO_SNAPSHOT}")
    )
    # A scheduled sample report leaves an idempotency claim for its period.
    # Clear it so the same period can be reported again once real data is
    # collected; the claim has no foreign key to the report row.
    for row in reports:
        if row["schedule_id"] is None:
            continue
        conn.execute(
            text(
                "DELETE FROM reporting.job_executions "
                "WHERE schedule_id = :schedule AND period_start = :period"
            ),
            {"schedule": row["schedule_id"], "period": row["period_start"]},
        )
    # Conflict rows attached to a demo asset have ON DELETE SET NULL, so
    # remove them before the asset so no orphan is left behind. Conflicts
    # raised by the seed's unmatched host have no asset at all; they are
    # recognised by the marker the seed writes, and — for estates seeded
    # before that marker existed — by falling inside the demo sync runs.
    conn.execute(
        text(
            "DELETE FROM inventory.reconciliation_conflicts "
            f"WHERE asset_id IN (SELECT id FROM inventory.assets WHERE {DEMO_ASSET}) "
            "OR payload->>'demo_data' = 'true' "
            "OR (asset_id IS NULL AND detected_at >= ("
            f"    SELECT min(started_at) FROM inventory.sync_runs WHERE {DEMO_SYNC_RUN}) "
            "AND detected_at <= ("
            "    SELECT max(coalesce(finished_at, started_at)) "
            f"    FROM inventory.sync_runs WHERE {DEMO_SYNC_RUN}))"
        )
    )
    conn.execute(
        text(f"DELETE FROM inventory.assets WHERE {DEMO_ASSET}")
    )
    # Assessments and last-assessment pointers have already cascaded with
    # the assets, so the demo sync runs are no longer referenced.
    conn.execute(
        text(f"DELETE FROM inventory.sync_runs WHERE {DEMO_SYNC_RUN}")
    )

    files = 0
    if not dry_run:
        files = _remove_report_files(report_dir, [row["file_path"] for row in reports])

    after = counts(conn)
    before["report_files"] = files
    before["remaining"] = after
    return before


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report what would be removed without changing anything",
    )
    parser.add_argument(
        "--report-dir", default=os.environ.get(
            "PATCHTRACK_REPORT_DIR", "/var/lib/patchtrack/reports"
        ),
        help="directory holding generated PDFs (default: /var/lib/patchtrack/reports)",
    )
    args = parser.parse_args()

    report_dir = Path(args.report_dir)
    # Connect directly rather than through connection(): a dry run must never
    # commit, and the context manager there commits on exit.
    with get_engine().connect() as conn:
        result = purge(conn, report_dir, dry_run=args.dry_run)
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()

    demo = result
    remaining = result["remaining"]
    verb = "would remove" if args.dry_run else "removed"
    print(f"Sample data purge ({'dry run' if args.dry_run else 'applied'})")
    print(f"    {verb}: {demo['assets']} assets, {demo['snapshots']} snapshots, "
          f"{demo['reports']} reports, {demo['sync_runs']} sync runs")
    if not args.dry_run:
        print(f"    report files removed: {demo['report_files']}")
    print(f"    remaining sample rows: {remaining['assets']} assets, "
          f"{remaining['snapshots']} snapshots, {remaining['reports']} reports")
    if not args.dry_run and not any(remaining.values()):
        print("    no synthetic sample data remains")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
