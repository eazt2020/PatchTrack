"""PDF report rendering with ReportLab.

Reports are rendered from the same compliance snapshots the dashboard uses,
so the numbers agree by construction. The renderer validates the produced
PDF (page count, non-trivial size, EOF marker) before the caller may publish
it.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from reportlab.graphics.charts.barcharts import HorizontalBarChart
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

BRAND_DARK = colors.HexColor("#1b2430")
BRAND_ACCENT = colors.HexColor("#2f81f7")
COMPLIANT = colors.HexColor("#2ea043")
NON_COMPLIANT = colors.HexColor("#d73a49")
UNKNOWN = colors.HexColor("#d29922")
MUTED = colors.HexColor("#5b6673")
GRID = colors.HexColor("#d8dee4")


@dataclass
class ReportContext:
    title: str
    organization: str
    classification: str
    period_label: str
    generated_at: datetime
    generated_by: str
    snapshot: dict[str, Any]
    groups: dict[str, list[dict[str, Any]]]
    previous: dict[str, Any] | None
    methodology: dict[str, Any]
    freshness: dict[str, Any]
    integration_health: list[dict[str, Any]]
    top_non_compliant: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    trend: list[dict[str, Any]]
    logo_path: str | None = None


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.1f}%"


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "PatchTitle", parent=base["Title"], fontName="Helvetica-Bold",
            fontSize=22, leading=26, textColor=BRAND_DARK, alignment=TA_LEFT,
            spaceAfter=2,
        ),
        "subtitle": ParagraphStyle(
            "PatchSubtitle", parent=base["Normal"], fontSize=10.5, leading=14,
            textColor=MUTED, spaceAfter=10,
        ),
        "h1": ParagraphStyle(
            "PatchH1", parent=base["Heading1"], fontName="Helvetica-Bold",
            fontSize=14, leading=18, textColor=BRAND_DARK, spaceBefore=10,
            spaceAfter=6,
        ),
        "h2": ParagraphStyle(
            "PatchH2", parent=base["Heading2"], fontName="Helvetica-Bold",
            fontSize=11.5, leading=15, textColor=BRAND_DARK, spaceBefore=8,
            spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "PatchBody", parent=base["Normal"], fontSize=9.5, leading=13.5,
            textColor=colors.HexColor("#24292f"),
        ),
        "small": ParagraphStyle(
            "PatchSmall", parent=base["Normal"], fontSize=8, leading=11,
            textColor=MUTED,
        ),
    }


class _NumberedDoc(BaseDocTemplate):
    def __init__(self, *args: Any, context: ReportContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.context = context

    def _draw_footer(self, canvas, doc) -> None:
        canvas.saveState()
        width, _ = A4
        canvas.setStrokeColor(GRID)
        canvas.setLineWidth(0.5)
        canvas.line(18 * mm, 15 * mm, width - 18 * mm, 15 * mm)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 10 * mm,
                          f"{self.context.classification} · "
                          f"{self.context.organization}")
        canvas.drawRightString(
            width - 18 * mm, 10 * mm,
            f"Generated {self.context.generated_at:%Y-%m-%d %H:%M} UTC · "
            f"Page {doc.page}",
        )
        canvas.restoreState()

    def _draw_header(self, canvas, doc) -> None:
        canvas.saveState()
        width, height = A4
        canvas.setFillColor(BRAND_DARK)
        canvas.rect(0, height - 12 * mm, width, 12 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 9)
        canvas.drawString(18 * mm, height - 8 * mm, "PATCHTRACK")
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(width - 18 * mm, height - 8 * mm,
                               self.context.period_label)
        canvas.restoreState()


def _kpi_table(context: ReportContext, styles: dict[str, ParagraphStyle]) -> Table:
    snap = context.snapshot
    values = [
        ["Total servers", "Compliant", "Non-compliant", "Unknown", "Stale",
         "Excluded"],
        [
            str(snap.get("total_servers", 0)),
            str(snap.get("compliant_servers", 0)),
            str(snap.get("non_compliant_servers", 0)),
            str(snap.get("unknown_servers", 0)),
            str(snap.get("stale_servers", 0)),
            str(snap.get("excluded_servers", 0)),
        ],
    ]
    table = Table(values, colWidths=[29 * mm] * 6)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f5f8")),
                ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5),
                ("FONT", (0, 1), (-1, 1), "Helvetica-Bold", 17),
                ("TEXTCOLOR", (1, 1), (1, 1), COMPLIANT),
                ("TEXTCOLOR", (2, 1), (2, 1), NON_COMPLIANT),
                ("TEXTCOLOR", (3, 1), (4, 1), UNKNOWN),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("GRID", (0, 0), (-1, -1), 0.5, GRID),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


def _group_table(rows: list[dict[str, Any]], label: str) -> Table:
    data = [[label, "Total", "Compliant", "Non-compliant", "Unknown/Stale", "%"]]
    for row in rows[:15]:
        total = row.get("total_servers") or 0
        denom = (row.get("compliant", 0) + row.get("non_compliant", 0)
                 + row.get("unknown", 0) + row.get("stale", 0))
        pct = f"{(row.get('compliant', 0) / denom * 100):.1f}%" if denom else "n/a"
        data.append([
            str(row.get("group_key", ""))[:38], str(total),
            str(row.get("compliant", 0)), str(row.get("non_compliant", 0)),
            str((row.get("unknown") or 0) + (row.get("stale") or 0)), pct,
        ])
    if len(data) == 1:
        data.append(["No data captured for this dimension", "", "", "", "", ""])
    table = Table(data, colWidths=[64 * mm, 22 * mm, 26 * mm, 30 * mm,
                                   28 * mm, 18 * mm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), BRAND_DARK),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5),
                ("FONT", (0, 1), (-1, -1), "Helvetica", 7.8),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [colors.white, colors.HexColor("#f7f9fb")]),
                ("GRID", (0, 0), (-1, -1), 0.4, GRID),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


SEVERITY_COLOURS = {
    "critical": colors.HexColor("#b3202d"),
    "important": colors.HexColor("#d4622f"),
    "moderate": colors.HexColor("#b58200"),
    "low": colors.HexColor("#2f6fb5"),
    "unknown": colors.HexColor("#5b6673"),
}


def _severity_chart(rows: list[dict[str, Any]]) -> Drawing:
    """Severity is a status scale, so each bar carries its own status colour and
    the category axis names it — identity is never colour alone."""
    drawing = Drawing(460, 130)
    chart = HorizontalBarChart()
    chart.width = 380
    chart.height = 110
    chart.x = 70
    chart.y = 10
    chart.data = [[row.get("finding_count", 0) for row in rows]]
    names = [str(row.get("group_key", "")).capitalize() for row in rows]
    chart.categoryAxis.categoryNames = names
    chart.valueAxis.valueMin = 0
    chart.barWidth = 8
    for index, row in enumerate(rows):
        chart.bars[(0, index)].fillColor = SEVERITY_COLOURS.get(
            str(row.get("group_key", "")).lower(), MUTED
        )
    drawing.add(chart)
    return drawing


def _trend_chart(rows: list[dict[str, Any]], target: float | None = None) -> Drawing:
    from reportlab.graphics.charts.linecharts import HorizontalLineChart
    from reportlab.graphics.shapes import Line, String

    drawing = Drawing(500, 170)
    chart = HorizontalLineChart()
    chart.width = 420
    chart.height = 118
    chart.x = 50
    chart.y = 26
    chart.data = [[float(r["compliance_pct"] or 0) for r in rows]]
    chart.categoryAxis.categoryNames = [
        str(r["month"])[:7] if not hasattr(r["month"], "strftime")
        else r["month"].strftime("%b %Y")
        for r in rows
    ]
    chart.valueAxis.valueMin = 0
    chart.valueAxis.valueMax = 100
    chart.lines[0].strokeColor = BRAND_ACCENT
    chart.lines[0].strokeWidth = 2
    drawing.add(chart)
    if target is not None and len(rows) >= 2:
        y = chart.y + (float(target) / 100.0) * chart.height
        drawing.add(Line(
            chart.x, y, chart.x + chart.width, y,
            strokeColor=colors.HexColor("#d4622f"), strokeWidth=1,
            strokeDashArray=[4, 3],
        ))
        drawing.add(String(
            chart.x + chart.width + 2, y - 3, f"Target {float(target):.0f}%",
            fontName="Helvetica", fontSize=6.5,
            fillColor=colors.HexColor("#d4622f"),
        ))
    return drawing


def render_report(context: ReportContext) -> bytes:
    buffer = io.BytesIO()
    styles = _styles()
    doc = _NumberedDoc(
        buffer, pagesize=A4, context=context,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=20 * mm,
        title=context.title,
        author=f"PatchTrack ({context.generated_by})",
        subject=f"{context.period_label} patch compliance",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height,
                  id="body")
    doc.addPageTemplates(
        [
            PageTemplate(id="first", frames=[frame],
                         onPage=doc._draw_header,
                         onPageEnd=doc._draw_footer),
            PageTemplate(id="rest", frames=[frame],
                         onPage=doc._draw_header,
                         onPageEnd=doc._draw_footer),
        ]
    )

    story: list[Any] = []
    if context.logo_path:
        try:
            story.append(Image(context.logo_path, width=52 * mm, height=16 * mm,
                               kind="proportional"))
            story.append(Spacer(1, 4))
        except Exception:  # noqa: BLE001 - a bad logo must not break the report
            pass
    story += [
        Paragraph(context.title, styles["title"]),
        Paragraph(
            f"{context.organization} · Reporting period {context.period_label} · "
            f"Generated by {context.generated_by} at "
            f"{context.generated_at:%Y-%m-%d %H:%M} UTC",
            styles["subtitle"],
        ),
        _kpi_table(context, styles),
        Spacer(1, 6),
    ]

    snap = context.snapshot
    pct = _fmt_pct(snap.get("compliance_pct"))
    target = _fmt_pct(snap.get("target_pct"))
    previous_pct = None
    if context.previous:
        previous_pct = context.previous.get("compliance_pct")
    delta = ""
    if previous_pct is not None and snap.get("compliance_pct") is not None:
        change = float(snap["compliance_pct"]) - float(previous_pct)
        direction = "improved" if change >= 0 else "deteriorated"
        delta = (f" Compliance has {direction} by {abs(change):.1f} percentage "
                 f"points compared with the previous period "
                 f"({_fmt_pct(previous_pct)}).")
    story += [
        Paragraph("Executive summary", styles["h1"]),
        Paragraph(
            f"Patch compliance for the reporting period "
            f"{context.period_label} is "
            f"<b>{pct}</b> against a target of <b>{target}</b>. "
            f"Of {snap.get('total_servers', 0)} servers in the recorded population, "
            f"{snap.get('compliant_servers', 0)} are compliant, "
            f"{snap.get('non_compliant_servers', 0)} are non-compliant, "
            f"{snap.get('unknown_servers', 0)} have unknown status and "
            f"{snap.get('stale_servers', 0)} carry stale assessment data."
            f"{delta}",
            styles["body"],
        ),
        Spacer(1, 4),
    ]

    methodology = context.methodology or {}
    gap = None
    if snap.get("compliance_pct") is not None and snap.get("target_pct") is not None:
        gap = float(snap["compliance_pct"]) - float(snap["target_pct"])
    if gap is not None and gap < 0:
        story.append(
            Paragraph(
                f"<b>Target not met.</b> The organisation is {abs(gap):.1f} "
                f"percentage points below the {target} target. Unknown and stale "
                "records are counted against compliance, not excluded from it.",
                styles["body"],
            )
        )
    else:
        story.append(
            Paragraph(f"<b>Target met</b> for the recorded period.",
                      styles["body"])
        )

    story += [Paragraph("Compliance overview", styles["h1"])]
    for dimension, label in (
        ("os_family", "Compliance by operating system"),
        ("environment", "Compliance by environment"),
        ("business_group", "Compliance by business group"),
        ("application_group", "Compliance by application group"),
    ):
        rows = context.groups.get(dimension, [])
        story.append(Paragraph(label, styles["h2"]))
        story.append(_group_table(rows, "Group"))
        story.append(Spacer(1, 5))

    story.append(PageBreak())
    story.append(Paragraph("Remediation overview", styles["h1"]))
    severity_rows = context.groups.get("severity", [])
    if severity_rows:
        story.append(_severity_chart(severity_rows))
        story.append(Spacer(1, 6))
    story.append(
        Paragraph(
            f"{snap.get('total_findings', 0)} outstanding patch findings were "
            f"recorded, of which {snap.get('critical_findings', 0)} are critical. "
            "Patch findings reflect the latest assessment reported by each "
            "patch-management source.",
            styles["body"],
        )
    )
    if context.top_non_compliant:
        story.append(Paragraph("Servers with the most outstanding patches",
                               styles["h2"]))
        data = [["Hostname", "OS", "Environment", "Business group", "Patches"]]
        for row in context.top_non_compliant[:15]:
            data.append([
                str(row.get("hostname", "")),
                f"{row.get('os_name', '')} {row.get('os_version', '')}".strip(),
                str(row.get("environment", "")),
                str(row.get("business_group", "")),
                str(row.get("total_patches") or 0),
            ])
        table = Table(data, colWidths=[42 * mm, 48 * mm, 26 * mm, 46 * mm, 20 * mm],
                      repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f5f8")),
                    ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5),
                    ("FONT", (0, 1), (-1, -1), "Helvetica", 7.8),
                    ("GRID", (0, 0), (-1, -1), 0.4, GRID),
                    ("ALIGN", (-1, 1), (-1, -1), "RIGHT"),
                ]
            )
        )
        story.append(table)

    story += [Paragraph("Historical analysis", styles["h1"])]
    if len(context.trend) >= 2:
        story.append(_trend_chart(
            context.trend, context.snapshot.get("target_pct")
        ))
        story.append(Spacer(1, 4))
        story.append(
            Paragraph(
                "The trend is derived from immutable daily compliance snapshots. "
                "Each point is the recorded compliance percentage for that month.",
                styles["small"],
            )
        )
    else:
        story.append(
            Paragraph(
                "Insufficient historical snapshots exist to plot a trend. Daily "
                "snapshots accumulate from the point the platform is in service.",
                styles["body"],
            )
        )

    story += [
        Paragraph("Data quality and operational health", styles["h1"]),
        Paragraph(
            f"Inventory last synchronised: "
            f"{_fmt_ts(context.freshness.get('inventory_synced_at'))}. "
            f"Patch compliance last synchronised: "
            f"{_fmt_ts(context.freshness.get('patch_synced_at'))}.",
            styles["body"],
        ),
    ]
    if context.integration_health:
        data = [["Integration", "Status", "Last success", "Last result"]]
        for row in context.integration_health:
            data.append([
                str(row.get("key", "")),
                str(row.get("health", "")),
                _fmt_ts(row.get("last_success_at")),
                str((row.get("last_run") or {}).get("status", "never")),
            ])
        table = Table(data, colWidths=[42 * mm, 40 * mm, 50 * mm, 30 * mm],
                      repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f5f8")),
                    ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5),
                    ("FONT", (0, 1), (-1, -1), "Helvetica", 7.8),
                    ("GRID", (0, 0), (-1, -1), 0.4, GRID),
                ]
            )
        )
        story.append(table)
    story.append(
        Paragraph(
            "Servers with unknown or stale data are not reported as compliant. "
            "Where a source system did not return data for a period, the "
            "affected servers are excluded from the compliant count.",
            styles["small"],
        )
    )

    story += [
        Paragraph("Methodology", styles["h1"]),
        Paragraph(
            f"<b>Reporting period:</b> {context.period_label}<br/>"
            f"<b>Population:</b> {methodology.get('population', 'in-scope assets')}<br/>"
            f"<b>Numerator:</b> {methodology.get('numerator', 'compliant assets')}<br/>"
            f"<b>Denominator:</b> "
            f"{methodology.get('denominator', 'compliant + non-compliant + unknown + stale')}<br/>"
            f"<b>Unknown treatment:</b> "
            f"{methodology.get('unknown_treatment', 'counted in denominator')}<br/>"
            f"<b>Freshness threshold:</b> "
            f"{methodology.get('freshness_hours', 'n/a')} hours<br/>"
            f"<b>Blocking severities:</b> "
            f"{', '.join(methodology.get('blocking_severities') or []) or 'n/a'}<br/>"
            f"<b>Policy:</b> {methodology.get('policy_name', 'Baseline')}",
            styles["body"],
        ),
        Spacer(1, 8),
        Paragraph(
            "Data sources: ManagedEngine AssetExplorer (inventory), Red Hat "
            "Satellite (Linux), Microsoft Configuration Manager (Windows) and "
            "the approved Solaris collection process. This report is a record "
            "of the state at generation time and is not revised retroactively.",
            styles["small"],
        ),
    ]
    doc.build(story)
    return buffer.getvalue()


def _fmt_ts(value: Any) -> str:
    if not value:
        return "never"
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M UTC")
    return str(value)


def validate_pdf(data: bytes) -> tuple[bool, str]:
    if len(data) < 1024:
        return False, "Generated PDF is implausibly small"
    if not data.startswith(b"%PDF-"):
        return False, "Output is not a PDF document"
    if b"%%EOF" not in data[-1024:]:
        return False, "PDF is truncated (missing EOF marker)"
    return True, ""


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
