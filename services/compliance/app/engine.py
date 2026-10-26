"""Compliance calculation engine.

Deliberate rules, applied identically by the dashboard, API and reports:

1. Population = assets with ``in_scope`` true and an active lifecycle status.
   Retired/decommissioned assets are reported as ``excluded`` and stay out of
   the denominator; their history is retained.
2. An asset with no usable assessment is ``unknown``. Unknown is NEVER
   compliant and stays in the denominator.
3. An asset whose newest assessment is older than the policy freshness
   threshold is ``stale``. Stale is NOT compliant and stays in the
   denominator.
4. Otherwise, the asset is ``non_compliant`` when it has more than
   ``max_blocking_findings`` outstanding findings whose severity is in the
   policy's blocking set for its scope; otherwise it is ``compliant``.
5. ``compliance_pct = compliant / (compliant + non_compliant + unknown + stale)``.
   An empty denominator yields ``None``, never ``0`` or ``100``.

Server-level percentage and patch-level counts are separate values and are
never combined into one ambiguous figure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_POLICY: dict[str, Any] = {
    "target_pct": 95.0,
    "quarterly_target_pct": 95.0,
    "freshness_hours": 48,
    "require_assessment": True,
    "max_blocking_findings": 0,
    "default_rule": {
        "blocking_severities": ["critical", "important"],
        "remediation_days": {"critical": 7, "important": 30,
                             "moderate": 90, "low": 180},
    },
    "scope_rules": [],
    "excluded_environments": [],
}

STATES = ("compliant", "non_compliant", "unknown", "stale", "excluded")
DIMENSIONS = ("os_family", "os_version", "environment", "business_group",
              "application_group")


@dataclass
class AssetInput:
    asset_id: str
    hostname: str
    os_family: str
    os_version: str = ""
    environment: str = "UNKNOWN"
    business_group: str = "UNKNOWN"
    application_group: str = "UNKNOWN"
    lifecycle_status: str = "active"
    in_scope: bool = True
    patch_source: str = ""
    assessment_at: datetime | None = None
    assessment_status: str | None = None
    severity_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class Evaluation:
    asset_id: str
    state: str
    reason: str
    severity_counts: dict[str, int]
    assessment_age_hours: float | None

    @property
    def total_patches(self) -> int:
        return sum(self.severity_counts.values())


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def rule_for(policy: dict[str, Any], asset: AssetInput) -> dict[str, Any]:
    """First matching scope rule wins; unmatched assets use default_rule."""
    for rule in policy.get("scope_rules", []):
        match = rule.get("match") or {}
        if not match:
            continue
        if all(
            str(getattr(asset, key, "")).lower() == str(expected).lower()
            for key, expected in match.items()
            if key in ("os_family", "environment", "business_group",
                       "application_group")
        ):
            return {**policy.get("default_rule", {}), **rule}
    return policy.get("default_rule", {})


def evaluate_asset(
    asset: AssetInput,
    policy: dict[str, Any],
    *,
    now: datetime | None = None,
) -> Evaluation:
    now = now or datetime.now(UTC)
    counts = {k: int(v) for k, v in (asset.severity_counts or {}).items()}
    assessed = _aware(asset.assessment_at)
    # Keep the exact age for the freshness comparison. The rounded value is
    # only for reporting: comparing the rounded figure against the threshold
    # would let an assessment up to half a minute past the boundary count as
    # fresh, which is not a defensible answer for an audit.
    exact_age_hours = (
        (now - assessed).total_seconds() / 3600 if assessed else None
    )
    age_hours = round(exact_age_hours, 2) if exact_age_hours is not None else None

    if not asset.in_scope:
        return Evaluation(asset.asset_id, "excluded",
                          "Asset is outside the compliance scope", counts, age_hours)
    if asset.lifecycle_status in ("retired", "decommissioned"):
        return Evaluation(
            asset.asset_id, "excluded",
            f"Asset lifecycle is {asset.lifecycle_status}", counts, age_hours,
        )
    if asset.environment in policy.get("excluded_environments", []):
        return Evaluation(
            asset.asset_id, "excluded",
            f"Environment {asset.environment} is excluded by policy",
            counts, age_hours,
        )

    if assessed is None or asset.assessment_status is None:
        return Evaluation(
            asset.asset_id, "unknown",
            "No patch assessment is available from the source system", counts, None,
        )
    if asset.assessment_status == "failed":
        return Evaluation(
            asset.asset_id, "unknown",
            "The most recent patch assessment failed", counts, age_hours,
        )
    freshness = float(policy.get("freshness_hours", 48))
    if exact_age_hours is not None and exact_age_hours > freshness:
        return Evaluation(
            asset.asset_id, "stale",
            f"Assessment is {exact_age_hours:.1f}h old; freshness threshold is "
            f"{freshness:.0f}h", counts, age_hours,
        )

    rule = rule_for(policy, asset)
    blocking = set(rule.get("blocking_severities", []))
    blocking_count = sum(counts.get(sev, 0) for sev in blocking)
    threshold = int(policy.get("max_blocking_findings", 0))
    if blocking_count > threshold:
        return Evaluation(
            asset.asset_id, "non_compliant",
            f"{blocking_count} outstanding blocking finding(s) "
            f"({', '.join(sorted(blocking)) or 'none configured'})",
            counts, age_hours,
        )
    return Evaluation(
        asset.asset_id, "compliant", "No outstanding blocking findings",
        counts, age_hours,
    )


@dataclass
class Population:
    total: int = 0
    compliant: int = 0
    non_compliant: int = 0
    unknown: int = 0
    stale: int = 0
    excluded: int = 0
    critical_findings: int = 0
    total_findings: int = 0
    target_pct: float | None = None

    @property
    def denominator(self) -> int:
        return self.compliant + self.non_compliant + self.unknown + self.stale

    @property
    def compliance_pct(self) -> float | None:
        if self.denominator == 0:
            return None
        return round(self.compliant / self.denominator * 100, 2)

    @property
    def gap_pct(self) -> float | None:
        if self.compliance_pct is None or self.target_pct is None:
            return None
        return round(self.compliance_pct - self.target_pct, 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_servers": self.total,
            "compliant": self.compliant,
            "non_compliant": self.non_compliant,
            "unknown": self.unknown,
            "stale": self.stale,
            "excluded": self.excluded,
            "in_scope": self.denominator,
            "compliance_pct": self.compliance_pct,
            "target_pct": self.target_pct,
            "gap_pct": self.gap_pct,
            "critical_findings": self.critical_findings,
            "total_findings": self.total_findings,
        }


def aggregate(evaluations: list[Evaluation], *, target_pct: float | None) -> Population:
    pop = Population(target_pct=target_pct)
    for evaluation in evaluations:
        pop.total += 1
        setattr(pop, evaluation.state, getattr(pop, evaluation.state) + 1)
        pop.critical_findings += evaluation.severity_counts.get("critical", 0)
        pop.total_findings += evaluation.total_patches
    return pop


def group_breakdown(
    evaluations: list[Evaluation],
    assets: dict[str, AssetInput],
    dimension: str,
) -> list[dict[str, Any]]:
    if dimension not in DIMENSIONS:
        raise ValueError(f"unsupported dimension {dimension}")
    buckets: dict[str, list[Evaluation]] = {}
    for evaluation in evaluations:
        asset = assets[evaluation.asset_id]
        key = getattr(asset, dimension, None) or "UNKNOWN"
        buckets.setdefault(str(key), []).append(evaluation)
    rows = []
    for key, group in buckets.items():
        pop = aggregate(group, target_pct=None)
        rows.append({"group": key, **pop.as_dict()})
    rows.sort(key=lambda r: (-r["total_servers"], r["group"]))
    return rows


def severity_breakdown(evaluations: list[Evaluation]) -> list[dict[str, Any]]:
    totals = {sev: 0 for sev in
              ("critical", "important", "moderate", "low", "unknown")}
    for evaluation in evaluations:
        for sev, count in evaluation.severity_counts.items():
            totals[sev] = totals.get(sev, 0) + count
    return [{"severity": sev, "count": n} for sev, n in totals.items()]


def period_bounds(
    reporting_month: str | None = None,
    quarter: str | None = None,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Resolve a reporting period to an inclusive start and exclusive end."""
    now = now or datetime.now(UTC)
    if quarter:
        text_value = quarter.upper().replace("-", " ")
        parts = text_value.split()
        year = next((int(p) for p in parts if p.isdigit() and len(p) == 4), now.year)
        quarter_num = next(
            (int(p.replace("Q", "")) for p in parts if p.startswith("Q")), None
        )
        if quarter_num not in (1, 2, 3, 4):
            raise ValueError("quarter must look like 2026-Q3")
        start = datetime(year, 3 * (quarter_num - 1) + 1, 1, tzinfo=UTC)
        month_after = start.month + 3
        end = (
            datetime(year + 1, month_after - 12, 1, tzinfo=UTC)
            if month_after > 12
            else datetime(year, month_after, 1, tzinfo=UTC)
        )
        return start, end
    if reporting_month:
        try:
            start = datetime.strptime(reporting_month, "%Y-%m").replace(tzinfo=UTC)
        except ValueError as exc:
            raise ValueError("reporting_month must look like 2026-10") from exc
        end = (
            datetime(start.year + 1, 1, 1, tzinfo=UTC)
            if start.month == 12
            else datetime(start.year, start.month + 1, 1, tzinfo=UTC)
        )
        return start, end
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        datetime(start.year + 1, 1, 1, tzinfo=UTC)
        if start.month == 12
        else datetime(start.year, start.month + 1, 1, tzinfo=UTC)
    )
    return start, end


def remediation_due(
    policy: dict[str, Any], asset: AssetInput,
    severity: str, released: datetime | None,
) -> datetime | None:
    if released is None:
        return None
    rule = rule_for(policy, asset)
    days = rule.get("remediation_days", {}).get(severity)
    if days is None:
        return None
    return released + timedelta(days=int(days))
