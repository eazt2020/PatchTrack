#!/usr/bin/env python3
"""Populate a development database with SYNTHETIC demo data.

Nothing here represents a real estate. The records are invented so that the
dashboard, drill-downs, snapshots and PDF reports can be exercised before the
AssetExplorer / Satellite / SCCM / Solaris integrations are pointed at real
systems. Every created asset carries ``raw.demo_data = true``.

Usage::

    PYTHONPATH=services/common:services python3 scripts/seed-demo.py [--reset]

The script writes through the same functions the live collectors use
(``upsert_asset``, ``ingest_assessments``, ``create_snapshot``), so it
validates the real ingestion path rather than inserting rows directly.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from patchtrack_common.db import connection
from inventory.app.connectors.base import AssetRecord, NormalisedFinding, PatchAssessment
from inventory.app.sync import _finish_run, _start_run, ingest_assessments, upsert_asset
from compliance.app.evaluator import create_snapshot, refresh_compliance

random.seed(20261026)

OS_CATALOGUE = [
    ("rhel", "Red Hat Enterprise Linux", ["9.4", "9.5", "9.6", "8.10"], "satellite"),
    ("windows", "Windows Server", ["2022", "2019"], "sccm"),
    ("solaris", "Oracle Solaris", ["11.4"], "solaris"),
]
ENVIRONMENTS = ["Production", "Pre-production", "Staging", "Development", "DMZ"]
BUSINESS = ["Payments", "Core Banking", "Customer Portal", "Data Platform",
            "Identity", "Logistics", "Analytics", "Messaging"]
APPS = ["ledger", "clearing", "statement-engine", "risk-engine", "api-gateway",
        "notification-hub", "batch-scheduler", "warehouse-etl"]
LOCATIONS = ["DC-North", "DC-South", "DC-West", "DR-East"]
TITLES = {
    "critical": ["Remote code execution in TLS stack", "Privilege escalation in kernel",
                 "Authentication bypass in web service"],
    "important": ["Denial of service in network stack", "Information disclosure in library",
                  "Security update for SMB client"],
    "moderate": ["Bug fix for volume manager", "Reliability update for storage driver"],
    "low": ["Timezone database update", "Documentation correction package"],
}


def _build_assets(count: int) -> list[AssetRecord]:
    records: list[AssetRecord] = []
    used_hosts: set[str] = set()
    weights = ["rhel"] * 6 + ["windows"] * 3 + ["solaris"]
    for index in range(count):
        family = random.choice(weights)
        meta = next(item for item in OS_CATALOGUE if item[0] == family)
        _, os_name, versions, _ = meta
        prefix = {"rhel": "lnx", "windows": "win", "solaris": "sol"}[family]
        business = random.choice(BUSINESS)
        slug = business.lower().replace(" ", "-")
        number = index + 1
        hostname = f"{prefix}-{slug[:10]}-{number:03d}"
        if hostname in used_hosts:
            continue
        used_hosts.add(hostname)
        lifecycle = "active"
        if random.random() < 0.05:
            lifecycle = random.choice(["retired", "decommissioned"])
        records.append(
            AssetRecord(
                source_id=f"AE-{100000 + index}",
                hostname=hostname,
                fqdn=f"{hostname}.corp.example.com",
                ip_address=f"10.{20 + index % 40}.{index % 250}.{10 + index % 200}",
                os_name=os_name,
                os_version=versions[index % len(versions)],
                environment=random.choice(ENVIRONMENTS),
                business_group=business,
                application_group=random.choice(APPS),
                asset_owner=random.choice(
                    ["Platform Engineering", "Database Services",
                     "Middleware", "Security Operations"]),
                department=random.choice(["Technology", "Operations", "Finance IT"]),
                location=random.choice(LOCATIONS),
                lifecycle_status=lifecycle,
                source_modified_at=datetime.now(UTC) - timedelta(hours=index % 72),
                raw={"demo_data": True, "generator": "scripts/seed-demo.py"},
            )
        )
    return records


def _findings(family: str) -> tuple[NormalisedFinding, ...]:
    """A realistic-shape outstanding set; ~15% of hosts are fully patched."""
    found: list[NormalisedFinding] = []
    if random.random() < 0.85:
        for severity, chance in (
            ("critical", 0.22), ("important", 0.55),
            ("moderate", 0.6), ("low", 0.4),
        ):
            if random.random() > chance:
                continue
            count = random.choice([1, 1, 2, 3])
            for _ in range(count):
                released = datetime.now(UTC) - timedelta(days=random.randint(2, 160))
                advisory = {
                    "rhel": f"RHSA-2026:{random.randint(1000, 9999)}",
                    "windows": f"KB{random.randint(5000000, 5099999)}",
                    "solaris": f"{random.randint(350000, 359999)}-01",
                }[family]
                found.append(
                    NormalisedFinding(
                        advisory_id=advisory,
                        title=random.choice(TITLES[severity]),
                        severity=severity,
                        category="security" if severity in ("critical", "important")
                        else "bugfix",
                        released_at=released,
                        source_reference=f"demo:{advisory}",
                    )
                )
    return tuple(found)


def seed(asset_count: int = 120) -> None:
    records = _build_assets(asset_count)
    with connection() as conn:
        run_id = _start_run(conn, "assetexplorer", "manual", "seed-demo")
        created = 0
        for record in records:
            _, is_new = upsert_asset(conn, record, run_id)
            created += int(is_new)
        _finish_run(conn, run_id, "success", seen=len(records), created=created,
                    updated=len(records) - created,
                    details={"source": "assetexplorer", "demo_data": True})

    by_source: dict[str, list[PatchAssessment]] = {"satellite": [], "sccm": [],
                                                   "solaris": []}
    now = datetime.now(UTC)
    for record in records:
        if record.lifecycle_status != "active":
            continue
        family = record.os_family
        source = {"rhel": "satellite", "windows": "sccm",
                  "solaris": "solaris"}[family]
        # Deliberate gaps: a minority of hosts have no fresh assessment.
        roll = random.random()
        if roll < 0.06:
            continue  # never assessed -> unknown
        assessed_at = now - timedelta(hours=random.randint(1, 30))
        if roll > 0.94:
            assessed_at = now - timedelta(hours=random.randint(60, 200))  # stale
        by_source[source].append(
            PatchAssessment(
                source_system=source,
                source_ref=record.fqdn or record.hostname,
                hostname=record.hostname,
                fqdn=record.fqdn,
                ip_address=record.ip_address,
                os_name=record.os_name,
                os_version=record.os_version,
                assessed_at=assessed_at,
                status="complete",
                findings=_findings(family),
                source_reference=f"{source}:{record.hostname}",
            )
        )
    # Two assessments for hosts that do not exist, to exercise reconciliation.
    by_source["satellite"].append(
        PatchAssessment(
            source_system="satellite", source_ref="lnx-unknown-host.corp.example.com",
            hostname="lnx-unknown-host", fqdn="lnx-unknown-host.corp.example.com",
            ip_address="10.99.99.99", os_name="Red Hat Enterprise Linux",
            os_version="9.5", assessed_at=now, status="complete",
            findings=(NormalisedFinding("RHSA-2026:9999", "Demo unmatched advisory",
                                        "important"),),
            source_reference="satellite:lnx-unknown-host",
        )
    )

    for source, assessments in by_source.items():
        with connection() as conn:
            run_id = _start_run(conn, source, "manual", "seed-demo")
        counts = ingest_assessments(assessments, run_id=run_id, source_key=source)
        with connection() as conn:
            _finish_run(conn, run_id, "success", seen=len(assessments),
                        updated=counts["matched"], rejected=counts["failed"],
                        unmatched=counts["unmatched"],
                        details={"source": source, "demo_data": True})
        print(f"  {source:10s} assessments={len(assessments):3d} "
              f"matched={counts['matched']:3d} unmatched={counts['unmatched']}")

    print("Evaluating and capturing snapshots…")
    print("  live evaluation:", refresh_compliance()["compliance_pct"])
    now = datetime.now(UTC)
    # Six months of history so the trend chart has something honest to show.
    for offset in range(6, -1, -1):
        month_start = (now.replace(day=1) - timedelta(days=offset * 30)).replace(day=1)
        month_end = (month_start + timedelta(days=32)).replace(day=1)
        create_snapshot(
            scope="monthly", period_start=month_start, period_end=month_end,
            generated_by="seed-demo",
            now=month_start.replace(hour=23, minute=50),
        )
        # Daily points inside the month, so monthly roll-ups have samples.
        for day in range(0, 28, 3):
            when = (month_start + timedelta(days=day)).replace(hour=2)
            if when > now:
                break
            create_snapshot(
                scope="daily", period_start=when.replace(hour=0, minute=0, second=0),
                period_end=when.replace(hour=0, minute=0, second=0),
                generated_by="seed-demo", now=when,
            )
    quarter_start = now.replace(month=((now.month - 1) // 3) * 3 + 1, day=1, hour=0,
                                minute=0, second=0, microsecond=0)
    create_snapshot(scope="quarterly", period_start=quarter_start,
                    period_end=now, generated_by="seed-demo")
    print("Done.")


def reset() -> None:
    with connection() as conn:
        conn.execute(text("DELETE FROM reporting.delivery_attempts"))
        conn.execute(text("DELETE FROM reporting.reports"))
        conn.execute(text("DELETE FROM reporting.job_executions"))
        conn.execute(text("DELETE FROM reporting.schedules"))
        conn.execute(text("DELETE FROM compliance.snapshot_groups"))
        conn.execute(text("DELETE FROM compliance.snapshots"))
        conn.execute(text("DELETE FROM compliance.findings"))
        conn.execute(text("DELETE FROM compliance.asset_latest_assessment"))
        conn.execute(text("DELETE FROM compliance.assessments"))
        conn.execute(text("DELETE FROM compliance.asset_compliance"))
        conn.execute(text("DELETE FROM inventory.reconciliation_conflicts"))
        conn.execute(text("DELETE FROM inventory.asset_source_links"))
        conn.execute(text("DELETE FROM inventory.asset_aliases"))
        conn.execute(text("DELETE FROM inventory.assets"))
        conn.execute(text("DELETE FROM inventory.sync_runs"))
    print("Demo data removed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true",
                        help="delete existing inventory/compliance demo rows first")
    parser.add_argument("--assets", type=int, default=120)
    args = parser.parse_args()
    if args.reset:
        reset()
    try:
        seed(args.assets)
    except Exception as exc:  # pragma: no cover - operator feedback
        print(f"seeding failed: {exc}", file=sys.stderr)
        raise
