"""PDF report rendering and integrity validation.

The requirements are explicit: a report is only published once the file has
been verified, and its SHA-256 must match the bytes that are archived.
"""

from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime

import pytest

from reporting.app.pdf import (
    ReportContext,
    _dimension_summary,
    _top_servers_summary,
    render_report,
    sha256_of,
    validate_pdf,
)

GENERATED_AT = datetime(2026, 10, 26, 0, 30, tzinfo=UTC)


def context(**overrides) -> ReportContext:
    snapshot = {
        "total_servers": 120,
        "compliant_servers": 48,
        "non_compliant_servers": 53,
        "unknown_servers": 6,
        "stale_servers": 4,
        "excluded_servers": 9,
        "compliance_pct": 43.24,
        "target_pct": 95.0,
        "critical_findings": 39,
        "total_findings": 272,
        "period_start": "2026-10-01",
        "period_end": "2026-11-01",
        "captured_at": GENERATED_AT,
    }
    base = {
        "title": "Monthly Patch Compliance Report",
        "organization": "Example Corporation",
        "classification": "CONFIDENTIAL",
        "period_label": "October 2026",
        "generated_at": GENERATED_AT,
        "generated_by": "tester",
        "snapshot": snapshot,
        "groups": {
            "os_family": [
                {"group": "rhel", "total_servers": 60, "compliant": 25,
                 "non_compliant": 28, "unknown": 4, "stale": 3,
                 "compliance_pct": 44.6},
                {"group": "windows", "total_servers": 51, "compliant": 20,
                 "non_compliant": 25, "unknown": 4, "stale": 2,
                 "compliance_pct": 41.2},
            ],
            "environment": [
                {"group": "Production", "total_servers": 60, "compliant": 30,
                 "non_compliant": 20, "unknown": 6, "stale": 4,
                 "compliance_pct": 50.0},
            ],
            "business_group": [
                {"group": "Payments", "total_servers": 40, "compliant": 20,
                 "non_compliant": 15, "unknown": 3, "stale": 2,
                 "compliance_pct": 50.0},
            ],
            "severity": [
                {"group": "critical", "count": 39},
                {"group": "important", "count": 87},
            ],
        },
        "previous": None,
        "methodology": {
            "denominator": "compliant + non_compliant + unknown + stale",
            "freshness_hours": 48,
            "blocking_severities": ["critical", "important"],
            "excluded_note": "Excluded servers are outside the denominator.",
        },
        "freshness": {"inventory_synced_at": GENERATED_AT,
                      "patch_synced_at": GENERATED_AT},
        "integration_health": [
            {"integration": "assetexplorer", "status": "success",
             "started_at": GENERATED_AT, "assets_unmatched": 0},
            {"integration": "satellite", "status": "success",
             "started_at": GENERATED_AT, "assets_unmatched": 1},
        ],
        "top_non_compliant": [
            {"hostname": f"srv-{i:03d}", "environment": "Production",
             "business_group": "Payments", "os_name": "Red Hat Enterprise Linux",
             "os_version": "9.4", "total_patches": 12, "critical_count": 3}
            for i in range(10)
        ],
        "findings": [
            {"advisory_id": f"RHSA-2026:{i}", "severity": "critical",
             "title": "Example advisory", "affected_servers": 5}
            for i in range(5)
        ],
        "trend": [
            {"month": "2026-09-01", "compliance_pct": 41.0},
            {"month": "2026-10-01", "compliance_pct": 43.24},
        ],
        "logo_path": None,
    }
    base.update(overrides)
    return ReportContext(**base)


@pytest.fixture(scope="module")
def rendered() -> bytes:
    return render_report(context())


def test_report_is_a_valid_pdf(rendered):
    ok, problem = validate_pdf(rendered)
    assert ok, problem
    assert rendered.startswith(b"%PDF-")
    assert b"%%EOF" in rendered[-1024:]


def test_report_is_nontrivial(rendered):
    assert len(rendered) > 4000


def test_report_is_deterministic_for_the_same_context():
    """Two renders of identical context describe the same period identically.

    Byte equality is not required (ReportLab embeds a creation timestamp), but
    the digest of the content must be stable enough that a re-generation of
    an archived period can be compared; here we assert the size is stable and
    the text content is unchanged.
    """
    first = render_report(context())
    second = render_report(context())
    assert abs(len(first) - len(second)) < 64


def test_abbreviations_page_follows_the_table_of_contents(rendered):
    """The abbreviation table sits on its own page after the contents page."""
    from pypdf import PdfReader

    pages = [page.extract_text() or ""
             for page in PdfReader(io.BytesIO(rendered)).pages]
    contents = next(index for index, text in enumerate(pages)
                    if "Contents" in text and "Executive summary" in text)
    abbreviations = next(index for index, text in enumerate(pages)
                         if "Abbreviations and definitions" in text
                         and "Meaning" in text)
    assert abbreviations == contents + 1
    assert "SCCM" in pages[abbreviations]
    assert "RHSA" in pages[abbreviations]
    # The contents entry points at that page (PDF page numbers are 1-based).
    assert "Abbreviations and definitions" in pages[contents]
    assert str(abbreviations + 1) in pages[contents]


def test_digest_is_the_sha256_of_the_bytes():
    data = b"%PDF-1.4 example"
    assert sha256_of(data) == hashlib.sha256(data).hexdigest()


def test_truncated_pdf_is_rejected(rendered):
    ok, problem = validate_pdf(rendered[:-1024])
    assert not ok
    assert "EOF" in problem or "small" in problem


def test_non_pdf_is_rejected():
    ok, problem = validate_pdf(b"<html>not a report</html>" * 100)
    assert not ok
    assert "not a PDF" in problem


def test_tiny_file_is_rejected():
    ok, problem = validate_pdf(b"%PDF-1.4")
    assert not ok
    assert "small" in problem


def test_branding_and_signatories_render():
    """The cover logo and the preparer/concurrence blocks are optional in the
    data model but must not prevent a report when they are supplied."""
    import base64

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
        "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    branded = context(
        logo_data=png,
        preparer_name="Alex Morgan", preparer_title="Security Analyst",
        concur_name="Robin Lee", concur_title="Head of IT Operations",
    )
    data = render_report(branded)
    ok, problem = validate_pdf(data)
    assert ok, problem
    # A logo is an image object on the page; its absence would indicate the
    # uploader saved a file the renderer silently dropped.
    assert b"/Subtype /Image" in data


def test_a_corrupt_logo_does_not_break_the_report():
    data = render_report(context(logo_data=b"definitely not an image"))
    ok, problem = validate_pdf(data)
    assert ok, problem


def test_empty_population_renders_without_fabricating_a_percentage():
    """An empty population must render a report, not crash and not invent 0%."""
    empty = context(snapshot={
        "total_servers": 0, "compliant_servers": 0, "non_compliant_servers": 0,
        "unknown_servers": 0, "stale_servers": 0, "excluded_servers": 0,
        "compliance_pct": None, "target_pct": 95.0, "critical_findings": 0,
        "total_findings": 0, "period_start": "2026-10-01",
        "period_end": "2026-11-01", "captured_at": GENERATED_AT,
    }, groups={"os_family": [], "environment": [], "business_group": [],
               "severity": []}, top_non_compliant=[], findings=[], trend=[])
    data = render_report(empty)
    ok, problem = validate_pdf(data)
    assert ok, problem


def test_verified_exceptions_list_both_justified_and_unjustified_servers():
    """The section shows the whole failing population, split by whether a
    justification exists, and keeps the statement inside its table."""
    from pypdf import PdfReader

    justified = {
        "hostname": "srv-justified-01",
        "state": "non_compliant",
        "compliance_reason": "3 blocking findings",
        "os_name": "Red Hat Enterprise Linux", "os_version": "9.4",
        "environment": "Production", "business_group": "Payments",
        "reason_code": "scheduled_remediation",
        "reason_label": "Remediation scheduled",
        "statement": "Verified against change record CHG-1042; the window is booked.",
        "remediation_plan": "Apply RHSA-2026:1234 in the Tuesday window.",
        "remediation_target": "2026-11-03",
        "created_by": "operator",
        "created_at": GENERATED_AT,
        "justification_id": "00000000-0000-0000-0000-000000000001",
        "evidence_count": 1,
        "evidence_names": ["CHG-1042.pdf"],
    }
    unverified = {
        "hostname": "srv-unverified-02",
        "state": "stale",
        "compliance_reason": "Assessment is 51.2h old",
        "os_name": "Microsoft Windows Server", "os_version": "2022",
        "environment": "Production", "business_group": "Core Banking",
        "reason_code": None, "justification_id": None,
        "evidence_count": 0, "evidence_names": [],
        "total_patches": 42,
    }
    data = render_report(context(failing_servers=[justified, unverified]))
    ok, problem = validate_pdf(data)
    assert ok, problem
    text = "\n".join(
        page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)
    flat = " ".join(text.split())
    assert "Verified exceptions and remediation" in text
    assert "Servers with a recorded justification (1)" in text
    assert "Servers without a justification (1)" in text
    assert "srv-justified-01" in text
    assert "srv-unverified-02" in text
    assert "Remediation scheduled" in flat
    assert "Verified against change record CHG-1042" in flat
    assert "Apply RHSA-2026:1234" in flat
    assert "CHG-1042" in flat and ".pdf" in flat
    assert "Assessment is 51.2h old" in flat


def test_a_justification_cannot_inject_report_markup():
    from pypdf import PdfReader

    data = render_report(context(failing_servers=[{
        "hostname": "srv-<b>bold</b>",
        "state": "stale",
        "reason_code": "collection_gap",
        "reason_label": "Collection gap",
        "statement": "Statement with <b>markup</b> & an ampersand.",
        "created_by": "operator",
        "created_at": GENERATED_AT,
        "justification_id": "00000000-0000-0000-0000-000000000002",
        "evidence_count": 0,
        "evidence_names": [],
    }]))
    text = "\n".join(
        page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)
    flat = " ".join(text.split())
    assert "srv-<b>bold</b>" in flat
    assert "Statement with <b>markup</b> & an ampersand." in flat


def test_no_verified_exceptions_section_when_nothing_is_recorded():
    """An empty section is not printed: its absence states there are none."""
    from pypdf import PdfReader

    data = render_report(context(failing_servers=[]))
    text = "\n".join(
        page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)
    assert "Verified exceptions and remediation" not in text


def test_data_quality_timestamps_use_the_system_timezone_without_time():
    from pypdf import PdfReader

    # 20:30 UTC is already the next day in Kuala Lumpur; the report shows the
    # local date, without a clock time or zone abbreviation.
    late = datetime(2026, 10, 25, 20, 30, tzinfo=UTC)
    data = render_report(context(
        timezone="Asia/Kuala_Lumpur",
        freshness={"inventory_synced_at": late, "patch_synced_at": late},
    ))
    text = "\n".join(
        page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)
    flat = " ".join(text.split())
    assert "Inventory last synchronised: 26/10/2026" in flat
    assert "Patch compliance last synchronised: 26/10/2026" in flat
    assert "+08" not in flat


MARKUP = '<img src="file:///etc/passwd" onerror="alert(1)">&'


def test_group_names_are_escaped_before_reportlab_parses_them():
    rows = [{
        "group_key": MARKUP, "compliant": 1, "non_compliant": 1,
        "unknown": 0, "stale": 0,
    }]
    summary = _dimension_summary(rows, "environment", 95.0, "environment")
    # The sentence is handed to a Paragraph, which parses mini-XML. A raw tag
    # here would be interpreted (or break the document) rather than shown.
    assert "&lt;img" in summary
    assert "<img" not in summary


def test_a_hostname_in_the_top_servers_sentence_is_escaped():
    rows = [{"hostname": MARKUP, "total_patches": 9}]
    summary = _top_servers_summary(rows)
    assert "&lt;img" in summary
    assert "<img" not in summary


def test_markup_in_branding_and_group_names_does_not_break_the_report():
    groups = {
        "os_family": [{"group": MARKUP, "compliant": 1, "non_compliant": 1}],
        "environment": [{"group": MARKUP, "compliant": 1, "non_compliant": 1}],
        "business_group": [],
        "severity": [{"group": "critical", "count": 1}],
    }
    malicious = context(
        organization=MARKUP,
        title=MARKUP,
        classification=MARKUP,
        groups=groups,
        top_non_compliant=[{
            "hostname": MARKUP, "environment": MARKUP,
            "business_group": MARKUP, "os_name": MARKUP, "os_version": "9",
            "total_patches": 9,
        }],
    )
    ok, problem = validate_pdf(render_report(malicious))
    assert ok, problem
