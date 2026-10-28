"""The demo history must be internally consistent and visibly varied.

The seed script is synthetic by design, but synthetic data that does not add up
teaches the wrong thing: a snapshot whose states exceed its population, or a
five-year trend where every quarter shows the same percentage, would hide the
bugs the demo exists to expose. These tests exercise the pure generators
without a database.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def load_seed_module():
    spec = importlib.util.spec_from_file_location(
        "seed_demo", SCRIPTS / "seed-demo.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


seed_demo = load_seed_module()


def fake_records(count: int = 40) -> list[SimpleNamespace]:
    families = ("rhel", "windows", "solaris")
    versions = ("9.4", "9.6", "2022")
    environments = ("Production", "Development", "DMZ", "Staging")
    businesses = ("Payments", "Analytics", "Core Banking")
    apps = ("ledger", "api-gateway")
    return [
        SimpleNamespace(
            hostname=f"host-{index:03d}",
            os_family=families[index % len(families)],
            os_version=versions[index % len(versions)],
            environment=environments[index % len(environments)],
            business_group=businesses[index % len(businesses)],
            application_group=apps[index % len(apps)],
            lifecycle_status="retired" if index % 20 == 0 else "active",
        )
        for index in range(count)
    ]


def test_five_years_means_sixty_months() -> None:
    today = date(2026, 10, 27)
    start = seed_demo._history_start(today, 5)
    months = (today.year - start.year) * 12 + today.month - start.month + 1
    assert months == 60
    assert start.day == 1


def test_every_day_plan_accounts_for_the_whole_population() -> None:
    day = date(2022, 1, 1)
    for _ in range(0, 365 * 5, 17):
        plan = seed_demo._day_plan(day, 95)
        states = (
            plan["compliant"] + plan["non_compliant"]
            + plan["unknown"] + plan["stale"]
        )
        assert states == 95, (day, plan)
        assert plan["compliance_pct"] == round(plan["compliant"] / 95 * 100, 2)
        assert sum(plan["severity"].values()) == plan["total_findings"]
        assert plan["severity"]["critical"] == plan["critical_findings"]
        day += timedelta(days=17)


def test_scenario_contains_a_spectrum_at_every_basis() -> None:
    today = date(2026, 10, 27)
    start = seed_demo._history_start(today, 5)
    buckets = {
        "month": defaultdict(list),
        "quarter": defaultdict(list),
        "year": defaultdict(list),
    }
    outages = 0
    day = start
    while day < today:
        plan = seed_demo._day_plan(day, 95)
        buckets["month"][(day.year, day.month)].append(plan["compliance_pct"])
        buckets["quarter"][(day.year, (day.month - 1) // 3 + 1)].append(
            plan["compliance_pct"]
        )
        buckets["year"][day.year].append(plan["compliance_pct"])
        outages += int(plan["outage"])
        day += timedelta(days=1)

    averages = {
        kind: {
            key: sum(values) / len(values)
            for key, values in grouped.items()
        }
        for kind, grouped in buckets.items()
    }
    # Each basis must show both outcomes, not a uniform pass or failure run.
    expected = {"month": 10, "quarter": 5, "year": 2}
    for kind, minimum in expected.items():
        met = [value for value in averages[kind].values() if value >= 95]
        missed = [value for value in averages[kind].values() if value < 95]
        assert len(met) >= minimum, (kind, averages[kind])
        assert len(missed) >= minimum, (kind, averages[kind])
    # The outage quarter must be visibly worse than an ordinary miss.
    assert outages > 0
    assert min(averages["quarter"].values()) < 80


def test_group_rows_add_up_to_the_snapshot() -> None:
    records = fake_records()
    sizes = seed_demo._group_sizes(records)
    plan = seed_demo._day_plan(date(2026, 10, 27), 38)

    for dimension in seed_demo.DIMENSIONS:
        rows = seed_demo._group_rows(
            uuid.uuid4(), sizes[dimension], plan, dimension
        )
        for field in ("compliant", "non_compliant", "unknown", "stale"):
            assert sum(row[field] for row in rows) == plan[field], dimension
        assert sum(row["finding_count"] for row in rows) == plan["total_findings"]
        for row in rows:
            in_scope = sizes[dimension][row["group_key"]]["in_scope"]
            assert row["compliant"] + row["non_compliant"] + row["unknown"] \
                + row["stale"] == in_scope, (dimension, row)
            assert row["total_servers"] == sizes[dimension][row["group_key"]]["total"]
        # Retired assets stay in a group's total but never in its states.
        assert sum(row["total_servers"] for row in rows) == len(records)


def test_live_assignment_places_every_host_in_exactly_one_state() -> None:
    records = [
        record for record in fake_records(100)
        if record.lifecycle_status == "active"
    ]
    plan = seed_demo._day_plan(date(2026, 10, 27), 95)
    states, failed = seed_demo._assign_live_states(records, plan)

    assert set(states) == {record.hostname for record in records}
    for name in ("compliant", "non_compliant", "unknown", "stale"):
        assert list(states.values()).count(name) == plan[name]
    assert failed <= {host for host, state in states.items() if state == "unknown"}
    assert len(failed) <= 1
