"""Compliance calculation engine.

These tests pin the rules the business requirements make non-negotiable:
unknown and stale are never compliant, excluded stays out of the denominator,
an empty population yields no percentage, and the server-level percentage is
never mixed with patch-level counts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from compliance.app.engine import (
    DEFAULT_POLICY,
    AssetInput,
    aggregate,
    evaluate_asset,
    group_breakdown,
    period_bounds,
    remediation_due,
    rule_for,
    severity_breakdown,
)

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def asset(**overrides) -> AssetInput:
    base = {
        "asset_id": "a1",
        "hostname": "srv-001",
        "os_family": "rhel",
        "environment": "Production",
        "business_group": "Payments",
        "assessment_at": NOW - timedelta(hours=1),
        "assessment_status": "complete",
        "severity_counts": {},
    }
    base.update(overrides)
    return AssetInput(**base)


def policy(**overrides) -> dict:
    merged = {**DEFAULT_POLICY, **overrides}
    return merged


# ------------------------------------------------------------------ states
def test_clean_asset_is_compliant():
    result = evaluate_asset(asset(), policy(), now=NOW)
    assert result.state == "compliant"


def test_blocking_finding_is_non_compliant():
    result = evaluate_asset(
        asset(severity_counts={"critical": 1}), policy(), now=NOW)
    assert result.state == "non_compliant"
    assert "1 outstanding" in result.reason


def test_moderate_finding_alone_is_compliant_by_default():
    """Only critical and important block in the baseline policy."""
    result = evaluate_asset(
        asset(severity_counts={"moderate": 5, "low": 9}), policy(), now=NOW)
    assert result.state == "compliant"
    # The patch counts are still reported; they are simply not blocking.
    assert result.total_patches == 14


def test_max_blocking_findings_tolerance():
    tolerant = policy(max_blocking_findings=2)
    assert evaluate_asset(
        asset(severity_counts={"critical": 2}), tolerant, now=NOW).state == "compliant"
    assert evaluate_asset(
        asset(severity_counts={"critical": 3}), tolerant, now=NOW).state == "non_compliant"


def test_missing_assessment_is_unknown_not_compliant():
    result = evaluate_asset(asset(assessment_at=None), policy(), now=NOW)
    assert result.state == "unknown"
    assert result.assessment_age_hours is None


def test_failed_assessment_is_unknown():
    result = evaluate_asset(
        asset(assessment_status="failed"), policy(), now=NOW)
    assert result.state == "unknown"


def test_assessment_at_freshness_boundary_is_not_stale():
    """The threshold is inclusive: exactly 48h old is still acceptable."""
    result = evaluate_asset(
        asset(assessment_at=NOW - timedelta(hours=48)), policy(), now=NOW)
    assert result.state == "compliant"


def test_assessment_just_past_freshness_is_stale():
    """One second past the threshold is stale, not rounded down to fresh."""
    result = evaluate_asset(
        asset(assessment_at=NOW - timedelta(hours=48, seconds=1)),
        policy(), now=NOW)
    assert result.state == "stale"
    # The reported age is rounded to two decimals for display only.
    assert result.assessment_age_hours == 48.0
    assert "48.0h old" in result.reason


def test_stale_wins_over_outstanding_findings():
    """An old assessment cannot support a non-compliant verdict either."""
    result = evaluate_asset(
        asset(assessment_at=NOW - timedelta(days=30),
              severity_counts={"critical": 9}),
        policy(), now=NOW)
    assert result.state == "stale"


def test_out_of_scope_asset_is_excluded():
    result = evaluate_asset(asset(in_scope=False), policy(), now=NOW)
    assert result.state == "excluded"


@pytest.mark.parametrize("lifecycle", ["retired", "decommissioned"])
def test_retired_asset_is_excluded(lifecycle):
    result = evaluate_asset(
        asset(lifecycle_status=lifecycle), policy(), now=NOW)
    assert result.state == "excluded"


def test_policy_excluded_environment_is_excluded():
    result = evaluate_asset(
        asset(environment="Development"),
        policy(excluded_environments=["Development"]), now=NOW)
    assert result.state == "excluded"


def test_naive_timestamps_are_treated_as_utc():
    naive = (NOW - timedelta(hours=100)).replace(tzinfo=None)
    result = evaluate_asset(asset(assessment_at=naive), policy(), now=NOW)
    assert result.state == "stale"


# ----------------------------------------------------------- scope rules
def test_scope_rule_matching_is_case_insensitive():
    configured = policy(scope_rules=[{
        "match": {"environment": "production"},
        "blocking_severities": ["moderate"],
    }])
    rule = rule_for(configured, asset(environment="Production"))
    assert rule["blocking_severities"] == ["moderate"]


def test_unmatched_asset_falls_back_to_default_rule():
    configured = policy(scope_rules=[{
        "match": {"environment": "DMZ"},
        "blocking_severities": ["critical", "important", "moderate", "low"],
    }])
    assert rule_for(configured, asset(environment="Production")) == \
        configured["default_rule"]


def test_scope_rule_overrides_blocking_severities():
    configured = policy(scope_rules=[{
        "match": {"business_group": "Payments"},
        "blocking_severities": ["critical"],
    }])
    result = evaluate_asset(
        asset(severity_counts={"important": 3}), configured, now=NOW)
    assert result.state == "compliant"


def test_empty_scope_rule_matches_nothing():
    configured = policy(scope_rules=[{"match": {}, "blocking_severities": []}])
    assert rule_for(configured, asset()) == configured["default_rule"]


# ------------------------------------------------------------ population
def test_percentage_is_compliant_over_the_full_denominator():
    evaluations = [evaluate_asset(asset(asset_id=f"a{i}"), policy(), now=NOW)
                   for i in range(4)]
    unknown = evaluate_asset(asset(asset_id="u", assessment_at=None), policy(), now=NOW)
    stale = evaluate_asset(
        asset(asset_id="s", assessment_at=NOW - timedelta(days=5)), policy(), now=NOW)
    bad = evaluate_asset(
        asset(asset_id="b", severity_counts={"critical": 1}), policy(), now=NOW)

    pop = aggregate(evaluations + [unknown, stale, bad], target_pct=95.0)
    assert (pop.compliant, pop.non_compliant, pop.unknown, pop.stale) == (4, 1, 1, 1)
    assert pop.denominator == 7
    assert pop.compliance_pct == round(4 / 7 * 100, 2)
    assert pop.gap_pct == round(4 / 7 * 100 - 95.0, 2)


def test_excluded_assets_leave_the_denominator():
    kept = evaluate_asset(asset(asset_id="k"), policy(), now=NOW)
    excluded = evaluate_asset(asset(asset_id="x", in_scope=False), policy(), now=NOW)
    pop = aggregate([kept, excluded], target_pct=95.0)
    assert pop.total == 2
    assert pop.excluded == 1
    assert pop.denominator == 1
    assert pop.compliance_pct == 100.0


def test_empty_population_yields_no_percentage():
    pop = aggregate([], target_pct=95.0)
    assert pop.denominator == 0
    assert pop.compliance_pct is None
    assert pop.gap_pct is None


def test_all_unknown_is_zero_percent_not_a_pass():
    unknown = [evaluate_asset(asset(asset_id=f"u{i}", assessment_at=None),
                              policy(), now=NOW) for i in range(3)]
    pop = aggregate(unknown, target_pct=95.0)
    assert pop.compliance_pct == 0.0
    assert pop.gap_pct == -95.0


def test_patch_counts_are_never_blended_into_the_percentage():
    """Two compliant servers with many findings still give 100% compliance."""
    evaluations = [
        evaluate_asset(asset(asset_id="a", severity_counts={"moderate": 40}),
                       policy(), now=NOW),
        evaluate_asset(asset(asset_id="b", severity_counts={"low": 10}),
                       policy(), now=NOW),
    ]
    pop = aggregate(evaluations, target_pct=95.0)
    assert pop.compliance_pct == 100.0
    assert pop.total_findings == 50
    assert pop.critical_findings == 0


def test_group_breakdown_reports_unknown_for_missing_values():
    evaluations = [evaluate_asset(asset(), policy(), now=NOW)]
    assets = {"a1": asset(environment="")}
    rows = group_breakdown(evaluations, assets, "environment")
    assert rows[0]["group"] == "UNKNOWN"


def test_group_breakdown_rejects_unknown_dimension():
    with pytest.raises(ValueError):
        group_breakdown([], {}, "hostname")


def test_group_breakdown_totals_match_population():
    evaluations, assets = [], {}
    for index in range(6):
        key = f"a{index}"
        item = asset(asset_id=key, environment=("Production" if index % 2 else "DMZ"))
        assets[key] = item
        evaluations.append(evaluate_asset(item, policy(), now=NOW))
    rows = group_breakdown(evaluations, assets, "environment")
    assert sum(row["total_servers"] for row in rows) == 6


def test_severity_breakdown_covers_every_severity():
    evaluations = [evaluate_asset(
        asset(severity_counts={"critical": 2, "low": 1}), policy(), now=NOW)]
    rows = {row["severity"]: row["count"] for row in severity_breakdown(evaluations)}
    assert rows == {"critical": 2, "important": 0, "moderate": 0,
                    "low": 1, "unknown": 0}


# ------------------------------------------------------ remediation due
def test_remediation_due_uses_policy_days():
    released = datetime(2026, 10, 1, tzinfo=UTC)
    due = remediation_due(DEFAULT_POLICY, asset(), "critical", released)
    assert due == released + timedelta(days=7)


def test_remediation_due_is_none_without_a_release_date():
    assert remediation_due(DEFAULT_POLICY, asset(), "critical", None) is None


def test_remediation_due_is_none_for_unconfigured_severity():
    configured = policy(default_rule={"blocking_severities": [], "remediation_days": {}})
    assert remediation_due(configured, asset(), "critical",
                           datetime(2026, 10, 1, tzinfo=UTC)) is None


# ------------------------------------------------------------ periods
@pytest.mark.parametrize(
    ("kwargs", "start", "end"),
    [
        ({"reporting_month": "2026-10"},
         datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)),
        ({"reporting_month": "2026-12"},
         datetime(2026, 12, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)),
        ({"quarter": "2026-Q4"},
         datetime(2026, 10, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)),
        ({"quarter": "2026 Q1"},
         datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC)),
        ({"quarter": "2026-q2"},
         datetime(2026, 4, 1, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC)),
        ({}, datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)),
    ],
)
def test_period_bounds(kwargs, start, end):
    assert period_bounds(**kwargs, now=NOW) == (start, end)


def test_period_end_is_exclusive():
    start, end = period_bounds(reporting_month="2026-10", now=NOW)
    assert end > start
    assert (end - start).days == 31


@pytest.mark.parametrize("value", ["2026-13", "2026/10", "October", ""])
def test_invalid_month_is_rejected(value):
    if value == "":
        # Empty means "current month", not an error.
        assert period_bounds(reporting_month=None, now=NOW)[0].month == 10
        return
    with pytest.raises(ValueError):
        period_bounds(reporting_month=value, now=NOW)


@pytest.mark.parametrize("value", ["2026-Q5", "2026-Q0", "2026-QX"])
def test_invalid_quarter_is_rejected(value):
    with pytest.raises(ValueError):
        period_bounds(quarter=value, now=NOW)
