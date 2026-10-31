"""One-page A4 landscape patch-transparency dashboard.

The console's Export Dashboard button produces a sheet for a management
audience. It is deliberately informational: it shows what is outstanding, how
old it is and where it sits, and stops short of recommending an action. The
five bands are:

1. a strip of six headline measures, each with a small proportion bar;
2. the compliance trend against the policy target, and the backlog split by
   severity and deadline state;
3. compliance against the target per environment, and the age profile of the
   overdue work;
4. the most widespread outstanding advisories, with host counts and lateness.

Everything is drawn on a ReportLab canvas rather than through Platypus: the
page is a fixed board, so absolute placement is simpler and cannot reflow onto
a second page. The caller passes the numbers the console is showing, so the
sheet cannot disagree with the screen; the SLA, ageing and advisory statistics
come from the compliance service's ``management`` block.
"""

from __future__ import annotations

import io
import math
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

PAGE = landscape(A4)

NAVY = colors.HexColor("#0d1b33")
INK = colors.HexColor("#1c2740")
MUTED = colors.HexColor("#6b7a99")
FAINT = colors.HexColor("#93a1bb")
LINE = colors.HexColor("#dfe6f2")
GRID = colors.HexColor("#edf1f8")
CARD = colors.white
TINT = colors.HexColor("#f5f7fc")
ACCENT = colors.HexColor("#4f7dff")
VIOLET = colors.HexColor("#7c5cff")
GREEN = colors.HexColor("#0f9d76")
RED = colors.HexColor("#d9204c")
AMBER = colors.HexColor("#d98a00")
SLATE = colors.HexColor("#8fa0b8")

SEVERITY_COLORS = {
    "critical": RED,
    "important": colors.HexColor("#e2701a"),
    "moderate": AMBER,
    "low": colors.HexColor("#3f7d5c"),
    "unknown": SLATE,
}
SEVERITY_ORDER = ("critical", "important", "moderate", "low", "unknown")

# Deadline state uses one colour job: overdue is a status, due-soon an
# approaching deadline, and on-track a neutral magnitude.
ON_TRACK = ACCENT
DUE_SOON = AMBER
OVERDUE = RED
UNDATED = SLATE

# Overdue age is an ordered ramp, oldest darkest, so the eye reads severity of
# age without a second legend.
AGE_COLORS = (colors.HexColor("#f4c7b5"), colors.HexColor("#e2701a"),
              colors.HexColor("#d9204c"), colors.HexColor("#8f1030"))
AGE_LABELS = ("1–7d", "8–30d", "31–90d", "90d+")

OS_LABELS = {"rhel": "RHEL", "windows": "Windows",
             "solaris": "Solaris", "other": "Other"}

# Status thresholds live in one place so a reader can audit what green means.
COVERAGE_GREEN = 98.0
COVERAGE_AMBER = 90.0
SLA_GREEN = 95.0
SLA_AMBER = 85.0
OVERDUE_RED_SHARE = 10.0
COMPLIANCE_AMBER_POINTS = 5.0

MARGIN = 22.0
HEADER_H = 54.0
FOOTER_Y = 28.0
FOOTER_TOP = 44.0          # lowest y the body may reach
HEADER_GAP = 10.0          # whitespace between the navy header and chapter 01

# Design heights, in points, for the body bands. They are the authority for
# what each band draws: a panel gets exactly the height reserved here, so type
# can never spill into an adjacent chapter strip.
STAGE_H = 12.0             # chapter strip
STAGE_GAP = 2.0            # strip sits this close to its band
SECTION_GAP = 8.0          # whitespace between one chapter and the next
BAND_HEIGHTS = (50.0, 124.0, 116.0, 114.0)
BODY_H = (sum(BAND_HEIGHTS) + 4 * STAGE_H + 4 * STAGE_GAP
          + 3 * SECTION_GAP)


def _num(value: Any) -> str:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return "—"
    return f"{number:,}"


def _pct(value: Any, digits: int = 1) -> str:
    try:
        return f"{float(value):.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _count(value: Any) -> int:
    try:
        return max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return 0


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fit(text: Any, font: str, size: float, width: float) -> str:
    """Trim with an ellipsis so a long label cannot cross a column rule."""
    text = str(text if text not in (None, "") else "—")
    if stringWidth(text, font, size) <= width:
        return text
    while text and stringWidth(text + "…", font, size) > width:
        text = text[:-1]
    return (text + "…") if text else "…"


def _wrap(text: str, font: str, size: float, width: float) -> list[str]:
    words = str(text).split()
    lines: list[str] = []
    line = ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if stringWidth(candidate, font, size) <= width:
            line = candidate
            continue
        if line:
            lines.append(line)
        line = word
    if line:
        lines.append(line)
    return lines


def _time_label(value: datetime, timezone: str) -> str:
    try:
        zone = ZoneInfo(timezone)
    except Exception:  # noqa: BLE001 - a bad stored name must not break the export
        zone = ZoneInfo("UTC")
    return value.astimezone(zone).strftime("%d %b %Y %H:%M")


def _panel(c: canvas.Canvas, x: float, y: float, w: float, h: float,
           title: str, hint: str = "") -> None:
    c.setFillColor(CARD)
    c.setStrokeColor(LINE)
    c.setLineWidth(0.7)
    c.roundRect(x, y, w, h, 6, stroke=1, fill=1)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 9.8)
    c.drawString(x + 12, y + h - 17, title)
    if hint:
        c.setFillColor(FAINT)
        c.setFont("Helvetica", 6.8)
        c.drawRightString(x + w - 12, y + h - 17, hint)


def _section_label(c: canvas.Canvas, text: str, x: float, y: float,
                   size: float = 6.4) -> None:
    c.setFillColor(FAINT)
    c.setFont("Helvetica-Bold", size)
    c.drawString(x, y, text.upper())


def _stage_bar(c: canvas.Canvas, x: float, y: float, w: float, number: str,
               title: str, detail: str) -> None:
    """A tinted chapter strip attached to the band it introduces.

    The strip is what makes ownership unambiguous: it touches the band below
    it and leaves real whitespace above, instead of floating as a line of text
    equidistant from two bands.
    """
    h = 12.0
    c.setFillColor(TINT)
    c.roundRect(x, y, w, h, 3, stroke=0, fill=1)
    c.setFillColor(ACCENT)
    c.rect(x, y, 2.5, h, stroke=0, fill=1)
    c.setFillColor(ACCENT)
    c.setFont("Helvetica-Bold", 7.2)
    c.drawString(x + 10, y + 4, number)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 8.0)
    c.drawString(x + 26, y + 4, title)
    offset = x + 26 + stringWidth(title, "Helvetica-Bold", 8.0) + 8
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 6.6)
    c.drawString(offset, y + 4, detail)


def _legend(c: canvas.Canvas, x: float, y: float, items: list[tuple[str, Any]],
            size: float = 6.2) -> None:
    c.setFont("Helvetica", size)
    for label, colour in items:
        c.setFillColor(colour)
        c.circle(x + 2.4, y + 2.2, 2.4, stroke=0, fill=1)
        c.setFillColor(MUTED)
        c.drawString(x + 8, y, label)
        x += 8 + stringWidth(label, "Helvetica", size) + 12


def _track(c: canvas.Canvas, x: float, y: float, w: float, h: float = 3.0,
           colour: Any = ACCENT, share: float = 1.0, radius: float = 1.5) -> None:
    c.setFillColor(LINE)
    c.roundRect(x, y, w, h, radius, stroke=0, fill=1)
    fill = max(0.0, min(1.0, share)) * w
    if fill > 0.4:
        c.setFillColor(colour)
        c.roundRect(x, y, max(0.4, fill), h, radius, stroke=0, fill=1)


# --------------------------------------------------------------------- status
def _rag_colour(status: str) -> colors.Color:
    return {"green": GREEN, "amber": AMBER, "red": RED}.get(status, SLATE)


def _compliance_status(pct: float | None, target: float | None) -> str:
    if pct is None:
        return "slate"
    if target is None:
        return "green" if pct >= 90 else "amber" if pct >= 80 else "red"
    if pct >= target:
        return "green"
    if pct >= target - COMPLIANCE_AMBER_POINTS:
        return "amber"
    return "red"


def _coverage_status(pct: float | None) -> str:
    if pct is None:
        return "slate"
    if pct >= COVERAGE_GREEN:
        return "green"
    return "amber" if pct >= COVERAGE_AMBER else "red"


def _sla_status(pct: float | None) -> str:
    if pct is None:
        return "slate"
    if pct >= SLA_GREEN:
        return "green"
    return "amber" if pct >= SLA_AMBER else "red"


def _overdue_status(overdue: int, total: int, critical_over_30: bool) -> str:
    if not overdue:
        return "green"
    if critical_over_30:
        return "red"
    if total and overdue / total * 100 > OVERDUE_RED_SHARE:
        return "red"
    return "amber"


# ------------------------------------------------------------------ analytics
def _coverage(payload: Any) -> float | None:
    total = _count(payload.kpis.total_servers)
    return _count(payload.kpis.in_scope) / total * 100 if total else None


def _management_rows(payload: Any) -> list[Any]:
    management = payload.management
    if management is None:
        return []
    return sorted(
        management.by_severity,
        key=lambda row: SEVERITY_ORDER.index(row.severity)
        if row.severity in SEVERITY_ORDER else len(SEVERITY_ORDER),
    )


def _advisories(payload: Any) -> list[tuple[str, Any]]:
    """Return up to six advisories, two per operating system.

    A mixed-platform top-N is dominated by whichever platform carries the most
    late findings, so the other platforms disappear. Faceting by platform keeps
    each one visible and comparable, and the label travels with the row so the
    reader always knows which patch line it belongs to.
    """
    management = payload.management
    if management is None:
        return []
    groups: dict[str, list[Any]] = {}
    for row in management.advisories:
        key = str(getattr(row, "os_family", "") or "").lower()
        groups.setdefault(key, []).append(row)

    os_order = {name: index for index, name in enumerate(OS_LABELS)}
    block_order = {name: index for index, name in enumerate(SEVERITY_ORDER)}
    ranked = []
    for key, rows in groups.items():
        rows.sort(key=lambda row: (
            -_count(row.affected_servers), -_count(row.overdue),
            -_count(row.open), -_count(row.oldest_days),
            block_order.get(str(row.severity), 9), str(row.advisory_id),
        ))
        chosen = rows[:2]
        label = OS_LABELS.get(key, key.capitalize() if key else "—")
        # Platforms are ordered by the exposure their shown rows represent, so
        # the largest platform leads without hiding the smaller ones.
        ranked.append((
            (-sum(_count(row.overdue) for row in chosen),
             -sum(_count(row.affected_servers) for row in chosen),
             os_order.get(key, len(OS_LABELS)), label),
            label, chosen,
        ))
    ranked.sort(key=lambda item: item[0])
    return [(label, row) for _, label, chosen in ranked[:3] for row in chosen]


def _overdue(payload: Any) -> int:
    management = payload.management
    if management is not None:
        return _count(management.overdue)
    return _count(payload.remediation.overdue) if payload.remediation else 0


def _total_open(payload: Any) -> int:
    management = payload.management
    if management is not None:
        return _count(management.total_open)
    return _count(payload.remediation.total) if payload.remediation else 0


def _undated(payload: Any) -> int:
    management = payload.management
    if management is not None:
        return _count(management.undated)
    return _count(payload.remediation.undated) if payload.remediation else 0


def _sla_adherence(payload: Any) -> float | None:
    measured = _total_open(payload) - _undated(payload)
    if measured <= 0:
        return None
    return max(0.0, measured - _overdue(payload)) / measured * 100


def _trend_stats(points: list[Any]) -> dict[str, Any]:
    values = [p for p in points if p.compliance_pct is not None]
    if not values:
        return {"count": 0, "delta": None, "at_target": None, "total": 0,
                "first": None, "last": None, "target": None}
    target = next((_number(p.target_pct) for p in values
                   if p.target_pct is not None), None)
    first = _number(values[0].compliance_pct)
    last = _number(values[-1].compliance_pct)
    at_target = None
    if target is not None:
        at_target = sum(
            1 for p in values
            if _number(p.compliance_pct) is not None
            and _number(p.compliance_pct) >= target
        )
    return {
        "count": len(values),
        "delta": (last - first) if first is not None and last is not None else None,
        "at_target": at_target,
        "total": len(values),
        "first": values[0].label,
        "last": last,
        "target": target,
    }


def _unit_overdue(payload: Any) -> dict[str, Any]:
    management = payload.management
    if management is None:
        return {}
    return {
        str(getattr(unit, "group_key", "") or getattr(unit, "group", "")).lower(): unit
        for unit in management.units
    }


# --------------------------------------------------------------------- bands
def _kpis(c: canvas.Canvas, payload: Any, y: float, w: float,
          h: float) -> None:
    kpis = payload.kpis
    management = payload.management
    pct = _number(kpis.compliance_pct)
    target = _number(kpis.target_pct)
    coverage = _coverage(payload)
    open_total = _total_open(payload)
    overdue = _overdue(payload)
    measured = max(0, open_total - _undated(payload))
    on_time = max(0, measured - overdue)
    sla = _sla_adherence(payload)
    critical = _count(kpis.critical_findings)
    critical_over_30 = 0
    if management is not None:
        critical_over_30 = (_count(management.overdue_age.days_31_90)
                            + _count(management.overdue_age.days_90_plus))

    tiles = [
        {
            "label": "Compliance", "value": _pct(pct),
            "sub": f"target {_pct(target, 0)}" if target is not None
            else "no target set",
            "status": _compliance_status(pct, target),
            "kind": "target", "share": (pct or 0) / 100.0, "target": target,
        },
        {
            "label": "Coverage", "value": _pct(coverage),
            "sub": f"{_num(kpis.in_scope)} of {_num(kpis.total_servers)} assessed",
            "status": _coverage_status(coverage),
            "kind": "fill", "share": (coverage or 0) / 100.0,
        },
        {
            "label": "Open findings", "value": _num(open_total),
            "sub": f"{_num(critical)} critical",
            "status": "red" if critical else "green",
            "kind": "severity", "share": 0,
        },
        {
            "label": "SLA on time", "value": _pct(sla),
            "sub": f"{_num(on_time)} of {_num(measured)} dated" if measured
            else "no dated findings",
            "status": _sla_status(sla),
            "kind": "split", "share": (sla or 0) / 100.0,
        },
        {
            "label": "Past due", "value": _num(overdue),
            "sub": (f"{_num(critical_over_30)} more than 30 days"
                    if critical_over_30 else "none beyond 30 days"),
            "status": _overdue_status(overdue, open_total, critical_over_30),
            "kind": "age", "share": overdue / open_total if open_total else 0.0,
        },
        {
            "label": "Data confidence", "value": _fresh_label(payload),
            "sub": (f"{_num(_count(kpis.unknown) + _count(kpis.stale))} "
                    "unknown or stale"),
            "status": "amber" if _count(kpis.stale) else "green",
            "kind": "fresh", "share": _fresh_share(payload),
        },
    ]

    gap = 8.0
    tile_w = (w - gap * (len(tiles) - 1)) / len(tiles)
    tile_h = h
    for index, tile in enumerate(tiles):
        x = MARGIN + index * (tile_w + gap)
        c.setFillColor(CARD)
        c.setStrokeColor(LINE)
        c.setLineWidth(0.7)
        c.roundRect(x, y, tile_w, tile_h, 6, stroke=1, fill=1)
        c.setFillColor(_rag_colour(tile["status"]))
        c.circle(x + tile_w - 10, y + tile_h - 10, 2.6, stroke=0, fill=1)
        _section_label(c, tile["label"], x + 10, y + tile_h - 13)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 18)
        c.drawString(x + 10, y + 20,
                     _fit(tile["value"], "Helvetica-Bold", 18, tile_w - 20))
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 6.2)
        c.drawString(x + 10, y + 8,
                     _fit(tile["sub"], "Helvetica", 6.2, tile_w - 18))
        _tile_bar(c, tile, payload, x + 10, y + 3.5, tile_w - 20)


def _fresh_share(payload: Any) -> float:
    freshness = payload.freshness
    total = _count(freshness.total_sources) if freshness is not None else 0
    if not total:
        return 1.0
    stale = _count(freshness.stale_sources)
    return max(0.0, (total - stale) / total)


def _fresh_label(payload: Any) -> str:
    freshness = payload.freshness
    total = _count(freshness.total_sources) if freshness is not None else 0
    if not total:
        return "—"
    return f"{_num(total - _count(freshness.stale_sources))}/{_num(total)}"


def _tile_bar(c: canvas.Canvas, tile: dict[str, Any], payload: Any,
              x: float, y: float, w: float) -> None:
    kind = tile["kind"]
    if kind == "target":
        _track(c, x, y, w, colour=ACCENT, share=tile["share"])
        target = tile["target"]
        if target is not None:
            tick_x = x + w * max(0.0, min(1.0, target / 100.0))
            c.setStrokeColor(INK)
            c.setLineWidth(0.7)
            c.line(tick_x, y - 1.5, tick_x, y + 4.5)
        return
    if kind == "split":
        c.setFillColor(LINE)
        c.roundRect(x, y, w, 3, 1.5, stroke=0, fill=1)
        share = max(0.0, min(1.0, tile["share"]))
        if share > 0:
            c.setFillColor(GREEN)
            c.roundRect(x, y, max(0.4, w * share), 3, 1.5, stroke=0, fill=1)
        if share < 1:
            c.setFillColor(RED)
            c.roundRect(x + w * share, y, max(0.4, w * (1 - share)), 3, 1.5,
                        stroke=0, fill=1)
        return
    if kind == "severity":
        rows = _management_rows(payload)
        total = sum(_count(row.open) for row in rows)
        if not total:
            _track(c, x, y, w, colour=SLATE, share=0)
            return
        cursor = x
        for index, row in enumerate(rows):
            share = _count(row.open) / total
            width = max(0.0, share * w - (0.8 if index < len(rows) - 1 else 0))
            c.setFillColor(SEVERITY_COLORS.get(row.severity, SLATE))
            c.rect(cursor, y, width, 3, stroke=0, fill=1)
            cursor += share * w
        return
    if kind == "age":
        age = payload.management.overdue_age if payload.management else None
        buckets = ([_count(age.days_1_7), _count(age.days_8_30),
                    _count(age.days_31_90), _count(age.days_90_plus)]
                   if age is not None else [0, 0, 0, 0])
        total = sum(buckets)
        if not total:
            _track(c, x, y, w, colour=GREEN, share=0)
            return
        cursor = x
        for index, value in enumerate(buckets):
            share = value / total
            width = max(0.0, share * w - (0.8 if index < 3 else 0))
            c.setFillColor(AGE_COLORS[index])
            c.rect(cursor, y, width, 3, stroke=0, fill=1)
            cursor += share * w
        return
    if kind == "fresh":
        _track(c, x, y, w, colour=GREEN, share=tile["share"])
        return
    _track(c, x, y, w, colour=ACCENT, share=tile["share"])


def _trend_panel(c: canvas.Canvas, x: float, y: float, w: float, h: float,
                 points: list[Any]) -> None:
    stats = _trend_stats(points)
    hint = ""
    if stats["delta"] is not None and stats["count"] > 1:
        hint = f"{stats['delta']:+.1f} pp since {str(stats['first'])[:12]}"
    _panel(c, x, y, w, h, "Compliance trend", hint)
    plot_x, plot_w = x + 34, w - 48
    plot_y, plot_h = y + 26, h - 64
    values = [p for p in points if p.compliance_pct is not None]
    target = stats.get("target")

    low, high = 0.0, 100.0
    if values:
        readings = [min(100.0, max(0.0, float(p.compliance_pct))) for p in values]
        if target is not None:
            readings.append(min(100.0, max(0.0, target)))
        low = max(0.0, math.floor((min(readings) - 4) / 10) * 10)
        high = 100.0

    def y_for(value: float) -> float:
        return plot_y + plot_h * (min(high, max(low, value)) - low) / (high - low)

    span = high - low
    step = 5 if span <= 15 else 10 if span <= 40 else 25
    level = low
    while level <= high + 0.01:
        gy = y_for(level)
        c.setStrokeColor(GRID)
        c.setLineWidth(0.6)
        c.line(plot_x, gy, plot_x + plot_w, gy)
        c.setFillColor(FAINT)
        c.setFont("Helvetica", 6.0)
        c.drawRightString(plot_x - 5, gy - 2, f"{level:g}%")
        level += step

    if not values:
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 8)
        c.drawString(x + 12, y + h / 2, "No recorded snapshots in this range.")
        return

    if target is not None:
        ty = y_for(high if target >= high else target)
        c.saveState()
        c.setStrokeColor(colors.HexColor("#b9c4d8"))
        c.setDash(3, 3)
        c.setLineWidth(0.7)
        c.line(plot_x, ty, plot_x + plot_w, ty)
        c.restoreState()
        c.setFillColor(FAINT)
        c.setFont("Helvetica", 6.0)
        c.drawRightString(plot_x + plot_w, ty + 3.5, f"target {target:g}%")

    count = len(values)
    step_x = plot_w / max(1, count - 1)
    coords = [
        (plot_x + (index * step_x if count > 1 else plot_w / 2),
         y_for(float(p.compliance_pct)))
        for index, p in enumerate(values)
    ]
    if len(coords) > 1:
        area = c.beginPath()
        area.moveTo(coords[0][0], plot_y)
        for px, py in coords:
            area.lineTo(px, py)
        area.lineTo(coords[-1][0], plot_y)
        area.close()
        c.saveState()
        c.setFillColor(ACCENT)
        c.setFillAlpha(0.10)
        c.drawPath(area, stroke=0, fill=1)
        c.restoreState()

        path = c.beginPath()
        path.moveTo(coords[0][0], coords[0][1])
        for px, py in coords[1:]:
            path.lineTo(px, py)
        c.setStrokeColor(ACCENT)
        c.setLineWidth(2)
        c.setLineJoin(1)
        c.drawPath(path)
    for px, py in coords:
        c.setFillColor(CARD)
        c.setStrokeColor(ACCENT)
        c.setLineWidth(1.3)
        c.circle(px, py, 2.3, stroke=1, fill=1)

    last_x, last_y = coords[-1]
    c.setFillColor(ACCENT)
    c.setFont("Helvetica-Bold", 7)
    c.drawCentredString(min(x + w - 22, max(x + 22, last_x)),
                        min(plot_y + plot_h + 2, last_y + 8),
                        _pct(values[-1].compliance_pct))

    stride = max(1, count // 7)
    c.setFillColor(FAINT)
    c.setFont("Helvetica", 6.0)
    for index, point in enumerate(values):
        if index % stride and index != count - 1:
            continue
        px = coords[index][0]
        label = _fit(point.label, "Helvetica", 6.0,
                     step_x * stride if count > 1 else plot_w)
        c.drawCentredString(px, plot_y - 11, label)


def _backlog_panel(c: canvas.Canvas, x: float, y: float, w: float, h: float,
                   payload: Any) -> None:
    _panel(c, x, y, w, h, "Backlog by severity", "current findings")
    _legend(c, x + 12, y + h - 31, [
        ("On track", ON_TRACK), ("Due ≤7d", DUE_SOON),
        ("Overdue", OVERDUE), ("No deadline", UNDATED),
    ])
    rows = _management_rows(payload)
    if not rows:
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 7.2)
        c.drawString(x + 12, y + h / 2,
                     "Finding detail is not recorded for this view.")
        return

    label_w, bar_w = 56.0, w - 196.0
    bar_x = x + 12 + label_w
    top = y + h - 47
    row_h = min(15.0, (top - y - 16) / max(1, len(rows)))
    maximum = max((_count(row.open) for row in rows), default=0) or 1
    for index, row in enumerate(rows):
        row_y = top - index * row_h
        if index:
            c.setStrokeColor(GRID)
            c.setLineWidth(0.5)
            c.line(x + 12, row_y + 11, x + w - 12, row_y + 11)
        colour = SEVERITY_COLORS.get(row.severity, SLATE)
        c.setFillColor(colour)
        c.circle(x + 15, row_y + 3, 2.4, stroke=0, fill=1)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 7.4)
        c.drawString(x + 21, row_y, str(row.severity).capitalize())
        open_count = _count(row.open)
        bar_total = bar_w * open_count / maximum
        segments = [
            (open_count - _count(row.overdue) - _count(row.due_soon)
             - _count(row.undated), ON_TRACK),
            (_count(row.due_soon), DUE_SOON),
            (_count(row.overdue), OVERDUE),
            (_count(row.undated), UNDATED),
        ]
        cursor = bar_x
        drawn = 0
        for seg_index, (value, seg_colour) in enumerate(segments):
            if value <= 0:
                continue
            width = bar_total * value / open_count
            if drawn:
                width = max(0.4, width - 1.2)
            c.setFillColor(seg_colour)
            c.rect(cursor, row_y, max(0.4, width), 8, stroke=0, fill=1)
            cursor += bar_total * value / open_count
            drawn += 1
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 7.4)
        c.drawRightString(x + w - 88, row_y + 1, _num(open_count))
        median = row.median_age_days
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 6.6)
        c.drawRightString(x + w - 12, row_y + 1,
                          f"med {float(median):.0f}d" if median is not None
                          else "med —")

    total = sum(_count(row.open) for row in rows)
    overdue = sum(_count(row.overdue) for row in rows)
    c.setFillColor(FAINT)
    c.setFont("Helvetica", 6.2)
    c.drawString(x + 12, y + 6,
                 f"{_num(total)} open · {_num(overdue)} past deadline "
                 f"({_pct(overdue / total * 100 if total else None)})")


def _compliance_panel(c: canvas.Canvas, x: float, y: float, w: float, h: float,
                      *, title: str, name_label: str, value_label: str,
                      entries: list[dict[str, Any]], target: float | None,
                      empty_text: str) -> None:
    """One compliance table: name, fleet size, RAG bar, and an exposure count."""
    _panel(c, x, y, w, h, title,
           f"target {_pct(target, 0)}" if target is not None else "")
    header_y = y + h - 33
    wide = w >= 360
    name_w = 96 if wide else 84
    bar_w = 130 if wide else 88
    cols = {
        "name": x + 12,
        "servers": x + (130 if wide else 106),
        "bar": x + (140 if wide else 114),
        "pct": x + (278 if wide else 210),
        "value": x + w - 12,
    }
    _section_label(c, name_label, cols["name"], header_y)
    c.setFillColor(FAINT)
    c.setFont("Helvetica-Bold", 6.4)
    c.drawRightString(cols["servers"], header_y, "SERVERS")
    _section_label(c, "Compliance", cols["pct"], header_y)
    c.setFillColor(FAINT)
    c.setFont("Helvetica-Bold", 6.4)
    c.drawRightString(cols["value"], header_y, value_label.upper())
    c.setStrokeColor(LINE)
    c.setLineWidth(0.6)
    c.line(x + 12, header_y - 5, x + w - 12, header_y - 5)

    if not entries:
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 7.2)
        c.drawString(x + 12, header_y - 20, empty_text)
        return

    top = header_y - 18
    row_step = min(14.5, (top - y - 10) / max(1, len(entries)))
    for index, entry in enumerate(entries):
        row_y = top - index * row_step
        if index % 2:
            c.setFillColor(TINT)
            c.rect(x + 8, row_y - 4, w - 16, max(9.0, row_step - 1),
                   stroke=0, fill=1)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 7.3)
        c.drawString(cols["name"], row_y,
                     _fit(entry["group"], "Helvetica-Bold", 7.3, name_w))
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 7.1)
        c.drawRightString(cols["servers"], row_y, _num(entry["servers"]))
        if entry["pct"] is not None:
            status = _compliance_status(entry["pct"], target)
            _track(c, cols["bar"], row_y + 1.5, bar_w, 3.5,
                   colour=_rag_colour(status), share=entry["pct"] / 100)
            c.setFillColor(INK)
            c.setFont("Helvetica", 7.1)
            c.drawString(cols["pct"], row_y, _pct(entry["pct"]))
        else:
            c.setFillColor(FAINT)
            c.setFont("Helvetica", 7.1)
            c.drawString(cols["pct"], row_y, "not assessed")
        value = entry["value"]
        c.setFillColor(RED if value else MUTED)
        c.setFont("Helvetica-Bold" if value else "Helvetica", 7.1)
        c.drawRightString(cols["value"], row_y, _num(value) if value else "—")


def _environment_panel(c: canvas.Canvas, x: float, y: float, w: float, h: float,
                       payload: Any) -> None:
    target = _number(payload.kpis.target_pct)
    overdue_by_unit = _unit_overdue(payload)
    entries = []
    for row in payload.by_environment or []:
        in_scope = _count(row.in_scope)
        pct = (_count(row.compliant) / in_scope * 100) if in_scope else None
        unit = overdue_by_unit.get(str(row.group).lower())
        entries.append({
            "group": row.group, "servers": _count(row.total_servers),
            "pct": pct,
            "value": _count(unit.overdue) if unit is not None else 0,
            "rank": _count(unit.overdue) if unit is not None else 0,
        })
    entries.sort(key=lambda item: (
        -(item["rank"] or 0), -(item["servers"] or 0), str(item["group"])))
    _compliance_panel(
        c, x, y, w, h, title="Compliance by environment",
        name_label="Environment", value_label="Overdue",
        entries=entries[:6], target=target,
        empty_text="No environment breakdown on this view.")


def _business_panel(c: canvas.Canvas, x: float, y: float, w: float, h: float,
                    payload: Any) -> None:
    target = _number(payload.kpis.target_pct)
    entries = []
    for row in payload.by_business_group or []:
        in_scope = _count(row.in_scope)
        pct = (_count(row.compliant) / in_scope * 100) if in_scope else None
        non_compliant = _count(row.non_compliant)
        entries.append({
            "group": row.group, "servers": _count(row.total_servers),
            "pct": pct, "value": non_compliant, "rank": non_compliant,
        })
    entries.sort(key=lambda item: (
        -(item["rank"] or 0), -(item["servers"] or 0), str(item["group"])))
    _compliance_panel(
        c, x, y, w, h, title="Compliance by business service",
        name_label="Business service", value_label="Non-compliant",
        entries=entries[:6], target=target,
        empty_text="No business-service breakdown on this view.")


def _os_panel(c: canvas.Canvas, x: float, y: float, w: float, h: float,
              payload: Any) -> None:
    target = _number(payload.kpis.target_pct)
    entries = []
    for row in payload.by_os or []:
        in_scope = _count(row.in_scope)
        pct = (_count(row.compliant) / in_scope * 100) if in_scope else None
        non_compliant = _count(row.non_compliant)
        entries.append({
            "group": OS_LABELS.get(str(row.group), str(row.group).capitalize()),
            "servers": _count(row.total_servers), "pct": pct,
            "value": non_compliant, "rank": non_compliant,
        })
    entries.sort(key=lambda item: (-item["rank"], -item["servers"],
                                   str(item["group"])))
    _compliance_panel(
        c, x, y, w, h, title="Compliance by operating system",
        name_label="OS", value_label="N/C",
        entries=entries[:6], target=target,
        empty_text="No operating-system breakdown on this view.")


def _advisories_panel(c: canvas.Canvas, x: float, y: float, w: float, h: float,
                      payload: Any) -> None:
    rows = _advisories(payload)
    _panel(c, x, y, w, h, "Most widespread outstanding advisories",
           "2 per operating system · by servers affected")
    header_y = y + h - 33
    cols = {
        "os": x + 12,
        "id": x + 44,
        "title": x + 112,
        "sev": x + w - 250,
        "servers": x + w - 126,
        "open": x + w - 86,
        "overdue": x + w - 50,
        "oldest": x + w - 12,
    }
    _section_label(c, "OS", cols["os"], header_y)
    _section_label(c, "Advisory", cols["id"], header_y)
    _section_label(c, "Title", cols["title"], header_y)
    _section_label(c, "Severity", cols["sev"], header_y)
    for key, label in (("servers", "Servers"), ("open", "Open"),
                       ("overdue", "Overdue"), ("oldest", "Oldest")):
        c.setFillColor(FAINT)
        c.setFont("Helvetica-Bold", 6.4)
        c.drawRightString(cols[key], header_y, label.upper())
    c.setStrokeColor(LINE)
    c.setLineWidth(0.6)
    c.line(x + 12, header_y - 5, x + w - 12, header_y - 5)

    if not rows:
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 7.2)
        c.drawString(x + 12, header_y - 20,
                     "No outstanding advisories on this view.")
        return

    top = header_y - 17
    previous_label = None
    for index, (os_label, row) in enumerate(rows):
        row_y = top - index * 9.8
        if index % 2:
            c.setFillColor(TINT)
            c.rect(x + 8, row_y - 3, w - 16, 9.8, stroke=0, fill=1)
        # The platform is labelled once per block; the row beneath it belongs
        # to the same operating system.
        if os_label != previous_label:
            c.setFillColor(INK)
            c.setFont("Helvetica-Bold", 6.8)
            c.drawString(cols["os"], row_y,
                         _fit(os_label, "Helvetica-Bold", 6.8, 32))
            previous_label = os_label
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 7.0)
        c.drawString(cols["id"], row_y,
                     _fit(row.advisory_id, "Helvetica-Bold", 7.0, 62))
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 7.0)
        c.drawString(cols["title"], row_y,
                     _fit(row.title or row.advisory_id, "Helvetica", 7.0,
                          cols["sev"] - cols["title"] - 10))
        colour = SEVERITY_COLORS.get(row.severity, SLATE)
        c.setFillColor(colour)
        c.circle(cols["sev"] + 2, row_y + 2, 2.2, stroke=0, fill=1)
        c.setFillColor(INK)
        c.setFont("Helvetica", 7.0)
        c.drawString(cols["sev"] + 9, row_y,
                     _fit(str(row.severity).capitalize(), "Helvetica", 7.0, 54))
        c.setFillColor(MUTED)
        c.drawRightString(cols["servers"], row_y, _num(row.affected_servers))
        c.drawRightString(cols["open"], row_y, _num(row.open))
        c.setFillColor(RED if _count(row.overdue) else MUTED)
        c.setFont("Helvetica-Bold" if _count(row.overdue) else "Helvetica", 7.0)
        c.drawRightString(cols["overdue"], row_y,
                          _num(row.overdue) if _count(row.overdue) else "—")
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 7.0)
        c.drawRightString(cols["oldest"], row_y,
                          f"{_num(row.oldest_days)}d"
                          if _count(row.oldest_days) else "—")


# --------------------------------------------------------------------- render
def render_management_pdf(
    payload: Any,
    *,
    organization: str = "PatchTrack",
    generated_at: datetime | None = None,
    generated_by: str | None = None,
    timezone: str = "UTC",
    logo_data: bytes | None = None,
    logo_path: str | None = None,
) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=PAGE)
    c.setTitle("PatchTrack Management Summary")
    c.setAuthor("PatchTrack")
    c.setSubject("Patch management transparency summary")
    width, height = PAGE

    c.setFillColor(NAVY)
    c.rect(0, height - HEADER_H, width, HEADER_H, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 16.5)
    c.drawString(MARGIN, height - 25,
                 "Patch Management — Transparency Dashboard")
    c.setFillColor(colors.HexColor("#aebdd8"))
    c.setFont("Helvetica", 8.2)
    c.drawString(MARGIN, height - 41, _fit(payload.scope_label, "Helvetica", 8.2, 440))

    _draw_logo(c, logo_data, logo_path, width - MARGIN - 178, height - 44, 20)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 9)
    c.drawRightString(width - MARGIN, height - 22,
                      _fit(organization, "Helvetica-Bold", 9, 132))
    c.setFillColor(colors.HexColor("#aebdd8"))
    c.setFont("Helvetica", 7)
    stamp = _time_label(generated_at or datetime.now().astimezone(), timezone)
    c.drawRightString(width - MARGIN, height - 34, stamp)
    if generated_by:
        c.drawRightString(width - MARGIN, height - 45,
                          _fit(f"Prepared by {generated_by}", "Helvetica", 7, 160))

    content_w = width - 2 * MARGIN
    body_top = height - HEADER_H - HEADER_GAP
    available = body_top - FOOTER_TOP
    # Safety net: if the design needs more room than the sheet has, shrink the
    # whole body uniformly (which also shrinks every font) rather than letting
    # anything overlap. Normally the design fits and the scale stays at 1.
    scale = min(1.0, available / BODY_H)
    c.saveState()
    if scale < 1.0:
        c.translate((width - width * scale) / 2,
                    body_top * (1.0 - scale))
        c.scale(scale, scale)

    def stage(y: float, number: str, title: str, detail: str) -> float:
        _stage_bar(c, MARGIN, y - STAGE_H, content_w, number, title, detail)
        return y - STAGE_H - STAGE_GAP

    def band(top: float, index: int) -> float:
        return top - BAND_HEIGHTS[index]

    y = stage(body_top, "01", "Where the estate stands",
              "assessed compliance, coverage and current exposure")
    kpi_y = band(y, 0)
    _kpis(c, payload, kpi_y, content_w, BAND_HEIGHTS[0])
    y = kpi_y - SECTION_GAP

    y = stage(y, "02", "How the position is moving",
              "trend against target, and the state of the open backlog")
    trend_y = band(y, 1)
    trend_w = 400.0
    _trend_panel(c, MARGIN, trend_y, trend_w, BAND_HEIGHTS[1],
                 list(payload.trend or []))
    _backlog_panel(c, MARGIN + trend_w + 12, trend_y,
                   content_w - trend_w - 12, BAND_HEIGHTS[1], payload)
    y = trend_y - SECTION_GAP

    y = stage(y, "03", "Where exposure concentrates",
              "compliance by environment and by business service")
    env_y = band(y, 2)
    env_w = 393.0
    _environment_panel(c, MARGIN, env_y, env_w, BAND_HEIGHTS[2], payload)
    business_x = MARGIN + env_w + 12
    _business_panel(c, business_x, env_y, content_w - env_w - 12,
                    BAND_HEIGHTS[2], payload)
    y = env_y - SECTION_GAP

    y = stage(y, "04", "What remains to be patched",
              "operating systems, then the most widespread advisories")
    advisory_y = band(y, 3)
    os_w = 300.0
    _os_panel(c, MARGIN, advisory_y, os_w, BAND_HEIGHTS[3], payload)
    _advisories_panel(c, MARGIN + os_w + 12, advisory_y,
                      content_w - os_w - 12, BAND_HEIGHTS[3], payload)
    c.restoreState()

    c.setStrokeColor(LINE)
    c.setLineWidth(0.6)
    c.line(MARGIN, 38, width - MARGIN, 38)
    c.setFillColor(FAINT)
    c.setFont("Helvetica", 6.4)
    kpis = payload.kpis
    coverage = _coverage(payload)
    footer = (f"Generated {stamp} · {payload.scope_label} · "
              f"confidence: {_pct(coverage) if coverage is not None else '—'} assessed, "
              f"{_num(_count(kpis.unknown) + _count(kpis.stale))} unknown/stale, "
              f"{_num(kpis.excluded)} excluded")
    freshness = payload.freshness
    if freshness is not None and _count(freshness.total_sources):
        fresh = _count(freshness.total_sources) - _count(freshness.stale_sources)
        footer += f" · data {fresh}/{_count(freshness.total_sources)} sources fresh"
    c.drawString(MARGIN, FOOTER_Y, _fit(footer, "Helvetica", 6.4, content_w - 250))
    c.drawRightString(width - MARGIN, FOOTER_Y,
                      "CONFIDENTIAL — INTERNAL USE ONLY · Page 1 of 1")

    c.showPage()
    c.save()
    return buffer.getvalue()


def _draw_logo(c: canvas.Canvas, data: bytes | None, path: str | None,
               x: float, y: float, max_h: float) -> float:
    """Draw the organisation mark if it decodes; return the width consumed."""
    reader = None
    if data:
        try:
            reader = ImageReader(io.BytesIO(data))
        except Exception:  # noqa: BLE001 - a bad logo must not fail the export
            reader = None
    if reader is None and path:
        try:
            reader = ImageReader(path)
        except Exception:  # noqa: BLE001
            reader = None
    if reader is None:
        return 0.0
    try:
        img_w, img_h = reader.getSize()
    except Exception:  # noqa: BLE001
        return 0.0
    if not img_w or not img_h:
        return 0.0
    scale = max_h / float(img_h)
    draw_w = float(img_w) * scale
    try:
        c.drawImage(reader, x, y, width=draw_w, height=max_h,
                    preserveAspectRatio=True, anchor="ne", mask="auto")
    except Exception:  # noqa: BLE001
        return 0.0
    return draw_w
