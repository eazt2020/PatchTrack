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

import calendar
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

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
    exclusion_reason: str = ""
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

    @property
    def required_compliant(self) -> int | None:
        """Servers that must be compliant to reach the target exactly."""
        if self.target_pct is None:
            return None
        return math.ceil(self.target_pct / 100 * self.denominator)

    @property
    def gap_servers(self) -> int | None:
        """How many more servers must be compliant to meet the target.

        ``None`` when no target is configured or the population is empty —
        a gap of zero would claim success against nothing.
        """
        required = self.required_compliant
        if required is None or self.denominator == 0:
            return None
        return max(0, required - self.compliant)

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
            "gap_servers": self.gap_servers,
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
    tz: str = "UTC",
) -> tuple[datetime, datetime]:
    """Resolve a reporting period to an inclusive start and exclusive end."""
    zone = ZoneInfo(tz)
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
        start = datetime(year, 3 * (quarter_num - 1) + 1, 1, tzinfo=zone)
        month_after = start.month + 3
        end = (
            datetime(year + 1, month_after - 12, 1, tzinfo=zone)
            if month_after > 12
            else datetime(year, month_after, 1, tzinfo=zone)
        )
        return start, end
    if reporting_month:
        try:
            start = datetime.strptime(reporting_month, "%Y-%m").replace(tzinfo=zone)
        except ValueError as exc:
            raise ValueError("reporting_month must look like 2026-10") from exc
        end = (
            datetime(start.year + 1, 1, 1, tzinfo=zone)
            if start.month == 12
            else datetime(start.year, start.month + 1, 1, tzinfo=zone)
        )
        return start, end
    local = now.astimezone(zone)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        datetime(start.year + 1, 1, 1, tzinfo=zone)
        if start.month == 12
        else datetime(start.year, start.month + 1, 1, tzinfo=zone)
    )
    return start, end


# A dashboard period key names one calendar month, quarter or year. The key is
# what the API and the browser store: it is unambiguous, stable and readable in
# a URL, unlike a pair of dates that could be typed inconsistently.
PERIOD_KEY_PATTERN = re.compile(
    r"^(?P<year>\d{4})(?:-(?P<month>\d{2})|-Q(?P<quarter>[1-4]))?$"
)


def parse_period_key(value: str) -> tuple[str, date, date]:
    """Resolve a period key to (kind, first day, last day), both inclusive.

    Accepted forms are ``2026-10`` (month), ``2026-Q4`` (quarter) and ``2026``
    (year). Anything else is rejected rather than guessed at: a dashboard that
    silently interpreted ``2026-10`` as a quarter would report the wrong
    period.
    """
    match = PERIOD_KEY_PATTERN.match((value or "").strip())
    if match is None:
        raise ValueError("period must look like 2026-10, 2026-Q4 or 2026")
    year = int(match.group("year"))
    month = match.group("month")
    quarter = match.group("quarter")
    try:
        if month is not None:
            number = int(month)
            if number not in range(1, 13):
                raise ValueError
            start = date(year, number, 1)
            end = date(year, number, calendar.monthrange(year, number)[1])
            return "month", start, end
        if quarter is not None:
            start_month = 3 * int(quarter) - 2
            end_month = start_month + 2
            return (
                "quarter",
                date(year, start_month, 1),
                date(year, end_month, calendar.monthrange(year, end_month)[1]),
            )
        return "year", date(year, 1, 1), date(year, 12, 31)
    except ValueError as exc:
        raise ValueError(
            "period must look like 2026-10, 2026-Q4 or 2026"
        ) from exc


def period_key(kind: str, start: date) -> str:
    """The canonical key for the calendar period beginning at ``start``."""
    if kind == "month":
        return f"{start.year:04d}-{start.month:02d}"
    if kind == "quarter":
        return f"{start.year:04d}-Q{(start.month - 1) // 3 + 1}"
    if kind == "year":
        return f"{start.year:04d}"
    raise ValueError(f"unsupported period kind {kind}")


def period_label(kind: str, start: date) -> str:
    """How a period is named to a reader: October 2026, Q4 2026, 2026."""
    if kind == "month":
        return start.strftime("%B %Y")
    if kind == "quarter":
        return f"Q{(start.month - 1) // 3 + 1} {start.year}"
    if kind == "year":
        return str(start.year)
    raise ValueError(f"unsupported period kind {kind}")


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


# --------------------------------------------------------------------------- insights
# These are pure functions over already-loaded rows so the dashboard, the tests
# and any future export all answer a question the same way.

def finding_sla(findings: Iterable[Mapping[str, Any]],
                *, today: date | None = None,
                due_soon_days: int = 7) -> dict[str, Any]:
    """Bucket current findings by their recorded remediation due date.

    ``overdue`` is decided by the stored ``remediation_due`` — the date the
    policy produced when the finding was ingested — never by re-deriving it
    from ``released_at``. A finding with no due date is ``undated``: it is
    neither on track nor overdue, and it is reported so it cannot hide.
    """
    today = today or datetime.now(UTC).date()
    rows = list(findings)
    overdue = due_soon = on_track = undated = 0
    oldest_days = 0
    for row in rows:
        due = row.get("remediation_due")
        if isinstance(due, datetime):
            due = due.date()
        if due is None:
            undated += 1
            continue
        days = (due - today).days
        if days < 0:
            overdue += 1
            oldest_days = max(oldest_days, -days)
        elif days <= due_soon_days:
            due_soon += 1
        else:
            on_track += 1
    return {
        "total": len(rows),
        "overdue": overdue,
        "due_soon": due_soon,
        "on_track": on_track,
        "undated": undated,
        "due_soon_days": due_soon_days,
        "oldest_days_overdue": oldest_days,
    }


def concentration(evaluations: Iterable[Evaluation]) -> dict[str, Any]:
    """How unevenly outstanding patches sit across the fleet.

    Returns a cumulative curve (server share against patch share) plus the
    headline shares. Servers with no outstanding patches are part of the
    population but add nothing to the curve, so the curve starts at the first
    affected server; that boundary is stated in the response.
    """
    counts = sorted(
        (e.total_patches for e in evaluations), reverse=True)
    population = len(counts)
    affected = [n for n in counts if n > 0]
    total = sum(affected)
    if not affected or total == 0:
        return {
            "population": population, "affected": 0, "total_patches": 0,
            "top_10_pct_share": 0.0, "top_1pct_share": 0.0,
            "top_10_servers_share": 0.0, "top_30_servers_share": 0.0,
            "top_1pct_servers": 0, "top_10pct_servers": 0, "points": [],
        }

    def count_for(share: float) -> int:
        return max(1, math.ceil(len(affected) * share))

    def patch_share(n: int) -> float:
        return round(sum(affected[:n]) / total * 100, 1)

    # The curve records deciles of the affected population. Each point is the
    # share of all outstanding patches held by that share of affected servers.
    points = []
    for step in range(1, 11):
        n = max(1, math.ceil(len(affected) * step / 10))
        points.append({
            "server_share": round(n / len(affected) * 100, 1),
            "patch_share": patch_share(n),
            "servers": n,
        })
    return {
        "population": population,
        "affected": len(affected),
        "total_patches": total,
        "top_1pct_share": patch_share(count_for(0.01)),
        "top_10_pct_share": patch_share(count_for(0.10)),
        "top_10_servers_share": patch_share(min(10, len(affected))),
        "top_30_servers_share": patch_share(min(30, len(affected))),
        "top_1pct_servers": count_for(0.01),
        "top_10pct_servers": count_for(0.10),
        "points": points,
    }


def excluded_breakdown(
    evaluations: Iterable[Evaluation], assets: Mapping[str, AssetInput],
) -> dict[str, Any]:
    """Why assets are outside the compliance denominator.

    The engine's own reason is used first because it names the rule that
    actually excluded the asset; the inventory's recorded exclusion reason is
    the fallback when the asset was marked out of scope upstream.
    """
    buckets: dict[str, int] = {}
    total = 0
    for evaluation in evaluations:
        if evaluation.state != "excluded":
            continue
        asset = assets.get(evaluation.asset_id)
        reason = (
            (asset.exclusion_reason if asset else "")
            or evaluation.reason
            or "Excluded without a recorded reason"
        )
        buckets[reason] = buckets.get(reason, 0) + 1
        total += 1
    rows = [
        {"reason": reason, "count": count}
        for reason, count in sorted(
            buckets.items(), key=lambda item: (-item[1], item[0]))
    ]
    return {"total": total, "reasons": rows}


def movers(current: Iterable[Mapping[str, Any]],
           previous: Iterable[Mapping[str, Any]],
           *, limit: int = 6) -> list[dict[str, Any]]:
    """Percentage-point change per group between two recorded panels.

    Only groups present in both observations are compared: a new group has no
    previous rate, and treating it as zero would invent a collapse.
    """
    def rate(row: Mapping[str, Any]) -> float | None:
        in_scope = int(row.get("in_scope") or 0)
        compliant = int(row.get("compliant") or 0)
        if in_scope == 0:
            return None
        return round(compliant / in_scope * 100, 2)

    before = {str(row.get("group")): row for row in previous}
    rows = []
    for row in current:
        key = str(row.get("group"))
        old = before.get(key)
        if old is None:
            continue
        now_rate, old_rate = rate(row), rate(old)
        if now_rate is None or old_rate is None:
            continue
        rows.append({
            "group": key,
            "compliance_pct": now_rate,
            "previous_pct": old_rate,
            "change_pp": round(now_rate - old_rate, 2),
            "servers": int(row.get("in_scope") or 0),
        })
    rows.sort(key=lambda r: (-abs(r["change_pp"]), r["group"]))
    return rows[:limit]
