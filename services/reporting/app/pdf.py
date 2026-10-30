"""PDF report rendering with ReportLab.

Reports are rendered from the same compliance snapshots the dashboard uses,
so the numbers agree by construction. The renderer validates the produced
PDF (page count, non-trivial size, EOF marker) before the caller may publish
it.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

from reportlab.graphics.charts.barcharts import HorizontalBarChart
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    BaseDocTemplate,
    CondPageBreak,
    Frame,
    Image,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

# The PDF mirrors the console's neon palette, but every colour here is tuned
# for paper: the page itself stays white, accents are darkened to hold contrast
# when printed or photocopied, and no panel relies on a glow to be legible.
BRAND_DARK = colors.HexColor("#241436")
BRAND_ACCENT = colors.HexColor("#c2185b")
BRAND_VIOLET = colors.HexColor("#5b3fd8")
BRAND_CYAN = colors.HexColor("#0e7490")
COMPLIANT = colors.HexColor("#0b7a5c")
NON_COMPLIANT = colors.HexColor("#c81e4a")
UNKNOWN = colors.HexColor("#9a5b00")
MUTED = colors.HexColor("#5d5870")
# Table rules need to survive a mono office printer, so they are drawn a step
# darker and heavier than the hairline used for decorative dividers.
GRID = colors.HexColor("#9d8db8")
PANEL_TINT = colors.HexColor("#f9f1f8")
BODY_INK = colors.HexColor("#2b2440")

# Bundled product marks, used only when the deployment has not uploaded an
# organisation logo. The tagline version sits on the cover as a dark plaque so
# the neon artwork keeps its contrast on white paper; the square mark goes in
# the running header.
ASSET_DIR = Path(__file__).with_name("assets")
DEFAULT_COVER_LOGO = ASSET_DIR / "logo-tagline.png"
DEFAULT_HEADER_LOGO = ASSET_DIR / "logo.png"
_LOGO_BYTES: dict[str, bytes | None] = {}


def _asset_bytes(path: Path) -> bytes | None:
    key = str(path)
    if key not in _LOGO_BYTES:
        try:
            _LOGO_BYTES[key] = path.read_bytes()
        except OSError:  # a missing asset must never break a report
            _LOGO_BYTES[key] = None
    return _LOGO_BYTES[key]


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
    failing_servers: list[dict[str, Any]] = field(default_factory=list)
    logo_path: str | None = None
    logo_data: bytes | None = None
    preparer_name: str = ""
    preparer_title: str = ""
    concur_name: str = ""
    concur_title: str = ""
    timezone: str = "UTC"


def _generated_label(context: ReportContext) -> str:
    """Render the generation date in the deployment's timezone."""
    try:
        zone = ZoneInfo(context.timezone)
    except Exception:  # noqa: BLE001 - a bad stored name must not break rendering
        zone = ZoneInfo("UTC")
    local = context.generated_at.astimezone(zone)
    return f"{local:%d/%m/%Y}"


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
            fontSize=14, leading=18, textColor=BRAND_DARK, spaceBefore=22,
            spaceAfter=8, alignment=TA_LEFT,
        ),
        "h2": ParagraphStyle(
            "PatchH2", parent=base["Heading2"], fontName="Helvetica-Bold",
            fontSize=11.5, leading=15, textColor=BRAND_DARK, spaceBefore=14,
            spaceAfter=6, alignment=TA_LEFT,
        ),
        # Body copy is justified so the management report reads as a typeset
        # document rather than a stack of ragged-right labels.
        "body": ParagraphStyle(
            "PatchBody", parent=base["Normal"], fontSize=9.5, leading=13.5,
            textColor=BODY_INK, alignment=TA_JUSTIFY,
        ),
        "small": ParagraphStyle(
            "PatchSmall", parent=base["Normal"], fontSize=8, leading=11,
            textColor=MUTED, alignment=TA_JUSTIFY,
        ),
        # Table cells are set small enough for wide evidence tables; operators'
        # statements wrap inside the cell rather than being truncated.
        "cell": ParagraphStyle(
            "PatchCell", parent=base["Normal"], fontName="Helvetica",
            fontSize=7, leading=9, textColor=BODY_INK,
            alignment=TA_LEFT,
        ),
        "cell_head": ParagraphStyle(
            "PatchCellHead", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=7, leading=9, textColor=colors.white, alignment=TA_LEFT,
        ),
        # Cover styles are deliberately far from the body scale: the cover is
        # read at a glance, from a printed page or a board pack thumbnail.
        "cover_org": ParagraphStyle(
            "PatchCoverOrg", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=11, leading=14, textColor=BRAND_ACCENT, alignment=TA_LEFT,
            spaceAfter=2,
        ),
        "cover_title": ParagraphStyle(
            "PatchCoverTitle", parent=base["Title"], fontName="Helvetica-Bold",
            fontSize=30, leading=36, textColor=BRAND_DARK, alignment=TA_LEFT,
            spaceAfter=0,
        ),
        "cover_period": ParagraphStyle(
            "PatchCoverPeriod", parent=base["Normal"], fontName="Helvetica",
            fontSize=15, leading=20, textColor=MUTED, alignment=TA_LEFT,
        ),
        "cover_label": ParagraphStyle(
            "PatchCoverLabel", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=8, leading=10, textColor=MUTED, alignment=TA_LEFT,
        ),
        "cover_value": ParagraphStyle(
            "PatchCoverValue", parent=base["Normal"], fontName="Helvetica",
            fontSize=10, leading=13, textColor=BRAND_DARK, alignment=TA_LEFT,
        ),
        "contents": ParagraphStyle(
            "PatchContents", parent=base["Title"], fontName="Helvetica-Bold",
            fontSize=24, leading=30, textColor=BRAND_DARK, alignment=TA_LEFT,
            spaceAfter=14,
        ),
        "sign_org": ParagraphStyle(
            "PatchSignOrg", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=11.5, leading=15, textColor=BRAND_DARK, alignment=TA_LEFT,
            spaceBefore=14, spaceAfter=6,
        ),
        "sign_role": ParagraphStyle(
            "PatchSignRole", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=9, leading=12, textColor=BRAND_DARK, alignment=TA_LEFT,
        ),
    }


class _NumberedDoc(BaseDocTemplate):
    def __init__(self, *args: Any, context: ReportContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.context = context

    def afterFlowable(self, flowable) -> None:
        """Register every section heading for the table of contents.

        The document is built twice: the first pass records the page each
        heading landed on, the second resolves those numbers in the contents
        page. Only the two heading styles participate; cover text and the
        contents heading itself are deliberately excluded.
        """
        if not isinstance(flowable, Paragraph):
            return
        style = flowable.style.name
        if style == "PatchH1":
            level = 0
        elif style == "PatchH2":
            level = 1
        else:
            return
        text = flowable.getPlainText()
        key = "toc-" + "".join(
            character if character.isalnum() else "-" for character in text
        ).strip("-").lower()
        self.canv.bookmarkPage(key)
        self.notify("TOCEntry", (level, text, self.page, key))

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
            f"Generated {_generated_label(self.context)} · "
            f"Page {doc.page}",
        )
        canvas.restoreState()

    def _draw_header(self, canvas, doc) -> None:
        canvas.saveState()
        width, height = A4
        canvas.setFillColor(BRAND_DARK)
        canvas.rect(0, height - 12 * mm, width, 12 * mm, stroke=0, fill=1)
        _accent_band(canvas, 0, height - 12 * mm, width, 1.2 * mm)
        text_x = 18 * mm
        if not (self.context.logo_data or self.context.logo_path):
            _draw_default_mark(canvas, 18 * mm, height - 10.6 * mm, 8 * mm)
            text_x = 28.5 * mm
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 9)
        canvas.drawString(text_x, height - 8 * mm,
                          _clip(self.context.organization, 48))
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(width - 18 * mm, height - 8 * mm,
                               _clip(self.context.period_label, 52))
        canvas.restoreState()

    def _draw_cover(self, canvas, doc) -> None:
        """The cover opens under its own band, without the running header."""
        width, height = A4
        canvas.saveState()
        canvas.setFillColor(BRAND_DARK)
        canvas.rect(0, height - 42 * mm, width, 42 * mm, stroke=0, fill=1)
        _accent_band(canvas, 0, height - 44.5 * mm, width, 2.5 * mm)
        text_x = 18 * mm
        if not (self.context.logo_data or self.context.logo_path):
            _draw_default_mark(canvas, 18 * mm, height - 31.5 * mm, 10 * mm)
            text_x = 32 * mm
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 10)
        canvas.drawString(text_x, height - 26 * mm,
                          _clip(self.context.organization, 48))
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(width - 18 * mm, height - 26 * mm,
                               self.context.classification)
        canvas.restoreState()

    def _draw_cover_footer(self, canvas, doc) -> None:
        width, _ = A4
        canvas.saveState()
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
            f"Generated {_generated_label(self.context)}",
        )
        canvas.restoreState()


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else f"{text[:limit - 1]}…"


def _accent_band(canvas, x: float, y: float, width: float,
                 height: float) -> None:
    """The console's magenta–violet–cyan rule, flattened for print."""
    segment = width / 3
    for index, colour in enumerate((BRAND_ACCENT, BRAND_VIOLET, BRAND_CYAN)):
        canvas.setFillColor(colour)
        canvas.rect(x + segment * index, y, segment + 0.5, height,
                    stroke=0, fill=1)


def _logo_flowable(context: ReportContext, width: float,
                   height: float) -> Image | None:
    """An uploaded logo wins; a legacy path is only a fallback.

    When neither is configured the bundled PatchTrack mark is used, so a fresh
    installation still ships branded reports without an upload step.
    """
    source: Any = context.logo_data
    if source is None and context.logo_path:
        source = context.logo_path
    if not source:
        source = _asset_bytes(DEFAULT_COVER_LOGO)
        if not source:
            return None
        image = Image(io.BytesIO(source), width=width, height=height,
                      kind="proportional")
        image.hAlign = "LEFT"
        return image
    try:
        image = Image(io.BytesIO(source) if context.logo_data else source,
                      width=width, height=height, kind="proportional")
        image.hAlign = "LEFT"  # Image defaults to centred; the cover is left-aligned
        return image
    except Exception:  # noqa: BLE001 - a bad logo must not break the report
        return None


def _draw_default_mark(canvas, x: float, y: float, size: float) -> None:
    """Draw the square bundled mark on the dark band, if it is available."""
    data = _asset_bytes(DEFAULT_HEADER_LOGO)
    if not data:
        return
    try:
        canvas.drawImage(ImageReader(io.BytesIO(data)), x, y, width=size,
                         height=size, mask="auto", preserveAspectRatio=True,
                         anchor="sw")
    except Exception:  # noqa: BLE001 - never fail a report over branding
        return


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
    table = Table(values, colWidths=[29 * mm] * 6, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), BRAND_DARK),
                ("LINEABOVE", (0, 0), (-1, 0), 1.4, BRAND_ACCENT),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5),
                ("FONT", (0, 1), (-1, 1), "Helvetica-Bold", 17),
                ("TEXTCOLOR", (1, 1), (1, 1), COMPLIANT),
                ("TEXTCOLOR", (2, 1), (2, 1), NON_COMPLIANT),
                ("TEXTCOLOR", (3, 1), (4, 1), UNKNOWN),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("GRID", (0, 0), (-1, -1), 0.7, GRID),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


OS_FAMILY_LABELS = {
    "rhel": "RHEL",
    "windows": "Microsoft Windows",
    "solaris": "Solaris",
    "other": "Other",
}


def _row_key(row: dict[str, Any], dimension: str | None = None) -> str:
    """Real snapshot rows use group_key; older fixtures use group."""
    key = str(row.get("group_key") or row.get("group") or "")
    if dimension == "os_family":
        return OS_FAMILY_LABELS.get(key, key)
    return key


def _compliance_of(row: dict[str, Any]) -> tuple[int, int]:
    """(compliant, denominator) for one dimension row."""
    compliant = int(row.get("compliant") or 0)
    denom = (compliant + int(row.get("non_compliant") or 0)
             + int(row.get("unknown") or 0) + int(row.get("stale") or 0))
    return compliant, denom


def _dimension_summary(
    rows: list[dict[str, Any]], noun: str, target: float | None,
    dimension: str | None = None,
) -> str:
    """A management statement for one breakdown table.

    The table alone makes the reader do the comparison. The sentence in front
    of it states the direction, names the best and worst group and counts the
    groups missing the target, so an executive can read the page without
    scanning every row.
    """
    if not rows:
        return f"No {noun} data was recorded for this reporting period."

    measured = []
    for row in rows:
        compliant, denom = _compliance_of(row)
        measured.append({
            # This text is later parsed by ReportLab as mini-XML, and group
            # names come from inventory imports, so escape them here.
            "name": _escape(_row_key(row, dimension) or "Unnamed group"),
            "compliant": compliant,
            "denom": denom,
            "pct": (compliant / denom * 100) if denom else None,
            "non_compliant": int(row.get("non_compliant") or 0),
            "uncertain": (int(row.get("unknown") or 0)
                          + int(row.get("stale") or 0)),
        })
    total_compliant = sum(item["compliant"] for item in measured)
    total_denom = sum(item["denom"] for item in measured)
    overall = (total_compliant / total_denom * 100) if total_denom else None
    with_pct = [item for item in measured if item["pct"] is not None]

    count = len(rows)
    if count == 1:
        statement = (
            f"The single recorded {noun} covers {total_denom} servers, of "
            f"which {total_compliant} are compliant ({_fmt_pct(overall)})."
        )
    else:
        statement = (
            f"Across {count} {noun}s, {total_denom} servers were assessed and "
            f"{total_compliant} were compliant ({_fmt_pct(overall)})."
        )
    if len(with_pct) >= 2:
        best = max(with_pct, key=lambda item: item["pct"])
        worst = min(with_pct, key=lambda item: item["pct"])
        statement += (
            f" {best['name']} records the highest compliance at "
            f"{_fmt_pct(best['pct'])}, while {worst['name']} records the lowest "
            f"at {_fmt_pct(worst['pct'])}."
        )
    elif len(with_pct) == 1:
        only = with_pct[0]
        statement += (
            f" {only['name']} is the only group with a measurable result, at "
            f"{_fmt_pct(only['pct'])}."
        )

    if target is not None and with_pct:
        missing = [item for item in with_pct if item["pct"] < float(target)]
        if count == 1:
            statement += (
                f" It is below the {_fmt_pct(target)} target." if missing
                else f" It meets the {_fmt_pct(target)} target."
            )
        elif missing:
            statement += (
                f" {len(missing)} of {count} groups are below the "
                f"{_fmt_pct(target)} target."
            )
        else:
            statement += (
                f" All groups with a measurable result meet the "
                f"{_fmt_pct(target)} target."
            )
    non_compliant = sum(item["non_compliant"] for item in measured)
    uncertain = sum(item["uncertain"] for item in measured)
    statement += (
        f" {non_compliant} servers carry outstanding non-compliance and "
        f"{uncertain} have unknown or stale assessment data."
    )
    return statement


def _top_servers_summary(rows: list[dict[str, Any]]) -> str:
    """Management statement for the exception list of worst offenders."""
    if not rows:
        return "No non-compliant servers were recorded for this period."
    shown = rows[:15]
    patches = sum(int(row.get("total_patches") or 0) for row in shown)
    top = max(shown, key=lambda row: int(row.get("total_patches") or 0))
    top_name = _escape(str(top.get("hostname") or "The highest-ranked server"))
    top_count = int(top.get("total_patches") or 0)
    scope = (
        f" of the {len(rows)} non-compliant servers returned" if len(rows) > len(shown)
        else ""
    )
    return (
        f"The table lists the {len(shown)} servers with the largest outstanding "
        f"patch counts{scope}. Together they account for {patches} outstanding "
        f"patches, and {top_name} carries the largest single backlog at "
        f"{top_count}. Remediating this group first delivers the greatest "
        f"reduction in exposure per unit of effort."
    )


def _month_label(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%b %Y")
    text = str(value)
    try:
        return datetime.strptime(text[:7], "%Y-%m").strftime("%b %Y")
    except ValueError:
        return text[:7]


def _trend_summary(trend: list[dict[str, Any]],
                   target: Any) -> str | None:
    """Management statement for the historical chart, or None if unplottable."""
    points = [row for row in trend if row.get("compliance_pct") is not None]
    if len(points) < 2:
        return None
    first, last = points[0], points[-1]
    start = float(first["compliance_pct"])
    end = float(last["compliance_pct"])
    change = end - start
    direction = "improved" if change >= 0 else "declined"
    statement = (
        f"Compliance {direction} from {_fmt_pct(start)} in "
        f"{_month_label(first.get('month'))} to {_fmt_pct(end)} in "
        f"{_month_label(last.get('month'))}, a change of "
        f"{abs(change):.1f} percentage points across {len(points)} recorded "
        f"months."
    )
    if target is not None:
        gap = end - float(target)
        position = "below" if gap < 0 else "above"
        statement += (
            f" The latest month is {abs(gap):.1f} percentage points "
            f"{position} the {_fmt_pct(target)} target."
        )
    return statement


def _group_table(rows: list[dict[str, Any]], label: str,
                 dimension: str | None = None) -> Table:
    data = [[label, "Total", "Compliant", "Non-compliant", "Unknown/Stale", "%"]]
    for row in rows[:15]:
        total = row.get("total_servers") or 0
        denom = (row.get("compliant", 0) + row.get("non_compliant", 0)
                 + row.get("unknown", 0) + row.get("stale", 0))
        pct = f"{(row.get('compliant', 0) / denom * 100):.1f}%" if denom else "n/a"
        data.append([
            _row_key(row, dimension)[:38], str(total),
            str(row.get("compliant", 0)), str(row.get("non_compliant", 0)),
            str((row.get("unknown") or 0) + (row.get("stale") or 0)), pct,
        ])
    if len(data) == 1:
        data.append(["No data captured for this dimension", "", "", "", "", ""])
    # Widths sum to the 174 mm text column (210 mm − 2 × 18 mm margins), so
    # the table shares the exact left and right edge of the headings and body.
    table = Table(data, colWidths=[58 * mm, 20 * mm, 24 * mm, 28 * mm,
                                   27 * mm, 17 * mm], repeatRows=1,
                  hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), BRAND_DARK),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5),
                ("FONT", (0, 1), (-1, -1), "Helvetica", 7.8),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [colors.white, colors.HexColor("#fcf8fd")]),
                ("GRID", (0, 0), (-1, -1), 0.7, GRID),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


SEVERITY_COLOURS = {
    "critical": colors.HexColor("#c2185b"),
    "important": colors.HexColor("#b45309"),
    "moderate": colors.HexColor("#8a6100"),
    "low": colors.HexColor("#0e7490"),
    "unknown": colors.HexColor("#5d5870"),
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
            strokeColor=BRAND_VIOLET, strokeWidth=1,
            strokeDashArray=[4, 3],
        ))
        drawing.add(String(
            chart.x + chart.width + 2, y - 3, f"Target {float(target):.0f}%",
            fontName="Helvetica", fontSize=6.5,
            fillColor=BRAND_VIOLET,
        ))
    return drawing


def _rule(width: float, colour=GRID, thickness: float = 0.8) -> Table:
    line = Table([[""]], colWidths=[width], rowHeights=[1])
    line.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, -1), thickness, colour)]))
    return line


def _cover_story(context: ReportContext,
                 styles: dict[str, ParagraphStyle]) -> list[Any]:
    """A cover that stands on its own: who issued it, what period it covers,
    and how it is classified — before any chart is read."""
    snap = context.snapshot
    prepared_name = _escape(context.preparer_name or context.generated_by)
    prepared_title = _escape(context.preparer_title or "Report preparer")
    default_brand = not (context.logo_data or context.logo_path)
    logo_width, logo_height = ((56 * mm, 56 * mm) if default_brand
                               else (58 * mm, 22 * mm))
    story: list[Any] = [
        Spacer(1, 6 * mm),
        _logo_flowable(context, logo_width, logo_height) or Spacer(1, 0),
        Spacer(1, 8 * mm),
        Paragraph(_escape(context.organization), styles["cover_org"]),
        Spacer(1, 3 * mm),
        Paragraph(_escape(context.title), styles["cover_title"]),
        Spacer(1, 2 * mm),
        Paragraph(_escape(context.period_label), styles["cover_period"]),
        Spacer(1, 8 * mm),
        _rule(150 * mm, BRAND_ACCENT, 1.2),
        Spacer(1, 6 * mm),
    ]

    meta = [
        ("Reporting period", _escape(context.period_label)),
        ("Organisation", _escape(context.organization)),
        ("Prepared by", f"{prepared_name} · {prepared_title}"),
        ("Generated", _escape(_generated_label(context))),
        ("Population", f"{snap.get('total_servers', 0)} servers in scope"),
        ("Classification", _escape(context.classification)),
    ]
    rows = [[Paragraph(label.upper(), styles["cover_label"]),
             Paragraph(value, styles["cover_value"])] for label, value in meta]
    table = Table(rows, colWidths=[44 * mm, 130 * mm], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, GRID),
    ]))
    story += [table, Spacer(1, 30 * mm)]

    if context.concur_name:
        story.append(Paragraph(
            f"Concurred by {_escape(context.concur_name)}"
            f"{f' · {_escape(context.concur_title)}' if context.concur_title else ''}. "
            f"Signed copy required.",
            styles["small"],
        ))
    else:
        story.append(Paragraph(
            "This report requires preparer and concurrence sign-off; the "
            "signature page is at the end of the document.",
            styles["small"],
        ))
    story += [
        Spacer(1, 3 * mm),
        Paragraph(
            "This document is a formal record of patch compliance at the "
            "reporting period end. It is not revised retroactively.",
            styles["small"],
        ),
        NextPageTemplate("rest"),
        PageBreak(),
    ]
    return story


def _signature_cell(role: str, name: str, title: str,
                    styles: dict[str, ParagraphStyle]) -> list[Any]:
    """One signature block. Blank fields are intentional: the printed copy is
    signed by hand, so a pre-filled name never substitutes for a signature."""
    return [
        Paragraph(role.upper(), styles["sign_role"]),
        Spacer(1, 4 * mm),
        _rule(72 * mm, MUTED, 0.6),
        Spacer(1, 2 * mm),
        Paragraph(_escape(name) or "Name", styles["body"]),
        Paragraph(_escape(title) or "Designation", styles["small"]),
        Spacer(1, 7 * mm),
        _rule(55 * mm, MUTED, 0.6),
        Spacer(1, 2 * mm),
        Paragraph("Signature", styles["small"]),
        Spacer(1, 5 * mm),
        _rule(45 * mm, MUTED, 0.6),
        Spacer(1, 2 * mm),
        Paragraph("Date", styles["small"]),
    ]


def _signoff_story(context: ReportContext,
                   styles: dict[str, ParagraphStyle]) -> list[Any]:
    blocks = Table(
        [[
            _signature_cell("Prepared by", context.preparer_name,
                            context.preparer_title, styles),
            _signature_cell("Concurred by", context.concur_name,
                            context.concur_title, styles),
        ]],
        colWidths=[80 * mm, 80 * mm], hAlign="LEFT",
    )
    blocks.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (0, 0), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
    ]))
    return [
        CondPageBreak(115 * mm),
        Paragraph("Approval and sign-off", styles["h1"]),
        Paragraph(
            f"The preparer confirms that this report was compiled from the "
            f"recorded compliance snapshots for {_escape(context.period_label)} in "
            f"accordance with the stated methodology. The concurring officer "
            f"confirms that the figures and conclusions are fit to be issued "
            f"to management and that exceptions remain owned and tracked.",
            styles["body"],
        ),
        Spacer(1, 10 * mm),
        Paragraph(_escape(context.organization), styles["sign_org"]),
        Paragraph(_escape(context.title), styles["small"]),
        Spacer(1, 6 * mm),
        blocks,
        Spacer(1, 4 * mm),
        Paragraph(
            f"Report reference: {_escape(context.period_label)} · "
            f"{_generated_label(context)} · "
            f"generated by {_escape(context.generated_by)}.",
            styles["small"],
        ),
        Paragraph(
            f"Classification: {_escape(context.classification)}. Retain the "
            f"signed copy with the report archive.",
            styles["small"],
        ),
    ]


def _toc() -> TableOfContents:
    toc = TableOfContents()
    toc.dotsMinLevel = 0
    toc.levelStyles = [
        ParagraphStyle(
            "TOCH1", fontName="Helvetica-Bold", fontSize=10, leading=20,
            textColor=BRAND_DARK, leftIndent=0, firstLineIndent=0,
            spaceBefore=2,
        ),
        ParagraphStyle(
            "TOCH2", fontName="Helvetica", fontSize=9, leading=15,
            textColor=MUTED, leftIndent=14, firstLineIndent=0,
        ),
    ]
    return toc


ABBREVIATIONS = [
    ("API", "Application Programming Interface"),
    ("CSV", "Comma-Separated Values"),
    ("CVE", "Common Vulnerabilities and Exposures"),
    ("FQDN", "Fully Qualified Domain Name"),
    ("KPI", "Key Performance Indicator"),
    ("OS", "Operating System"),
    ("PDF", "Portable Document Format"),
    ("RHEL", "Red Hat Enterprise Linux"),
    ("RHSA", "Red Hat Security Advisory"),
    ("SCCM", "Microsoft Configuration Manager (formerly System Center "
             "Configuration Manager)"),
    ("SLA", "Service Level Agreement — the remediation target for a finding"),
    ("TLS", "Transport Layer Security"),
    ("UTC", "Coordinated Universal Time"),
]


def _abbreviations_table() -> Table:
    rows = [["Abbreviation", "Meaning"]]
    rows += [[term, meaning] for term, meaning in ABBREVIATIONS]
    table = Table(rows, colWidths=[32 * mm, 142 * mm], repeatRows=1,
                  hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), BRAND_DARK),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5),
                ("FONT", (0, 1), (0, -1), "Helvetica-Bold", 8.2),
                ("FONT", (1, 1), (1, -1), "Helvetica", 8.2),
                ("GRID", (0, 0), (-1, -1), 0.7, GRID),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


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
    cover_frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width,
                        doc.height - 50 * mm, id="cover")
    doc.addPageTemplates(
        [
            PageTemplate(id="cover", frames=[cover_frame],
                         onPage=doc._draw_cover,
                         onPageEnd=doc._draw_cover_footer),
            PageTemplate(id="rest", frames=[frame],
                         onPage=doc._draw_header,
                         onPageEnd=doc._draw_footer),
        ]
    )

    story: list[Any] = _cover_story(context, styles)
    story += [
        Paragraph("Contents", styles["contents"]),
        _toc(),
        PageBreak(),
        Paragraph("Abbreviations and definitions", styles["h1"]),
        Paragraph(
            "The following abbreviations and product names appear in this "
            "report. Terms specific to a reporting period are defined where "
            "they first appear.",
            styles["body"],
        ),
        Spacer(1, 5),
        _abbreviations_table(),
        PageBreak(),
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
            f"{_escape(context.period_label)} is "
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
            Paragraph("<b>Target met</b> for the recorded period.",
                      styles["body"])
        )

    story += [Paragraph("Compliance overview", styles["h1"])]
    policy_target = snap.get("target_pct")
    for dimension, label, noun in (
        ("os_family", "Compliance by operating system", "operating system"),
        ("environment", "Compliance by environment", "environment"),
        ("business_group", "Compliance by business group", "business group"),
        ("application_group", "Compliance by application group",
         "application group"),
    ):
        rows = context.groups.get(dimension, [])
        # A heading, table and statement are one unit: splitting them across
        # pages leaves a lone heading or a final row under a repeated header.
        story.append(KeepTogether([
            Paragraph(label, styles["h2"]),
            _group_table(rows, "Group", dimension),
            Spacer(1, 4),
            Paragraph(_dimension_summary(rows, noun, policy_target, dimension),
                      styles["body"]),
        ]))
        story.append(Spacer(1, 7))

    # Let the remediation section follow the compliance tables on the same
    # page. It used to force a new page, leaving a large empty gap whenever the
    # last table ended early.
    story.append(CondPageBreak(60 * mm))
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
        data = [["Hostname", "OS", "Environment", "Business group", "Patches"]]
        for row in context.top_non_compliant[:15]:
            data.append([
                str(row.get("hostname", "")),
                f"{row.get('os_name', '')} {row.get('os_version', '')}".strip(),
                str(row.get("environment", "")),
                str(row.get("business_group", "")),
                str(row.get("total_patches") or 0),
            ])
        table = Table(data, colWidths=[40 * mm, 46 * mm, 24 * mm, 44 * mm,
                                        20 * mm], repeatRows=1, hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), BRAND_DARK),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5),
                    ("FONT", (0, 1), (-1, -1), "Helvetica", 7.8),
                    ("GRID", (0, 0), (-1, -1), 0.7, GRID),
                    ("ALIGN", (-1, 1), (-1, -1), "RIGHT"),
                ]
            )
        )
        story.append(KeepTogether([
            Paragraph("Servers with the most outstanding patches",
                      styles["h2"]),
            table,
            Spacer(1, 5),
            Paragraph(_top_servers_summary(context.top_non_compliant),
                      styles["body"]),
        ]))

    # Printed when the population contains a failing server. It shows both
    # what was justified and what was not, so a missing justification cannot be
    # mistaken for an omission from the report.
    if context.failing_servers:
        justified = [item for item in context.failing_servers
                     if item.get("justification_id")]
        unverified = [item for item in context.failing_servers
                      if not item.get("justification_id")]
        story.append(CondPageBreak(70 * mm))
        story += [Paragraph("Verified exceptions and remediation", styles["h1"])]
        story.append(Paragraph(
            f"{len(context.failing_servers)} server(s) are in a failing state "
            f"(non-compliant, stale or unknown). {len(justified)} carry a "
            f"recorded justification and {len(unverified)} do not. A "
            "justification explains a failing state; it does not change the "
            "state or the figures above, and every one of these servers remains "
            "in the failing counts.",
            styles["body"],
        ))
        story.append(Spacer(1, 5))

        story.append(Paragraph(
            f"Servers with a recorded justification ({len(justified)})",
            styles["h2"]))
        if justified:
            rows = [[
                Paragraph("Hostname", styles["cell_head"]),
                Paragraph("State", styles["cell_head"]),
                Paragraph("Reason", styles["cell_head"]),
                Paragraph("Justification statement and plan", styles["cell_head"]),
                Paragraph("Target", styles["cell_head"]),
                Paragraph("Evidence", styles["cell_head"]),
                Paragraph("Recorded by", styles["cell_head"]),
            ]]
            for item in justified:
                statement = _escape(str(item.get("statement", "")))
                plan = _escape(str(item.get("remediation_plan") or ""))
                if plan:
                    statement += f"<br/><b>Plan:</b> {plan}"
                evidence = item.get("evidence_names") or []
                rows.append([
                    Paragraph(_escape(str(item.get("hostname", ""))),
                              styles["cell"]),
                    Paragraph(_escape(str(item.get("state", ""))
                                      .replace("_", " ")), styles["cell"]),
                    Paragraph(_escape(str(item.get("reason_label")
                                          or item.get("reason_code") or "")),
                              styles["cell"]),
                    Paragraph(statement, styles["cell"]),
                    Paragraph(_fmt_date(item.get("remediation_target")),
                              styles["cell"]),
                    Paragraph(
                        _escape(", ".join(str(name) for name in evidence))
                        if evidence else "none attached",
                        styles["cell"]),
                    Paragraph(
                        _escape(str(item.get("created_by", ""))),
                        styles["cell"]),
                ])
            table = Table(rows, colWidths=[23 * mm, 18 * mm, 20 * mm,
                                           52 * mm, 18 * mm, 23 * mm, 20 * mm],
                          repeatRows=1, splitInRow=1, hAlign="LEFT")
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), BRAND_DARK),
                ("GRID", (0, 0), (-1, -1), 0.7, GRID),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]))
            story.append(table)
        else:
            story.append(Paragraph(
                "No failing server has a recorded justification.",
                styles["body"]))
        story.append(Spacer(1, 8))

        story.append(Paragraph(
            f"Servers without a justification ({len(unverified)})",
            styles["h2"]))
        if unverified:
            rows = [[
                Paragraph("Hostname", styles["cell_head"]),
                Paragraph("Operating system", styles["cell_head"]),
                Paragraph("Environment", styles["cell_head"]),
                Paragraph("Business service", styles["cell_head"]),
                Paragraph("State", styles["cell_head"]),
                Paragraph("Compliance reason", styles["cell_head"]),
                Paragraph("Patches", styles["cell_head"]),
            ]]
            for item in unverified:
                os_text = f"{item.get('os_name') or ''} " \
                          f"{item.get('os_version') or ''}".strip()
                rows.append([
                    Paragraph(_escape(str(item.get("hostname", ""))),
                              styles["cell"]),
                    Paragraph(_escape(os_text), styles["cell"]),
                    Paragraph(_escape(str(item.get("environment") or "")),
                              styles["cell"]),
                    Paragraph(_escape(str(item.get("business_group") or "")),
                              styles["cell"]),
                    Paragraph(_escape(str(item.get("state", ""))
                                      .replace("_", " ")), styles["cell"]),
                    Paragraph(_escape(str(item.get("compliance_reason") or "")),
                              styles["cell"]),
                    Paragraph(str(item.get("total_patches") or 0),
                              styles["cell"]),
                ])
            table = Table(rows, colWidths=[25 * mm, 34 * mm, 20 * mm, 26 * mm,
                                           17 * mm, 36 * mm, 16 * mm],
                          repeatRows=1, splitInRow=1, hAlign="LEFT")
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), BRAND_DARK),
                ("GRID", (0, 0), (-1, -1), 0.7, GRID),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]))
            story.append(table)
        else:
            story.append(Paragraph(
                "Every failing server has a recorded justification.",
                styles["body"]))

    story.append(CondPageBreak(70 * mm))
    story += [Paragraph("Historical analysis", styles["h1"])]
    if len(context.trend) >= 2:
        story.append(_trend_chart(
            context.trend, context.snapshot.get("target_pct")
        ))
        story.append(Spacer(1, 5))
        summary = _trend_summary(context.trend,
                                 context.snapshot.get("target_pct"))
        if summary:
            story.append(Paragraph(summary, styles["body"]))
            story.append(Spacer(1, 3))
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

    # System data integrity always starts a new page, and is kept with the
    # sign-off that follows it on that page.
    story.append(PageBreak())
    story += [
        Paragraph("System data integrity", styles["h1"]),
        Paragraph(
            "The figures in this report rest on complete and timely collection "
            "from every source system. This section records collection "
            "freshness and integration health, then states the methodology "
            "behind the compliance percentage.",
            styles["body"],
        ),
        Paragraph("Data quality and operational health", styles["h2"]),
        Paragraph(
            f"Inventory last synchronised: "
            f"{_fmt_ts(context.freshness.get('inventory_synced_at'), context.timezone)}. "
            f"Patch compliance last synchronised: "
            f"{_fmt_ts(context.freshness.get('patch_synced_at'), context.timezone)}.",
            styles["body"],
        ),
    ]
    if context.integration_health:
        data = [["Integration", "Status", "Last success", "Last result"]]
        for row in context.integration_health:
            data.append([
                str(row.get("key", "")),
                str(row.get("health", "")),
                _fmt_ts(row.get("last_success_at"), context.timezone),
                str((row.get("last_run") or {}).get("status", "never")),
            ])
        table = Table(data, colWidths=[42 * mm, 40 * mm, 58 * mm, 34 * mm],
                      repeatRows=1, hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), BRAND_DARK),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5),
                    ("FONT", (0, 1), (-1, -1), "Helvetica", 7.8),
                    ("GRID", (0, 0), (-1, -1), 0.7, GRID),
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

    story.append(KeepTogether([
        Paragraph("Methodology", styles["h2"]),
        Paragraph(
            f"<b>Reporting period:</b> {_escape(context.period_label)}<br/>"
            f"<b>Population:</b> "
            f"{_escape(str(methodology.get('population', 'in-scope assets')))}<br/>"
            f"<b>Numerator:</b> "
            f"{_escape(str(methodology.get('numerator', 'compliant assets')))}<br/>"
            f"<b>Denominator:</b> "
            f"{_escape(str(methodology.get('denominator', 'compliant + non-compliant + unknown + stale')))}<br/>"
            f"<b>Unknown treatment:</b> "
            f"{_escape(str(methodology.get('unknown_treatment', 'counted in denominator')))}<br/>"
            f"<b>Freshness threshold:</b> "
            f"{_escape(str(methodology.get('freshness_hours', 'n/a')))} hours<br/>"
            f"<b>Blocking severities:</b> "
            f"{_escape(', '.join(str(v) for v in methodology.get('blocking_severities') or [])) or 'n/a'}<br/>"
            f"<b>Policy:</b> "
            f"{_escape(str(methodology.get('policy_name', 'Baseline')))}",
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
    ]))
    story += _signoff_story(context, styles)
    # Two passes: headings are registered on the first, and the contents page
    # resolves their page numbers on the second.
    doc.multiBuild(story)
    return buffer.getvalue()


def _escape(value: str) -> str:
    """Escape operator-supplied text before it enters a Paragraph."""
    return _xml_escape(value or "")


def _fmt_date(value: Any) -> str:
    """A calendar date as DD/MM/YYYY, whether it arrives as a date or a string."""
    if not value:
        return "—"
    if hasattr(value, "strftime"):
        return value.strftime("%d/%m/%Y")
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return str(value)


def _fmt_ts(value: Any, timezone: str = "UTC") -> str:
    """The date of an instant in the deployment timezone, as DD/MM/YYYY.

    Sync and integration timestamps are stored in UTC. Converting them to the
    configured zone keeps a late-evening sync on the day it happened locally;
    the report shows dates only, so no time or zone abbreviation is printed.
    """
    if not value:
        return "never"
    if hasattr(value, "strftime"):
        try:
            zone = ZoneInfo(timezone)
        except Exception:  # noqa: BLE001 - unknown zone falls back to UTC
            zone = ZoneInfo("UTC")
        moment = value
        if moment.tzinfo is None:
            from datetime import UTC
            moment = moment.replace(tzinfo=UTC)
        local = moment.astimezone(zone)
        return f"{local:%d/%m/%Y}"
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
