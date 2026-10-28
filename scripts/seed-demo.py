#!/usr/bin/env python3
"""Populate a development database with SYNTHETIC demo data.

Nothing here represents a real estate. The records are invented so that the
dashboard, drill-downs, snapshots and PDF reports can be exercised before the
AssetExplorer / Satellite / SCCM / Solaris integrations are pointed at real
systems. Every created asset carries ``raw.demo_data = true``, and every
generated snapshot says so in its methodology.

The default is 100 assets and five years of history. The historical daily
snapshots are generated from an explicit month/quarter/year scenario rather
than by re-evaluating today's estate in the past: at every basis some periods
meet the 95% target and some miss it, and one quarter shows a collection
outage. They are still synthetic records, not a reconstruction of any real
assessment history.

Usage::

    PYTHONPATH=services/common:services python3 scripts/seed-demo.py \
        [--reset] [--assets 100] [--years 5]

Live records are written through the same functions the collectors use
(``upsert_asset``, ``ingest_assessments``, ``create_snapshot``), so the current
estate exercises the real ingestion and evaluation path. The historical
snapshots are inserted directly because the past cannot be re-evaluated from
today's assessments; their methodology is marked ``synthetic_history``.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import uuid
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import text

from patchtrack_common.db import connection
from inventory.app.connectors.base import AssetRecord, NormalisedFinding, PatchAssessment
from inventory.app.sync import _finish_run, _start_run, ingest_assessments, upsert_asset
from compliance.app.evaluator import active_policy, create_snapshot, refresh_compliance

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
SEVERITIES = ("critical", "important", "moderate", "low", "unknown")

# A monthly scenario assembled from three layers, each with a different job:
#
# * YEAR_SWING makes whole years land on different sides of the target, so the
#   yearly basis has both met and missed years. Indexed by year modulo five so
#   every five-year window contains each value once.
# * QUARTER_SWING and MONTH_SWING move individual quarters and months within
#   the year. MONTH_SWING sums to zero over a calendar year, so the yearly
#   average is the year swing; its amplitude is larger than any year swing, so
#   every twelve-month window contains months on both sides of the target.
# * The lowest year and lowest quarter are used as a collection outage below.
YEAR_SWING = {0: 1.6, 1: -1.7, 2: 0.9, 3: -1.9, 4: 0.5}
QUARTER_SWING = (0.9, 1.4, -1.2, -1.1)  # Jan-Mar, Apr-Jun, Jul-Sep, Oct-Dec
MONTH_SWING = (
    1.1, 1.9, 2.2, 1.9, 1.1, -0.2, -1.4, -2.2, -2.2, -1.6, -0.6, 0.0,
)

# How much of the remaining (or total) gap each group carries. These make the
# dashboard's breakdowns tell a story instead of repeating the overall average.
OS_FAMILY_GAP = {"rhel": 1.0, "windows": 1.4, "solaris": 1.9, "other": 1.5}
OS_VERSION_GAP = {"9.4": 1.15, "9.5": 0.95, "9.6": 0.7, "8.10": 1.5,
                  "2022": 1.0, "2019": 1.6, "11.4": 1.9}
ENVIRONMENT_GAP = {"Production": 0.75, "Pre-production": 0.95,
                   "Staging": 1.2, "Development": 1.6, "DMZ": 1.35}
ENVIRONMENT_UNKNOWN = {"Production": 0.6, "Pre-production": 1.0,
                       "Staging": 1.4, "Development": 1.7, "DMZ": 1.5}
BUSINESS_GAP = {"Payments": 0.6, "Core Banking": 1.3, "Customer Portal": 1.1,
                "Data Platform": 1.2, "Identity": 0.8, "Logistics": 1.4,
                "Analytics": 1.5, "Messaging": 0.9}
DIMENSIONS = ("os_family", "os_version", "environment", "business_group",
              "application_group")


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


def _add_months(value: date, months: int) -> date:
    index = value.year * 12 + value.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def _month_bounds(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    return start, _add_months(start, 1) - timedelta(days=1)


def _quarter_bounds(day: date) -> tuple[date, date]:
    start = date(day.year, ((day.month - 1) // 3) * 3 + 1, 1)
    return start, _add_months(start, 3) - timedelta(days=1)


def _year_swing(day: date) -> float:
    return YEAR_SWING[day.year % len(YEAR_SWING)]


def _quarter_swing(day: date) -> float:
    return QUARTER_SWING[(day.month - 1) // 3]


def _scenario_base(day: date) -> float:
    """The scenario midpoint for the calendar month containing ``day``."""
    return (
        95.0 + _year_swing(day) + _quarter_swing(day)
        + MONTH_SWING[day.month - 1]
    )


def _is_outage(day: date) -> bool:
    """One quarter in the five-year cycle has a failed collection period."""
    return (
        _year_swing(day) == min(YEAR_SWING.values())
        and _quarter_swing(day) == min(QUARTER_SWING)
    )


def _history_start(today: date, years: int) -> date:
    """First day of the oldest of ``years`` trailing calendar months."""
    return _add_months(today.replace(day=1), -(years * 12 - 1))


def _day_plan(day: date, in_scope: int) -> dict[str, int | float | None]:
    """Counts for one synthetic day.

    Compliance follows the quarter's midpoint plus a within-quarter climb (the
    patch cycle) and a small deterministic wobble. The lowest quarter in the
    cycle doubles as a collection outage: unknown rises sharply, which pulls the
    server percentage down without inventing assessments that do not exist.
    """
    rng = random.Random(day.toordinal() * 7919 + 101)
    base = _scenario_base(day)
    month_start, month_end = _month_bounds(day)
    span = (month_end - month_start).days + 1
    progress = (day - month_start).days / span
    # The climb is within the month: patches land through the cycle, then a new
    # month's advisories reset the estate. A monthly cycle keeps the monthly
    # average close to its scenario midpoint instead of tilting every month in
    # the same direction within a quarter.
    pct = base + 1.2 * (2 * progress - 1) + rng.uniform(-0.35, 0.35)
    outage = _is_outage(day)
    if outage:
        pct -= 18.0
    pct = max(55.0, min(99.0, pct))
    compliant = round(in_scope * pct / 100)
    remaining = in_scope - compliant
    if outage:
        unknown = min(round(in_scope * 0.22), remaining)
        stale = min(round(in_scope * 0.04), remaining - unknown)
    else:
        unknown = min(max(1, round(in_scope * 0.02)), remaining) if remaining else 0
        stale = 0
        if remaining - unknown >= 2:
            stale = min(max(1, round(in_scope * 0.01)), remaining - unknown)
    non_compliant = remaining - unknown - stale

    critical = round(non_compliant * rng.uniform(0.6, 1.4))
    important = round(non_compliant * rng.uniform(1.6, 3.0))
    moderate = round((in_scope - unknown) * rng.uniform(0.06, 0.14))
    low = round((in_scope - unknown) * rng.uniform(0.03, 0.08))
    severity = {
        "critical": critical, "important": important,
        "moderate": moderate, "low": low, "unknown": 0,
    }
    total_findings = sum(severity.values())
    compliance_pct = (
        round(compliant / in_scope * 100, 2) if in_scope else None
    )
    return {
        "in_scope": in_scope,
        "compliant": compliant,
        "non_compliant": non_compliant,
        "unknown": unknown,
        "stale": stale,
        "compliance_pct": compliance_pct,
        "critical_findings": critical,
        "total_findings": total_findings,
        "severity": severity,
        "outage": outage,
    }


def _allocate(total: int, weights: list[float], caps: list[int]) -> list[int]:
    """Split ``total`` across buckets without exceeding any bucket's cap."""
    count = len(weights)
    result = [0] * count
    if total <= 0 or count == 0:
        return result
    weight_sum = sum(weights)
    if weight_sum <= 0:
        weights = [1.0] * count
        weight_sum = float(count)
    targets = [total * weight / weight_sum for weight in weights]
    for index in range(count):
        result[index] = min(int(targets[index]), caps[index])
    remaining = total - sum(result)
    while remaining > 0:
        candidates = [i for i in range(count) if result[i] < caps[i]]
        if not candidates:
            break
        chosen = max(
            candidates,
            key=lambda i: (targets[i] - result[i], weights[i], -i),
        )
        result[chosen] += 1
        remaining -= 1
    return result


def _app_gap(key: str) -> float:
    return 0.7 + (sum(ord(char) for char in key) % 10) / 10.0


def _gap_factor(dimension: str, key: str) -> float:
    if dimension == "os_family":
        return OS_FAMILY_GAP.get(key, 1.3)
    if dimension == "os_version":
        return OS_VERSION_GAP.get(key, 1.2)
    if dimension == "environment":
        return ENVIRONMENT_GAP.get(key, 1.1)
    if dimension == "business_group":
        return BUSINESS_GAP.get(key, 1.1)
    return _app_gap(key)


def _unknown_factor(dimension: str, key: str) -> float:
    if dimension == "environment":
        return ENVIRONMENT_UNKNOWN.get(key, 1.0)
    if dimension == "os_family":
        return {"rhel": 0.8, "windows": 1.0, "solaris": 1.8}.get(key, 1.2)
    return 1.0


def _stale_factor(dimension: str, key: str) -> float:
    if dimension == "environment":
        return {"Production": 0.8, "Staging": 1.2, "Development": 1.4,
                "DMZ": 1.8}.get(key, 1.0)
    if dimension == "os_family":
        return {"rhel": 0.9, "windows": 1.0, "solaris": 2.0}.get(key, 1.2)
    return 1.0


def _group_sizes(records: list[AssetRecord]) -> dict[str, dict[str, dict[str, int]]]:
    getters = {
        "os_family": lambda record: record.os_family,
        "os_version": lambda record: record.os_version or "UNKNOWN",
        "environment": lambda record: record.environment or "UNKNOWN",
        "business_group": lambda record: record.business_group or "UNKNOWN",
        "application_group": lambda record: record.application_group or "UNKNOWN",
    }
    sizes: dict[str, dict[str, dict[str, int]]] = {}
    for dimension, getter in getters.items():
        buckets: dict[str, dict[str, int]] = {}
        for record in records:
            key = str(getter(record) or "UNKNOWN")
            bucket = buckets.setdefault(key, {"total": 0, "in_scope": 0})
            bucket["total"] += 1
            if record.lifecycle_status == "active":
                bucket["in_scope"] += 1
        sizes[dimension] = buckets
    return sizes


def _group_rows(
    snapshot_id: uuid.UUID,
    sizes: dict[str, dict[str, int]],
    plan: dict[str, int | float | None],
    dimension: str,
) -> list[dict[str, object]]:
    keys = sorted(sizes)
    caps = [sizes[key]["in_scope"] for key in keys]
    non_compliant = _allocate(
        int(plan["non_compliant"]),
        [cap * _gap_factor(dimension, key) for cap, key in zip(caps, keys)],
        caps,
    )
    unknown = _allocate(
        int(plan["unknown"]),
        [cap * _unknown_factor(dimension, key) for cap, key in zip(caps, keys)],
        [caps[i] - non_compliant[i] for i in range(len(keys))],
    )
    stale = _allocate(
        int(plan["stale"]),
        [cap * _stale_factor(dimension, key) for cap, key in zip(caps, keys)],
        [caps[i] - non_compliant[i] - unknown[i] for i in range(len(keys))],
    )
    finding_count = _allocate(
        int(plan["total_findings"]),
        [max(cap, 1) * _gap_factor(dimension, key) for cap, key in zip(caps, keys)],
        [int(plan["total_findings"])] * len(keys),
    )
    rows = []
    for index, key in enumerate(keys):
        compliant = caps[index] - non_compliant[index] - unknown[index] - stale[index]
        rows.append({
            "snapshot_id": snapshot_id,
            "dimension": dimension,
            "group_key": key,
            "total_servers": sizes[key]["total"],
            "compliant": compliant,
            "non_compliant": non_compliant[index],
            "unknown": unknown[index],
            "stale": stale[index],
            "finding_count": finding_count[index],
        })
    return rows


def _snapshot_group_rows(
    snapshot_id: uuid.UUID,
    group_sizes: dict[str, dict[str, dict[str, int]]],
    plan: dict[str, int | float | None],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for dimension in DIMENSIONS:
        rows.extend(_group_rows(snapshot_id, group_sizes[dimension], plan, dimension))
    severity = plan["severity"]
    for level in SEVERITIES:
        rows.append({
            "snapshot_id": snapshot_id, "dimension": "severity",
            "group_key": level, "total_servers": 0, "compliant": 0,
            "non_compliant": 0, "unknown": 0, "stale": 0,
            "finding_count": int(severity.get(level, 0)),
        })
    return rows


def _methodology(policy: dict, target_pct: float) -> dict[str, object]:
    return {
        "population": "in-scope assets with active lifecycle",
        "numerator": "assets whose state is compliant",
        "denominator": "compliant + non_compliant + unknown + stale",
        "unknown_treatment": "counted in denominator, never as compliant",
        "freshness_hours": policy.get("freshness_hours"),
        "blocking_severities": policy.get("default_rule", {}).get(
            "blocking_severities"
        ),
        "target_pct": target_pct,
        "quarterly_target_pct": policy.get("quarterly_target_pct"),
        "policy_name": policy.get("name"),
        "demo_data": True,
        "synthetic_history": True,
        "note": ("Generated by scripts/seed-demo.py from a quarterly scenario; "
                 "not a reconstruction of a real assessment history."),
    }


def _insert_history(
    conn,
    records: list[AssetRecord],
    *,
    years: int,
    policy: dict,
    version_id: str | None,
) -> dict[str, int]:
    today = datetime.now(UTC).date()
    start = _history_start(today, years)
    in_scope = sum(1 for record in records if record.lifecycle_status == "active")
    total = len(records)
    excluded = total - in_scope
    target_pct = float(policy.get("target_pct", 95.0))
    methodology = json.dumps(_methodology(policy, target_pct))
    group_sizes = _group_sizes(records)

    snapshot_sql = text(
        """
        INSERT INTO compliance.snapshots (
            id, scope, period_start, period_end, captured_at, policy_version_id,
            filters, total_servers, compliant_servers, non_compliant_servers,
            unknown_servers, stale_servers, excluded_servers, compliance_pct,
            target_pct, critical_findings, total_findings, generated_by,
            methodology
        ) VALUES (
            :id, :scope, :ps, :pe, :captured, :pv, CAST(:filters AS jsonb),
            :total, :compliant, :non_compliant, :unknown, :stale, :excluded,
            :pct, :target, :critical, :findings, 'seed-demo',
            CAST(:methodology AS jsonb)
        )
        """
    )
    group_sql = text(
        """
        INSERT INTO compliance.snapshot_groups (
            snapshot_id, dimension, group_key, total_servers, compliant,
            non_compliant, unknown, stale, finding_count
        ) VALUES (
            :snapshot_id, :dimension, :group_key, :total_servers, :compliant,
            :non_compliant, :unknown, :stale, :finding_count
        )
        """
    )

    plans: dict[date, dict[str, int | float | None]] = {}
    snapshots: list[dict[str, object]] = []
    group_rows: list[dict[str, object]] = []
    day = start
    yesterday = today - timedelta(days=1)
    while day <= yesterday:
        plan = _day_plan(day, in_scope)
        plans[day] = plan
        snapshot_id = uuid.uuid4()
        captured = datetime.combine(day, time(2, 0), tzinfo=UTC)
        snapshots.append({
            "id": snapshot_id, "scope": "daily", "ps": day, "pe": day,
            "captured": captured, "pv": version_id, "filters": "{}",
            "total": total, "compliant": int(plan["compliant"]),
            "non_compliant": int(plan["non_compliant"]),
            "unknown": int(plan["unknown"]), "stale": int(plan["stale"]),
            "excluded": excluded, "pct": plan["compliance_pct"],
            "target": target_pct, "critical": int(plan["critical_findings"]),
            "findings": int(plan["total_findings"]),
            "methodology": methodology,
        })
        group_rows.extend(_snapshot_group_rows(snapshot_id, group_sizes, plan))
        day += timedelta(days=1)
        if len(group_rows) >= 4000:
            conn.execute(snapshot_sql, snapshots)
            conn.execute(group_sql, group_rows)
            snapshots = []
            group_rows = []
    if snapshots:
        conn.execute(snapshot_sql, snapshots)
    if group_rows:
        conn.execute(group_sql, group_rows)
        group_rows = []

    # Completed months and quarters are frozen at their last day's plan so the
    # report service finds a period snapshot at the calendar period start.
    month_start = start
    while True:
        _, month_end = _month_bounds(month_start)
        if month_end >= today:
            break
        plan = plans[month_end]
        snapshot_id = uuid.uuid4()
        snapshots = [{
            "id": snapshot_id, "scope": "monthly", "ps": month_start,
            "pe": month_end,
            "captured": datetime.combine(month_end, time(23, 50), tzinfo=UTC),
            "pv": version_id, "filters": "{}", "total": total,
            "compliant": int(plan["compliant"]),
            "non_compliant": int(plan["non_compliant"]),
            "unknown": int(plan["unknown"]), "stale": int(plan["stale"]),
            "excluded": excluded, "pct": plan["compliance_pct"],
            "target": target_pct, "critical": int(plan["critical_findings"]),
            "findings": int(plan["total_findings"]),
            "methodology": methodology,
        }]
        conn.execute(snapshot_sql, snapshots)
        conn.execute(
            group_sql,
            _snapshot_group_rows(snapshot_id, group_sizes, plan),
        )
        month_start = _add_months(month_start, 1)

    quarter_start, _ = _quarter_bounds(start)
    while True:
        _, quarter_end = _quarter_bounds(quarter_start)
        if quarter_end >= today:
            break
        plan = plans[quarter_end]
        snapshot_id = uuid.uuid4()
        snapshots = [{
            "id": snapshot_id, "scope": "quarterly", "ps": quarter_start,
            "pe": quarter_end,
            "captured": datetime.combine(quarter_end, time(23, 50), tzinfo=UTC),
            "pv": version_id, "filters": "{}", "total": total,
            "compliant": int(plan["compliant"]),
            "non_compliant": int(plan["non_compliant"]),
            "unknown": int(plan["unknown"]), "stale": int(plan["stale"]),
            "excluded": excluded, "pct": plan["compliance_pct"],
            "target": target_pct, "critical": int(plan["critical_findings"]),
            "findings": int(plan["total_findings"]),
            "methodology": methodology,
        }]
        conn.execute(snapshot_sql, snapshots)
        conn.execute(
            group_sql,
            _snapshot_group_rows(snapshot_id, group_sizes, plan),
        )
        quarter_start = _add_months(quarter_start, 3)

    return {"days": len(plans), "start": start, "end": yesterday}


def _assign_live_states(
    records: list[AssetRecord], plan: dict[str, int | float | None]
) -> tuple[dict[str, str], set[str]]:
    """Map each active host onto today's planned state.

    Assigning the states explicitly keeps the live dashboard, the live lists and
    today's snapshot consistent with the last point of the historical scenario.
    """
    rng = random.Random(8601)
    ordered = list(records)
    rng.shuffle(ordered)
    states: dict[str, str] = {}
    index = 0
    for state, key in (("unknown", "unknown"), ("stale", "stale"),
                       ("non_compliant", "non_compliant"),
                       ("compliant", "compliant")):
        count = int(plan[key])
        for record in ordered[index:index + count]:
            states[record.hostname] = state
        index += count
    failed = {record.hostname for record in ordered[: int(plan["unknown"])][:1]}
    return states, failed


def _findings(
    family: str, *, blocking: bool, assessed_at: datetime
) -> tuple[NormalisedFinding, ...]:
    """Findings for one assessment; only blocking hosts carry critical/important."""
    chosen: list[str] = []
    if blocking:
        chosen.extend(
            random.choice(["critical", "important"])
            for _ in range(random.randint(1, 3))
        )
    if random.random() < 0.6:
        for severity in ("moderate", "low"):
            if random.random() < 0.5:
                chosen.append(severity)
    found: list[NormalisedFinding] = []
    for severity in chosen:
        released = assessed_at - timedelta(days=random.randint(2, 160))
        advisory = {
            "rhel": f"RHSA-{released.year}:{random.randint(1000, 9999)}",
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


def seed(asset_count: int = 100, years: int = 5) -> None:
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

    active = [record for record in records if record.lifecycle_status == "active"]
    today = datetime.now(UTC).date()
    live_plan = _day_plan(today, len(active))
    live_states, failed_hosts = _assign_live_states(active, live_plan)

    by_source: dict[str, list[PatchAssessment]] = {"satellite": [], "sccm": [],
                                                   "solaris": []}
    now = datetime.now(UTC)
    for record in active:
        family = record.os_family
        source = {"rhel": "satellite", "windows": "sccm",
                  "solaris": "solaris"}[family]
        state = live_states.get(record.hostname, "compliant")
        if state == "unknown" and record.hostname not in failed_hosts:
            continue  # never assessed -> unknown
        if state == "unknown":
            by_source[source].append(
                PatchAssessment(
                    source_system=source,
                    source_ref=record.fqdn or record.hostname,
                    hostname=record.hostname, fqdn=record.fqdn,
                    ip_address=record.ip_address, os_name=record.os_name,
                    os_version=record.os_version, assessed_at=now,
                    status="failed", findings=(),
                    source_reference=f"{source}:{record.hostname}",
                )
            )
            continue
        if state == "stale":
            assessed_at = now - timedelta(hours=random.randint(60, 200))
        else:
            assessed_at = now - timedelta(hours=random.randint(1, 30))
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
                findings=_findings(
                    family, blocking=state == "non_compliant",
                    assessed_at=assessed_at,
                ),
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

    print(f"Generating {years} years of synthetic daily snapshots…")
    policy, version_id = active_policy()
    with connection() as conn:
        history = _insert_history(
            conn, records, years=years, policy=policy, version_id=version_id,
        )
    print(f"  daily snapshots={history['days']} "
          f"from {history['start']} to {history['end']}")

    print("Evaluating and capturing the current period…")
    live = refresh_compliance(now=now)
    month_start, _ = _month_bounds(today)
    quarter_start, _ = _quarter_bounds(today)
    create_snapshot(
        scope="daily", period_start=datetime.combine(today, time(0), tzinfo=UTC),
        period_end=datetime.combine(today, time(0), tzinfo=UTC),
        generated_by="seed-demo", now=now,
    )
    create_snapshot(
        scope="monthly", period_start=datetime.combine(
            month_start, time(0), tzinfo=UTC),
        period_end=datetime.combine(today, time(0), tzinfo=UTC),
        generated_by="seed-demo", now=now,
    )
    create_snapshot(
        scope="quarterly", period_start=datetime.combine(
            quarter_start, time(0), tzinfo=UTC),
        period_end=datetime.combine(today, time(0), tzinfo=UTC),
        generated_by="seed-demo", now=now,
    )
    print(f"  live evaluation: {live['compliance_pct']}% "
          f"({live['compliant']} of {live['in_scope']} in scope)")
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
    parser.add_argument("--assets", type=int, default=100)
    parser.add_argument("--years", type=int, default=5,
                        help="years of synthetic daily snapshot history")
    args = parser.parse_args()
    if args.reset:
        reset()
    try:
        seed(args.assets, args.years)
    except Exception as exc:  # pragma: no cover - operator feedback
        print(f"seeding failed: {exc}", file=sys.stderr)
        raise
