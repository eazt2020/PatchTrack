"""The one-page landscape management summary export.

The export replaces the old board-style dashboard PDF: a single A4 landscape
sheet a manager can read without a walkthrough. It must carry the verdict, the
six-card scorecard, the SLA table and the unit heatmap, and still render one
page for a fresh install with no measured data.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime

import pytest
from pypdf import PdfReader

from reporting.app.main import DashboardExportRequest
from reporting.app.management_pdf import render_management_pdf
from reporting.app.pdf import validate_pdf

GENERATED_AT = datetime(2026, 10, 31, 10, 30, tzinfo=UTC)


def payload(**overrides) -> DashboardExportRequest:
    body = {
        "mode": "live",
        "scope_label": "Live · Production · last 12 months",
        "environment": "Production",
        "kpis": {
            "total_servers": 1000, "in_scope": 948, "compliant": 870,
            "non_compliant": 50, "unknown": 18, "stale": 10, "excluded": 52,
            "compliance_pct": 91.77, "target_pct": 95.0, "gap_pct": -3.23,
            "gap_servers": 30, "critical_findings": 12, "total_findings": 143,
        },
        "remediation": {
            "overdue": 21, "total": 143, "due_soon": 9,
            "due_soon_days": 7, "oldest_days_overdue": 61,
            "on_track": 114, "undated": 4,
        },
        "freshness": {"total_sources": 5, "stale_sources": 1, "oldest_age_hours": 30},
        "management": {
            "as_of": "2026-10-31", "due_soon_days": 7,
            "by_severity": [
                {"severity": "critical", "open": 12, "due_soon": 2, "overdue": 3,
                 "undated": 0, "oldest_days": 14, "median_age_days": 8.4, "days_1_7": 1, "days_8_30": 1, "days_31_90": 1, "days_90_plus": 0},
                {"severity": "important", "open": 38, "due_soon": 4, "overdue": 9,
                 "undated": 0, "oldest_days": 27, "median_age_days": 19.2, "days_1_7": 3, "days_8_30": 2, "days_31_90": 2, "days_90_plus": 2},
                {"severity": "moderate", "open": 61, "due_soon": 3, "overdue": 9,
                 "undated": 0, "oldest_days": 61, "median_age_days": 33.7, "days_1_7": 3, "days_8_30": 1, "days_31_90": 2, "days_90_plus": 2},
                {"severity": "low", "open": 28, "due_soon": 0, "overdue": 0,
                 "undated": 0, "oldest_days": 0, "median_age_days": 41.0, "days_1_7": 1, "days_8_30": 0, "days_31_90": 0, "days_90_plus": 0},
                {"severity": "unknown", "open": 4, "due_soon": 0, "overdue": 0,
                 "undated": 4, "oldest_days": 0, "median_age_days": 36.5, "days_1_7": 0, "days_8_30": 0, "days_31_90": 0, "days_90_plus": 0},
            ],
            "advisories": [
                {"advisory_id": "RHSA-2026:4811",
                 "title": "Important: kernel security and bug fix update",
                 "severity": "critical", "os_family": "rhel",
                 "affected_servers": 148, "open": 148,
                 "overdue": 41, "oldest_days": 87},
                {"advisory_id": "RHSA-2026:3902",
                 "title": "Moderate: openssl security update",
                 "severity": "moderate", "os_family": "rhel",
                 "affected_servers": 211, "open": 211,
                 "overdue": 17, "oldest_days": 44},
                {"advisory_id": "MS26-014",
                 "title": "Cumulative Update for Windows Server 2022",
                 "severity": "critical", "os_family": "windows",
                 "affected_servers": 96, "open": 96,
                 "overdue": 22, "oldest_days": 61},
                {"advisory_id": "MS26-009",
                 "title": "Security Update for .NET Framework",
                 "severity": "moderate", "os_family": "windows",
                 "affected_servers": 71, "open": 71,
                 "overdue": 4, "oldest_days": 11},
                {"advisory_id": "SOL-11.4-2026-10",
                 "title": "Solaris 11.4 kernel update",
                 "severity": "important", "os_family": "solaris",
                 "affected_servers": 38, "open": 38,
                 "overdue": 9, "oldest_days": 28},
            ],
            "units": [
                {"group": "Staging", "affected_servers": 18, "open": 21,
                 "overdue": 6, "oldest_days": 61},
                {"group": "Production", "affected_servers": 17, "open": 18,
                 "overdue": 4, "oldest_days": 27},
            ],
            "overdue_age": {"days_1_7": 8, "days_8_30": 4,
                            "days_31_90": 5, "days_90_plus": 4},
            "total_open": 143, "overdue": 21, "due_soon": 9, "undated": 4,
        },
        "by_environment": [
            {"group": "Production", "total_servers": 194, "compliant": 176,
             "non_compliant": 16, "unknown": 1, "stale": 1, "in_scope": 194},
            {"group": "Staging", "total_servers": 183, "compliant": 161,
             "non_compliant": 18, "unknown": 3, "stale": 1, "in_scope": 183},
        ],
        "by_business_group": [
            {"group": "Payments", "total_servers": 310, "compliant": 280,
             "non_compliant": 22, "unknown": 5, "stale": 3, "in_scope": 310},
            {"group": "Core Banking", "total_servers": 266, "compliant": 240,
             "non_compliant": 18, "unknown": 5, "stale": 3, "in_scope": 266},
        ],
        "by_os": [
            {"group": "rhel", "total_servers": 486, "compliant": 438,
             "non_compliant": 32, "unknown": 9, "stale": 7, "in_scope": 486},
            {"group": "windows", "total_servers": 371, "compliant": 339,
             "non_compliant": 24, "unknown": 5, "stale": 3, "in_scope": 371},
        ],
        "severity": [
            {"severity": "critical", "count": 12},
            {"severity": "important", "count": 38},
            {"severity": "moderate", "count": 61},
        ],
        "trend": [
            {"label": "Nov", "compliance_pct": 88.1, "target_pct": 95,
             "total_servers": 940},
            {"label": "Oct", "compliance_pct": 91.8, "target_pct": 95,
             "total_servers": 948},
        ],
        "servers": [],
    }
    body.update(overrides)
    return DashboardExportRequest.model_validate(body)


def rendered(request: DashboardExportRequest | None = None) -> bytes:
    return render_management_pdf(
        request or payload(), organization="Acme Group",
        generated_at=GENERATED_AT, generated_by="admin", timezone="Asia/Kuala_Lumpur",
    )


def page_text(data: bytes) -> str:
    return PdfReader(io.BytesIO(data)).pages[0].extract_text()


def test_export_is_a_valid_single_landscape_page():
    data = rendered()

    ok, problem = validate_pdf(data)
    assert ok, problem

    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) == 1
    box = reader.pages[0].mediabox
    assert float(box.width) > float(box.height), "A4 landscape, not portrait"


def test_export_carries_the_management_bands_and_figures():
    text = page_text(rendered())
    for expected in (
        "Patch Management — Transparency Dashboard",
        "Compliance trend",
        "Backlog by severity",
        "Compliance by environment",
        "Compliance by business service",
        "Compliance by operating system",
        "Most widespread outstanding advisories",
        "Acme Group",
        "91.8%",
        "Staging",
        "Core Banking",
        "RHEL",
        "RHSA-2026:4811",
        "Windows",
        "Solaris",
        "MS26-014",
        "SOL-11.4-2026-10",
    ):
        assert expected in text


def test_export_reports_sla_against_dated_findings():
    # 143 open, 4 undated, 21 overdue: 118 of the 139 dated findings are on
    # time. Counting the undated ones as late was the earlier bug.
    text = page_text(rendered())
    for expected in (
        "84.9%",
        "118 of 139 dated",
        "+3.7 pp",
        "data 4/5 sources fresh",
    ):
        assert expected in text


def test_environment_panel_reads_the_service_group_key():
    # The compliance service names the grouping column group_key; the model
    # also accepts the older group field. A mismatch here silently blanked the
    # Overdue column, so pin the lookup with a figure that appears nowhere
    # else on the sheet.
    body = payload().model_dump()
    body["management"]["units"] = [{
        "group_key": "Staging", "affected_servers": 18, "open": 21,
        "overdue": 987, "oldest_days": 61,
    }]
    text = page_text(rendered(DashboardExportRequest.model_validate(body)))
    assert "987" in text


def test_empty_view_still_renders_one_page():
    empty = DashboardExportRequest.model_validate({
        "mode": "period",
        "scope_label": "Recorded snapshot · September 2026",
    })
    data = rendered(empty)

    ok, problem = validate_pdf(data)
    assert ok, problem
    assert len(PdfReader(io.BytesIO(data)).pages) == 1


@pytest.mark.parametrize("group", ["G" * 200])
def test_long_values_are_truncated_not_overflowed(group):
    data = rendered(payload(by_environment=[{
        "group": group, "total_servers": 3, "compliant": 1,
        "non_compliant": 1, "unknown": 1, "stale": 0, "in_scope": 3,
    }]))
    ok, problem = validate_pdf(data)
    assert ok, problem
    assert len(PdfReader(io.BytesIO(data)).pages) == 1
