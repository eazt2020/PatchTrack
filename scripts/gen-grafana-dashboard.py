#!/usr/bin/env python3
"""Generate deploy/grafana/dashboards/patchtrack-overview.json.

The Grafana dashboard reads the same tables the application writes, so the
numbers agree by construction. Keeping the panel definitions in a small
generator avoids hand-editing a 700-line JSON blob and makes the SQL reviewable.

Run after changing any panel:

    python3 scripts/gen-grafana-dashboard.py

The generated JSON is committed; Grafana is provisioned from it at start-up
and the file is read-only inside the container.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "deploy" / "grafana" / "dashboards" / "patchtrack-overview.json"

DATASOURCE = {"type": "postgres", "uid": "patchtrack-postgres"}

# Status palette — the same tokens the application UI uses. Status colours are
# reserved: they never encode anything other than compliance state.
GREEN = "#0ca30c"
RED = "#d03b3b"
AMBER = "#fab219"
GREY = "#7f8b98"
ORANGE = "#ec835a"
BLUE = "#3987e5"

# Scope filters.
#
# Each variable's "All" option is given an explicit ALL_SENTINEL value. Without
# one, Grafana expands "All" to the comma-joined list of every option and the
# predicate becomes "'rhel','solaris','windows' = ALL" — a syntax error. With a
# sentinel the comparison is a plain string equality and the filter is skipped.
# Plain SQL rather than the $__conditionalAll macro keeps the behaviour
# checkable with psql.
ALL_SENTINEL = "__all__"


def scope_filters(*columns: str, indent: str = "      ") -> str:
    return "\n".join(
        f"{indent}AND ('${c}' = '{ALL_SENTINEL}' OR a.{c} = '${c}')"
        for c in columns)


def stat_sql(expression: str, *, include_excluded: bool = False) -> str:
    """A single headline number from the current evaluated population.

    ``include_excluded`` drops the scope and lifecycle filters. Excluded assets
    have ``in_scope = false`` or a retired/decommissioned lifecycle, so applying
    either filter would make the exclusion tile report zero by construction.
    """
    scope = "1 = 1" if include_excluded else (
        "a.in_scope AND a.lifecycle_status = 'active'")
    return f"""SELECT {expression} AS value
FROM (
    SELECT
        count(*) FILTER (WHERE state = 'compliant') AS compliant,
        count(*) FILTER (WHERE state = 'non_compliant') AS non_compliant,
        count(*) FILTER (WHERE state IN ('compliant','non_compliant','unknown','stale')) AS denominator,
        count(*) FILTER (WHERE state IN ('unknown','stale')) AS unknown_stale,
        count(*) FILTER (WHERE state = 'excluded') AS excluded
    FROM compliance.asset_compliance c
    JOIN inventory.asset_effective a ON a.id = c.asset_id
    WHERE {scope}
{scope_filters("os_family", "environment", "business_group")}
) s"""


def panel_sql(sql: str) -> list[dict]:
    return [{
        "refId": "A",
        "datasource": DATASOURCE,
        "rawSql": sql,
        "format": "table",
        "editorMode": "code",
        "rawQuery": True,
    }]


def stat_panel(
    panel_id: int, title: str, sql: str, x: int, y: int, w: int, h: int,
    *, unit: str = "short", color: str = BLUE, decimals: int = 0,
    description: str = "",
) -> dict:
    return {
        "id": panel_id,
        "type": "stat",
        "title": title,
        "description": description,
        "datasource": DATASOURCE,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": panel_sql(sql),
        "options": {
            "reduceOptions": {
                "values": False, "calcs": ["lastNotNull"], "fields": "",
            },
            "colorMode": "value",
            "graphMode": "none",
            "textMode": "auto",
            "orientation": "auto",
        },
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "decimals": decimals,
                "color": {"mode": "fixed", "fixedColor": color},
                "thresholds": {"mode": "absolute", "steps": [
                    {"color": color, "value": None},
                ]},
            },
            "overrides": [],
        },
    }


def breakdown_sql(column: str, label: str, limit: int = 12,
                  label_expr: str | None = None) -> str:
    expr = label_expr or f"a.{column}"
    return f"""
SELECT
    COALESCE(NULLIF({expr}, ''), 'UNKNOWN') AS "{label}",
    count(*) FILTER (WHERE c.state = 'compliant')     AS "Compliant",
    count(*) FILTER (WHERE c.state = 'non_compliant') AS "Non-compliant",
    count(*) FILTER (WHERE c.state IN ('unknown','stale')) AS "Unknown / stale"
FROM compliance.asset_compliance c
JOIN inventory.asset_effective a ON a.id = c.asset_id
WHERE a.in_scope AND a.lifecycle_status = 'active'
{scope_filters("os_family", "environment", "business_group", indent="  ")}
GROUP BY 1
ORDER BY 2 + 3 + 4 DESC
LIMIT {limit}
"""


def barchart_panel(
    panel_id: int, title: str, sql: str, x_field: str, x: int, y: int, w: int,
    h: int, *, horizontal: bool = False, upright_labels: bool = False,
    description: str = "",
) -> dict:
    return {
        "id": panel_id,
        "type": "barchart",
        "title": title,
        "description": description,
        "datasource": DATASOURCE,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": panel_sql(sql),
        "options": {
            "xField": x_field,
            "orientation": "horizontal" if horizontal else "vertical",
            "stacking": "normal",
            "showValue": "auto",
            "legend": {"displayMode": "list", "placement": "bottom",
                       "showLegend": True},
            "tooltip": {"mode": "single", "sort": "none"},
            "xTickLabelRotation": 0 if (horizontal or upright_labels) else -30,
            "xTickLabelMaxLength": 18,
            "barWidth": 0.62,
            "groupWidth": 0.7,
            "fullHighlight": False,
        },
        "fieldConfig": {
            "defaults": {
                "custom": {
                    "stacking": {"mode": "normal", "group": "A"},
                    "lineWidth": 0,
                    "fillOpacity": 100,
                    "gradientMode": "none",
                    "axisPlacement": "auto",
                    "axisLabel": "",
                    "showPoints": "never",
                },
                "color": {"mode": "fixed", "fixedColor": BLUE},
                "unit": "short",
                "min": 0,
            },
            "overrides": [
                {"matcher": {"id": "byName", "options": "Compliant"},
                 "properties": [{"id": "color", "value": {
                     "mode": "fixed", "fixedColor": GREEN}}]},
                {"matcher": {"id": "byName", "options": "Non-compliant"},
                 "properties": [{"id": "color", "value": {
                     "mode": "fixed", "fixedColor": RED}}]},
                {"matcher": {"id": "byName", "options": "Unknown / stale"},
                 "properties": [{"id": "color", "value": {
                     "mode": "fixed", "fixedColor": AMBER}}]},
            ],
        },
        # Category labels are text even when they look numeric ("9.5", "2022").
        # Without this Grafana formats the axis and renders them as 9.50 / 2.02 K.
        "transformations": [{
            "id": "convertFieldType",
            "options": {"conversions": [{
                "destinationType": "string", "targetField": x_field,
            }], "fields": {}},
        }],
    }


def donut_panel(panel_id: int, x: int, y: int, w: int, h: int) -> dict:
    sql = f"""
SELECT
    count(*) FILTER (WHERE state = 'compliant')     AS "Compliant",
    count(*) FILTER (WHERE state = 'non_compliant') AS "Non-compliant",
    count(*) FILTER (WHERE state IN ('unknown','stale')) AS "Unknown / stale"
FROM compliance.asset_compliance c
JOIN inventory.asset_effective a ON a.id = c.asset_id
WHERE a.in_scope AND a.lifecycle_status = 'active'
{scope_filters("os_family", "environment", "business_group", indent="  ")}
"""
    return {
        "id": panel_id,
        "type": "piechart",
        "title": "Patch status distribution",
        "description": "Unknown and stale servers are part of the population, "
                       "never counted as compliant.",
        "datasource": DATASOURCE,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": panel_sql(sql),
        "options": {
            "pieType": "donut",
            "displayLabels": [],
            "legend": {"displayMode": "list", "placement": "right",
                       "showLegend": True, "values": ["value"]},
            "tooltip": {"mode": "single", "sort": "none"},
            "reduceOptions": {"values": True, "calcs": ["lastNotNull"], "fields": ""},
        },
        "fieldConfig": {
            "defaults": {
                "unit": "short",
                "custom": {"hideFrom": {"tooltip": False, "viz": False, "legend": False}},
            },
            "overrides": [
                {"matcher": {"id": "byName", "options": "Compliant"},
                 "properties": [{"id": "color", "value": {
                     "mode": "fixed", "fixedColor": GREEN}}]},
                {"matcher": {"id": "byName", "options": "Non-compliant"},
                 "properties": [{"id": "color", "value": {
                     "mode": "fixed", "fixedColor": RED}}]},
                {"matcher": {"id": "byName", "options": "Unknown / stale"},
                 "properties": [{"id": "color", "value": {
                     "mode": "fixed", "fixedColor": AMBER}}]},
            ],
        },
        "transformations": [
            {"id": "reduce", "options": {
                "reducers": ["lastNotNull"],
                "mode": "seriesToRows",
            }},
        ],
    }


def timeseries_panel(
    panel_id: int, title: str, sql: str, x: int, y: int, w: int, h: int,
    *, description: str = "",
) -> dict:
    return {
        "id": panel_id,
        "type": "timeseries",
        "title": title,
        "description": description,
        "datasource": DATASOURCE,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": panel_sql(sql),
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom",
                       "showLegend": True, "calcs": ["lastNotNull"]},
            "tooltip": {"mode": "multi", "sort": "none"},
        },
        "fieldConfig": {
            "defaults": {
                "unit": "percent",
                "min": 0, "max": 100,
                "custom": {
                    "drawStyle": "line", "lineWidth": 2,
                    "fillOpacity": 8, "showPoints": "never",
                    "pointSize": 6, "spanNulls": False,
                    "lineInterpolation": "linear",
                    "axisPlacement": "auto",
                    "thresholdsStyle": {"mode": "dashed"},
                },
                "color": {"mode": "fixed", "fixedColor": BLUE},
                "thresholds": {"mode": "absolute", "steps": [
                    {"color": ORANGE, "value": None},
                ]},
            },
            # The target is drawn as its own dashed series rather than as a
            # threshold: a threshold is a visual guide, while this value is the
            # target recorded on each snapshot and must not move with the
            # current policy.
            "overrides": [{
                "matcher": {"id": "byName", "options": "Target"},
                "properties": [
                    {"id": "color", "value": {"mode": "fixed", "fixedColor": ORANGE}},
                    {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [8, 4]}},
                    {"id": "custom.fillOpacity", "value": 0},
                ],
            }],
        },
    }


def table_panel(panel_id: int, title: str, sql: str, x: int, y: int, w: int,
                h: int, *, description: str = "") -> dict:
    return {
        "id": panel_id,
        "type": "table",
        "title": title,
        "description": description,
        "datasource": DATASOURCE,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": panel_sql(sql),
        "options": {
            "showHeader": True,
            "cellHeight": "sm",
            "footer": {"show": False, "countRows": False, "fields": ""},
            "sortBy": [{"displayName": "Missing patches", "desc": True}],
        },
        "fieldConfig": {
            "defaults": {
                "custom": {"align": "auto", "cellOptions": {"type": "auto"}},
                "unit": "short",
            },
            "overrides": [{
                "matcher": {"id": "byName", "options": "Status"},
                "properties": [{"id": "custom.cellOptions", "value": {
                    "type": "color-text"}},
                    {"id": "mappings", "value": [{
                        "type": "value", "options": {
                            "non_compliant": {"text": "Non-compliant",
                                              "color": RED, "index": 0},
                            "compliant": {"text": "Compliant", "color": GREEN,
                                          "index": 1},
                            "unknown": {"text": "Unknown", "color": AMBER,
                                        "index": 2},
                            "stale": {"text": "Stale", "color": GREY, "index": 3},
                        }}]},
                ],
            }],
        },
    }


MONTHLY_SQL = """
SELECT
    date_trunc('month', period_start)::date AS "time",
    avg(compliance_pct)::numeric(6,2) AS "Compliance",
    avg(target_pct)::numeric(6,2) AS "Target"
FROM compliance.snapshots
WHERE scope = 'daily'
  AND period_start >= (date_trunc('month', now()) - interval '11 months')::date
GROUP BY 1
ORDER BY 1
"""

QUARTERLY_SQL = """
SELECT
    to_char(period_start, 'YYYY "Q"Q') AS "Quarter",
    compliance_pct AS "Compliance",
    target_pct AS "Target"
FROM compliance.snapshots
WHERE scope = 'quarterly'
ORDER BY period_start
"""

SEVERITY_SQL = """
SELECT
    f.severity AS "Severity",
    count(*) AS "Findings"
FROM compliance.findings f
JOIN compliance.asset_latest_assessment la ON la.assessment_id = f.assessment_id
JOIN inventory.asset_effective a ON a.id = f.asset_id
WHERE a.in_scope
GROUP BY 1
ORDER BY CASE f.severity WHEN 'critical' THEN 0 WHEN 'important' THEN 1
         WHEN 'moderate' THEN 2 WHEN 'low' THEN 3 ELSE 4 END
"""

TOP_SQL = f"""
SELECT
    a.hostname              AS "Hostname",
    host(a.ip_address)      AS "IP address",
    a.os_name || ' ' || a.os_version AS "Operating system",
    a.environment           AS "Environment",
    a.business_group        AS "Business service",
    c.state                 AS "Status",
    c.total_patches         AS "Missing patches",
    c.critical_count        AS "Critical",
    to_char(asmt.assessed_at, 'YYYY-MM-DD HH24:MI') AS "Last assessed"
FROM inventory.asset_effective a
JOIN compliance.asset_compliance c ON c.asset_id = a.id
LEFT JOIN compliance.asset_latest_assessment la ON la.asset_id = a.id
LEFT JOIN compliance.assessments asmt ON asmt.id = la.assessment_id
WHERE a.in_scope AND c.state = 'non_compliant'
{scope_filters("os_family", "environment", "business_group", indent="  ")}
ORDER BY c.total_patches DESC NULLS LAST, a.hostname
LIMIT 15
"""

FRESHNESS_SQL = """
SELECT
    i.key AS "Integration",
    r.status AS "Status",
    to_char(r.started_at, 'YYYY-MM-DD HH24:MI') AS "Last attempt",
    to_char(r.finished_at, 'YYYY-MM-DD HH24:MI') AS "Finished",
    r.records_seen AS "Seen",
    r.records_updated AS "Matched",
    r.records_rejected AS "Rejected",
    r.assets_unmatched AS "Unmatched",
    CASE WHEN r.status = 'failed' THEN left(r.error_summary, 80) ELSE '' END AS "Error"
FROM inventory.integrations i
LEFT JOIN LATERAL (
    SELECT * FROM inventory.sync_runs s
    WHERE s.integration_key = i.key
    ORDER BY s.started_at DESC LIMIT 1
) r ON TRUE
ORDER BY i.key
"""


def build() -> dict:
    panels: list[dict] = []
    y = 0
    panels.append(stat_panel(
        1, "Total servers (in scope)", stat_sql("compliant + non_compliant + unknown_stale + excluded"),
        0, y, 4, 4, color=BLUE,
        description="Active, in-scope assets. Excluded assets are reported separately."))
    panels.append(stat_panel(
        2, "Compliant", stat_sql("compliant"), 4, y, 4, 4, color=GREEN,
        description="Fresh assessment with no outstanding blocking findings."))
    panels.append(stat_panel(
        3, "Non-compliant", stat_sql("non_compliant"), 8, y, 4, 4, color=RED,
        description="At least one outstanding blocking finding."))
    panels.append(stat_panel(
        4, "Unknown / stale", stat_sql("unknown_stale"), 12, y, 4, 4, color=AMBER,
        description="Counted in the denominator. Never treated as compliant."))
    panels.append(stat_panel(
        5, "Compliance vs target",
        stat_sql("round(100.0 * compliant / NULLIF(denominator, 0), 2)"),
        16, y, 4, 4, unit="percent", decimals=1, color=BLUE,
        description="compliant ÷ (compliant + non_compliant + unknown + stale). "
                    "Compare against the target line in the trend panels."))
    panels.append(stat_panel(
        6, "Excluded from scope",
        stat_sql("excluded", include_excluded=True), 20, y, 4, 4, color=GREY,
        description="Retired, decommissioned or explicitly excluded. Retained in "
                    "history but outside the denominator."))

    y = 4
    panels.append(barchart_panel(
        10, "Compliance by operating system",
        breakdown_sql("os_version", "Operating system",
                      label_expr="a.os_family || ' ' || a.os_version"),
        "Operating system", 0, y, 8, 8))
    panels.append(barchart_panel(
        11, "Compliance by environment",
        breakdown_sql("environment", "Environment"), "Environment", 8, y, 8, 8,
        upright_labels=True))
    panels.append(donut_panel(12, 16, y, 8, 8))

    y = 12
    panels.append(barchart_panel(
        13, "Compliance by business service",
        breakdown_sql("business_group", "Business service", limit=10),
        "Business service", 0, y, 12, 9, horizontal=True))
    panels.append(barchart_panel(
        14, "Outstanding findings by severity",
        SEVERITY_SQL, "Severity", 12, y, 12, 9, horizontal=True,
        description="Patch-level metric. Kept separate from the server-level "
                    "compliance percentage; the two are never blended."))
    # One measure, one hue: severity rank is carried by the category axis, not
    # by a graded colour ramp that would imply a second dimension.
    panels[-1]["fieldConfig"]["defaults"]["color"] = {
        "mode": "fixed", "fixedColor": ORANGE}
    panels[-1]["fieldConfig"]["overrides"] = []

    y = 21
    panels.append(timeseries_panel(
        15, "Monthly compliance trend",
        MONTHLY_SQL, 0, y, 12, 8,
        description="Derived from immutable daily snapshots. The dashed threshold is "
                    "the active policy target."))
    panels.append(barchart_panel(
        16, "Quarterly compliance vs target", QUARTERLY_SQL, "Quarter", 12, y, 12, 8,
        description="Each bar is an archived quarterly snapshot and is never "
                    "replaced. The target is the value recorded with that snapshot."))
    # Two measures sharing one axis: both are percentages of the same population.
    panels[-1]["fieldConfig"]["defaults"]["unit"] = "percent"
    panels[-1]["fieldConfig"]["defaults"]["max"] = 100
    panels[-1]["fieldConfig"]["defaults"]["min"] = 0
    panels[-1]["fieldConfig"]["defaults"]["custom"]["stacking"] = {"mode": "none", "group": "A"}
    panels[-1]["options"]["stacking"] = "none"
    panels[-1]["fieldConfig"]["overrides"] = [
        {"matcher": {"id": "byName", "options": "Compliance"},
         "properties": [{"id": "color", "value": {
             "mode": "fixed", "fixedColor": BLUE}}]},
        {"matcher": {"id": "byName", "options": "Target"},
         "properties": [{"id": "color", "value": {
             "mode": "fixed", "fixedColor": ORANGE}}]},
    ]

    y = 29
    panels.append(table_panel(
        17, "Top non-compliant servers", TOP_SQL, 0, y, 16, 9,
        description="Remediation is performed in the authoritative patch system; "
                    "PatchTrack records status only."))
    panels.append(table_panel(
        18, "Collection health", FRESHNESS_SQL, 16, y, 8, 9,
        description="A failed collection does not delete inventory and does not "
                    "classify anything as compliant."))

    return {
        "__inputs": [],
        "__requires": [],
        "uid": "patchtrack-overview",
        "title": "PatchTrack — Patch Compliance Overview",
        "tags": ["patchtrack", "compliance"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "refresh": "5m",
        "editable": False,
        "graphTooltip": 1,
        "time": {"from": "now-90d", "to": "now"},
        "timepicker": {"refresh_intervals": ["1m", "5m", "15m", "30m", "1h"]},
        "templating": {"list": [
            {
                "name": "os_family",
                "label": "Operating system",
                "type": "query",
                "datasource": DATASOURCE,
                "query": "SELECT DISTINCT os_family FROM inventory.asset_effective "
                         "WHERE os_family <> '' ORDER BY 1",
                "refresh": 1,
                "includeAll": True,
                "allValue": ALL_SENTINEL,
                "multi": False,
                "current": {"selected": True, "text": "All",
                            "value": ALL_SENTINEL},
                "options": [],
            },
            {
                "name": "environment",
                "label": "Environment",
                "type": "query",
                "datasource": DATASOURCE,
                "query": "SELECT DISTINCT environment FROM inventory.asset_effective "
                         "WHERE environment <> '' ORDER BY 1",
                "refresh": 1,
                "includeAll": True,
                "allValue": ALL_SENTINEL,
                "multi": False,
                "current": {"selected": True, "text": "All",
                            "value": ALL_SENTINEL},
                "options": [],
            },
            {
                "name": "business_group",
                "label": "Business service",
                "type": "query",
                "datasource": DATASOURCE,
                "query": "SELECT DISTINCT business_group FROM inventory.asset_effective "
                         "WHERE business_group <> '' ORDER BY 1",
                "refresh": 1,
                "includeAll": True,
                "allValue": ALL_SENTINEL,
                "multi": False,
                "current": {"selected": True, "text": "All",
                            "value": ALL_SENTINEL},
                "options": [],
            },
        ]},
        "annotations": {"list": []},
        "panels": panels,
    }


def main() -> None:
    dashboard = build()
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)} ({len(dashboard['panels'])} panels)")


if __name__ == "__main__":
    main()
