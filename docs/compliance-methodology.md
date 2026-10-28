# Compliance methodology

This document defines exactly how every published number is produced. If a
figure cannot be derived from this document and the policy version recorded on
the snapshot, it is a defect.

## 1. The population

An asset is in the population when **all** of the following hold:

- `inventory.assets.in_scope` is true;
- `lifecycle_status` is not `retired` and not `decommissioned`;
- `environment` is not listed in the policy's `excluded_environments`.

Everything else is **Excluded**. Excluded assets are counted and displayed, but
are not in the denominator. Exclusion always has a reason, and the reason is
stored on the evaluation and shown in the drill-down.

An asset whose lifecycle is `unknown` (a value AssetExplorer returned that
PatchTrack could not map) stays **in** the population. Unknown lifecycle is
not a licence to exclude.

`environment` here and in every scope rule is the **effective** value from
`inventory.asset_effective`: a local override set by an operator counts exactly
as the placement does, and a value renamed in Settings resolves to its current
name. Saving an override re-evaluates compliance, and so does renaming a value
that servers hold, because either can move a server between scope rules; the
next snapshot records the effective placement under the current name. Snapshots
already captured are not rewritten.

## 2. The five states

Evaluated in this order. The first rule that matches determines the state.

| # | Condition | State | In denominator |
|---|-----------|-------|----------------|
| 1 | Not in scope, or retired/decommissioned lifecycle, or excluded environment | **Excluded** | No |
| 2 | No assessment, or no assessment timestamp, or the newest assessment failed | **Unknown** | Yes |
| 3 | `now − assessed_at > freshness_hours` (exact, unrounded) | **Stale** | Yes |
| 4 | Outstanding blocking findings > `max_blocking_findings` | **Non-compliant** | Yes |
| 5 | Otherwise | **Compliant** | Yes |

Two details matter in an audit:

- **Rule 2 before rule 3.** A failed assessment is Unknown, not Stale. Stale
  means "we have data but it is old"; Unknown means "we do not have usable
  data". They are reported separately because the operational response differs.
- **Comparison is on the exact age.** The stored `assessment_age_hours` is
  rounded to two decimals *for display*. The freshness test uses the
  unrounded duration, so an assessment 48 hours and 20 seconds old is Stale at
  a 48-hour threshold. The unit test
  `test_assessment_just_past_freshness_is_stale` pins this.

### Why unknown and stale are in the denominator

If a server with no patch data were removed from the denominator, an estate
where every collection job had stopped would report 100% compliance. That is
the exact failure mode this product exists to prevent. A server we cannot
assess is a server we cannot claim is patched.

## 3. The percentage

```
compliance_pct = compliant / (compliant + non_compliant + unknown + stale) × 100
```

Rounded to two decimal places. When the denominator is zero the value is
`null`, and every consumer — API, UI, PDF, Grafana — renders "n/a" rather than
`0%` or `100%`. An empty estate is not a perfect estate.

`gap_pct = compliance_pct − target_pct`, also `null` when either input is
`null`.

## 4. Patch-level figures are separate

`total_findings` and `critical_findings` are counts of outstanding findings
across the population. They are **never** combined with the server percentage
into a composite score, and there is no "patch compliance %" that mixes the
two. A server with 40 outstanding moderate patches and no critical ones is
Compliant under the default blocking set while contributing 40 findings; both
facts are true and both are reported.

## 5. Policy

The active policy is a JSON definition stored in an immutable version row.
Evaluations and snapshots record `policy_version_id`, so a result captured
under a previous policy remains explainable after the policy changes.

Default policy:

```json
{
  "target_pct": 95.0,
  "quarterly_target_pct": 95.0,
  "freshness_hours": 48,
  "require_assessment": true,
  "max_blocking_findings": 0,
  "default_rule": {
    "blocking_severities": ["critical", "important"],
    "remediation_days": {
      "critical": 7, "important": 30, "moderate": 90, "low": 180
    }
  },
  "scope_rules": [],
  "excluded_environments": []
}
```

### Scope rules

`scope_rules` is an ordered list. The first rule whose `match` object matches
the asset wins; all other fields of that rule override `default_rule`.
Matching is case-insensitive on `os_family`, `environment`, `business_group`
and `application_group`.

Example — a tighter rule for payment systems in production:

```json
{
  "name": "Payments production",
  "target_pct": 98.0,
  "freshness_hours": 24,
  "default_rule": {
    "blocking_severities": ["critical", "important", "moderate"],
    "remediation_days": {"critical": 3, "important": 14, "moderate": 60}
  },
  "scope_rules": [
    {"match": {"business_group": "Payments", "environment": "Production"}}
  ],
  "excluded_environments": []
}
```

Note that `target_pct` on a scope rule affects the target shown for that
group's report row; the headline percentage is always computed for the whole
population with the population-level target.

### Changing policy

1. `POST /policies/{id}/versions` with the new definition and a change note.
2. `POST /policies/{id}/activate?version_id=...`.
3. Activation re-evaluates immediately and is audited.

Historical snapshots keep the version they were captured with. Changing policy
does not rewrite history — a management report for a past month reproduces the
figures captured under the policy in force then.

## 6. Freshness and its operational meaning

`freshness_hours` is the maximum age of an assessment that may still be used
as evidence. It is not a collection interval; it is the point beyond which the
organisation stops being able to assert anything about that server's patch
state.

Practical guidance:

| Source cadence | Suggested `freshness_hours` |
|----------------|----------------------------|
| Satellite synced hourly | 6 |
| SCCM synced daily | 48 |
| Solaris file drop weekly | 192 |

A threshold shorter than the collection interval guarantees that servers
oscillate between Stale and Compliant — a configuration error, visible as a
saw-tooth trend line.

## 7. Snapshots

A snapshot freezes: totals by state, the percentage and target, finding counts,
per-dimension group rows, the policy version, the methodology text, the filter
set and the capture time.

- The daily job captures one snapshot per calendar day (UTC). The unique index
  `(period_start, (captured_at AT TIME ZONE 'UTC')::date) WHERE scope='daily'`
  makes a retry a no-op.
- Monthly and quarterly snapshots are captured on demand or by the reporting
  schedule, with `period_start` set to the first day of the period.
- `compliance_pct` is computed at capture. It is never recomputed later.
- Group rows are recorded for `os_family`, `os_version`, `environment`,
  `business_group`, `application_group` and `severity`. The dashboard's
  historical view reads the latest daily snapshot inside the chosen month,
  quarter or year, so the figures it shows are exactly the captured ones. A
  snapshot captured before a dimension was recorded (older deployments have no
  `os_family` rows) leaves that panel empty and says so; the current inventory
  is never used to fill the gap.

## 8. Reproducing a reported figure

Given a report or a snapshot from any date:

1. Read `snapshot.policy_version_id`, `snapshot.captured_at` and
   `snapshot.methodology`.
2. Read the group rows from `compliance.snapshot_groups`.
3. The percentage is `compliant_servers / (total_servers − excluded_servers)`.

If a later re-evaluation produces a different number, that is expected — the
estate has changed. The snapshot is the record of what was reported, and it is
never updated.

## 9. What the numbers do not mean

- They do not assert that no unpatched vulnerabilities exist beyond the
  advisories the source systems report.
- They do not assert compliance with any regulatory framework. Mapping to a
  framework (PCI DSS, APRA CPS 234, ISO 27001) is a separate exercise.
- They are not a measure of risk. A server with no critical patches and one
  unassessed is Unknown, which may be riskier than a server with five
  important patches.
