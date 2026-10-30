/* PatchTrack chart primitives — dependency-free SVG.
 *
 * Rules baked in:
 *  - 2px surface gap between stacked segments (drawn as stroke in surface colour)
 *  - 4px rounded data-end, square at the baseline
 *  - hairline solid gridlines, recessive
 *  - legend for every multi-series chart; identity never colour-alone
 *  - one value axis; no dual axes anywhere
 *  - hover tooltip on every mark
 */

const SURFACE = "#0f0f1c";
const INK = "#f5f2ff";
const INK_2 = "#c3bfe0";
const MUTED = "#918dae";
const BORDER = "#2c2b4d";
const ACCENT = "#ff2d95";

export const STATUS = {
  compliant: "#22e6a6",
  non_compliant: "#ff3b6b",
  unknown: "#ffb020",
  stale: "#8d8aa8",
  excluded: "#4b4a63",
  critical: "#ff3b6b",
};

export const SEVERITY = {
  critical: "#ff2d6f",
  important: "#ff8a3d",
  moderate: "#ffd23f",
  low: "#38c6ff",
  unknown: "#8d8aa8",
};

const NS = "http://www.w3.org/2000/svg";

function el(tag, attrs = {}, text) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined) continue;
    node.setAttribute(k, String(v));
  }
  if (text !== undefined) node.textContent = text;
  return node;
}

/* Chart marks are painted with a vertical tint rather than a flat fill: the
   loud end sits at the data end, the deep end at the baseline. It gives the
   neon console theme its signature glowing bars without making every series
   unreadable. */
function shade(hex, toward, amount) {
  const value = parseInt(hex.slice(1), 16);
  const target = toward === "white" ? 255 : 0;
  const channels = [(value >> 16) & 255, (value >> 8) & 255, value & 255]
    .map((c) => Math.round(c + (target - c) * amount));
  return `#${channels.map((c) => c.toString(16).padStart(2, "0")).join("")}`;
}

let gradientSeq = 0;

function gradientFactory(svg, vertical = true) {
  const cache = new Map();
  return (color) => {
    if (cache.has(color)) return cache.get(color);
    const id = `pt-grad-${gradientSeq += 1}`;
    const grad = el("linearGradient", {
      id,
      x1: "0", y1: "0",
      x2: vertical ? "0" : "1", y2: vertical ? "1" : "0",
    });
    grad.appendChild(el("stop", {
      offset: "0%", "stop-color": shade(color, "white", 0.34),
    }));
    grad.appendChild(el("stop", {
      offset: "100%", "stop-color": shade(color, "black", 0.12),
    }));
    const defs = el("defs");
    defs.appendChild(grad);
    svg.appendChild(defs);
    const value = `url(#${id})`;
    cache.set(color, value);
    return value;
  };
}

function niceMax(value) {
  if (!value || value <= 0) return 10;
  const exp = Math.floor(Math.log10(value));
  const base = 10 ** exp;
  const norm = value / base;
  const step = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10;
  return step * base;
}

export function compact(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
  const v = Number(n);
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (Math.abs(v) >= 1e4) return `${(v / 1e3).toFixed(1)}K`;
  return v.toLocaleString();
}

export function pct(n, digits = 1) {
  if (n === null || n === undefined) return "n/a";
  return `${Number(n).toFixed(digits)}%`;
}

/* ------------------------------------------------------------------ tooltip */
let tipEl = null;

function tooltip() {
  if (!tipEl) {
    tipEl = document.createElement("div");
    tipEl.className = "tooltip";
    tipEl.setAttribute("role", "tooltip");
    tipEl.style.display = "none";
    document.body.appendChild(tipEl);
  }
  return tipEl;
}

function renderTip(content) {
  const tip = tooltip();
  tip.replaceChildren();
  if (content.title !== undefined) {
    const title = document.createElement("strong");
    // Tooltip content includes asset and organisation names from the database;
    // text nodes keep them inert instead of parsing them as markup.
    title.textContent = String(content.title);
    tip.appendChild(title);
  }
  for (const row of content.rows || []) {
    const line = document.createElement("div");
    line.className = "tt-row";
    const left = document.createElement("span");
    if (row.color) {
      const swatch = document.createElement("span");
      swatch.className = "legend-swatch";
      swatch.style.display = "inline-block";
      swatch.style.background = String(row.color);
      left.append(swatch, document.createTextNode(" "));
    }
    left.append(document.createTextNode(String(row.label)));
    const value = document.createElement("strong");
    value.textContent = String(row.value);
    line.append(left, value);
    tip.appendChild(line);
  }
  return tip;
}

function showTip(content, event) {
  const tip = renderTip(content);
  tip.style.display = "block";
  const pad = 14;
  const rect = tip.getBoundingClientRect();
  let x = event.clientX + pad;
  let y = event.clientY + pad;
  if (x + rect.width > window.innerWidth - 8) x = event.clientX - rect.width - pad;
  if (y + rect.height > window.innerHeight - 8) y = event.clientY - rect.height - pad;
  tip.style.left = `${Math.max(8, x)}px`;
  tip.style.top = `${Math.max(8, y)}px`;
}

function hideTip() {
  if (tipEl) tipEl.style.display = "none";
}

function bindTip(node, content) {
  node.addEventListener("mouseenter", (e) => showTip(content, e));
  node.addEventListener("mousemove", (e) => showTip(content, e));
  node.addEventListener("mouseleave", hideTip);
  node.setAttribute("tabindex", "0");
  node.addEventListener("focus", (e) => {
    const box = node.getBoundingClientRect();
    showTip(content, { clientX: box.left + box.width / 2, clientY: box.top });
  });
  node.addEventListener("blur", hideTip);
}

function tipRow(label, value, color) {
  return { label, value, color };
}

function tipBody(title, rows) {
  return { title, rows: rows.filter(Boolean) };
}

export function legend(container, entries) {
  container.innerHTML = "";
  for (const entry of entries) {
    const span = document.createElement("span");
    span.className = "legend-item";
    const swatch = document.createElement("span");
    swatch.className = "legend-swatch";
    swatch.style.background = String(entry.color);
    span.append(swatch, document.createTextNode(String(entry.label)));
    container.appendChild(span);
  }
}

function emptyState(container, message) {
  container.replaceChildren();
  const node = document.createElement("div");
  node.className = "chart-empty";
  node.textContent = message;
  container.appendChild(node);
}

/* Render at the pane's own width. A fixed 640-unit viewBox scaled 11px labels
   down to about 6px on a phone (and to 7px in a desktop card); matching the
   container keeps text at its real size. */
/* Views are built detached and mounted in one step. A chart cannot measure its
   pane until then, so it queues its draw and mount() flushes the queue. */
const pendingDraws = new Set();
let flushScheduled = false;

function queueChartDraw(container, draw) {
  pendingDraws.add({ container, draw });
  if (!flushScheduled) {
    flushScheduled = true;
    requestAnimationFrame(() => {
      flushScheduled = false;
      flushCharts();
    });
  }
}

/* An unrelated mount (a spinner, another route) must not discard a chart that
   has not reached the document yet: keep unconnected containers queued. */
export function flushCharts() {
  for (const entry of [...pendingDraws]) {
    if (!entry.container.isConnected) continue;
    pendingDraws.delete(entry);
    entry.draw();
  }
}

function widthFor(container) {
  const measured = Math.round(container.getBoundingClientRect().width);
  return Math.max(300, Math.min(720, measured || 640));
}

function frame(container, height, label, width = 640) {
  container.innerHTML = "";
  const svg = el("svg", {
    class: "chart",
    viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: "none",
    role: "img",
  });
  if (label) svg.setAttribute("aria-label", label);
  svg.style.height = `${height}px`;
  container.appendChild(svg);
  return svg;
}

/* ------------------------------------------------------------------ stacked columns */
export function stackedColumns(container, options) {
  // Views are built detached and mounted in one step. A detached element has
  // no width to measure, so wait one frame for the mount before drawing.
  if (!container.isConnected) {
    queueChartDraw(container, () => {
      if (container.isConnected) stackedColumns(container, options);
    });
    return;
  }
  const {
    rows, keys, labels, colors, height = 220,
    valueLabel = "servers", showTotals = true, onSelect,
    label = "Server counts by group, split by compliance state",
  } = options;
  if (!rows || rows.length === 0) {
    emptyState(container, "No servers match the current filters.");
    return;
  }
  const width = widthFor(container);
  const pad = { top: 22, right: 12, bottom: 34, left: 44 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const totals = rows.map((r) => keys.reduce((sum, k) => sum + (r[k] || 0), 0));
  const max = niceMax(Math.max(...totals));
  const svg = frame(container, height, label, width);
  const fill = gradientFactory(svg);

  for (let i = 0; i <= 4; i += 1) {
    const value = (max / 4) * i;
    const y = pad.top + plotH - (value / max) * plotH;
    svg.appendChild(el("line", {
      x1: pad.left, x2: width - pad.right, y1: y, y2: y, class: "gridline",
    }));
    svg.appendChild(el("text", {
      x: pad.left - 8, y: y + 4, "text-anchor": "end", class: "axis-tick",
    }, compact(value)));
  }

  const band = plotW / rows.length;
  const barW = Math.min(44, band * 0.52);
  rows.forEach((row, index) => {
    const cx = pad.left + band * index + band / 2;
    const total = totals[index];
    let cursor = pad.top + plotH;
    const segments = keys
      .map((key) => ({ key, value: row[key] || 0 }))
      .filter((s) => s.value > 0);

    segments.forEach((segment, segIndex) => {
      const h = (segment.value / max) * plotH;
      const isTop = segIndex === segments.length - 1;
      const y = cursor - h;
      // 2px surface gap separates touching segments; 4px round on the data end only.
      const rect = el("rect", {
        x: cx - barW / 2,
        y: isTop ? y : y + 1,
        width: barW,
        height: Math.max(1, h - (isTop ? 0 : 1)),
        fill: fill(colors[segment.key]),
        rx: isTop ? 4 : 0,
        ry: isTop ? 4 : 0,
      });
      bindTip(rect, tipBody(row.group, [
        ...keys.map((k) =>
          tipRow(labels[k], compact(row[k] || 0), colors[k])),
        tipRow(valueLabel, compact(total)),
      ]));
      if (onSelect) {
        rect.style.cursor = "pointer";
        rect.addEventListener("click", () => onSelect(row));
      }
      svg.appendChild(rect);
      if (isTop && h > 14) {
        svg.appendChild(el("rect", {
          x: cx - barW / 2, y: y + h - 4, width: barW, height: 4,
          fill: colors[segment.key],
        }));
      }
      cursor = y;
    });

    if (showTotals) {
      svg.appendChild(el("text", {
        x: cx, y: pad.top + plotH - (total / max) * plotH - 7,
        "text-anchor": "middle", class: "axis-tick",
        style: `fill:${INK};font-weight:600`,
      }, compact(total)));
    }
    // Wide enough for the longest platform name ("Microsoft Windows"); the
    // old 16-character cut rendered it as "Microsoft Windo…".
    const label = String(row.group).length > 24
      ? `${String(row.group).slice(0, 23)}…` : row.group;
    const groupText = el("text", {
      x: cx, y: height - 12, "text-anchor": "middle", class: "axis-label",
    }, label);
    if (onSelect) {
      groupText.style.cursor = "pointer";
      groupText.addEventListener("click", () => onSelect(row));
    }
    svg.appendChild(groupText);
  });
}

/* ------------------------------------------------------------------ stacked rows */
export function stackedRows(container, options) {
  if (!container.isConnected) {
    queueChartDraw(container, () => {
      if (container.isConnected) stackedRows(container, options);
    });
    return;
  }
  const {
    rows, keys, labels, colors, maxRows = 7, withPct = false, onSelect,
    label = "Server counts by group, split by compliance state",
  } = options;
  if (!rows || rows.length === 0) {
    emptyState(container, "No servers match the current filters.");
    return;
  }
  const shown = rows.slice(0, maxRows);
  const rowH = 30;
  const height = shown.length * rowH + 8;
  const width = widthFor(container);
  // The group labels need room; on a narrow pane trade plot width for them.
  const pad = { left: 148, right: withPct ? 66 : 46 };
  const plotW = width - pad.left - pad.right;
  const max = Math.max(
    1, ...shown.map((r) => keys.reduce((s, k) => s + (r[k] || 0), 0))
  );
  const svg = frame(container, height, label, width);
  const fill = gradientFactory(svg, false);

  shown.forEach((row, index) => {
    const y = index * rowH + 6;
    const total = keys.reduce((s, k) => s + (row[k] || 0), 0);
    const groupLabel = String(row.group).length > 22
      ? `${String(row.group).slice(0, 21)}…` : row.group;
    const groupText = el("text", {
      x: pad.left - 10, y: y + 15, "text-anchor": "end", class: "axis-label",
      style: `fill:${INK_2}`,
    }, groupLabel);
    if (onSelect) {
      groupText.style.cursor = "pointer";
      groupText.addEventListener("click", () => onSelect(row));
    }
    svg.appendChild(groupText);

    const fullW = (total / max) * plotW;
    svg.appendChild(el("rect", {
      x: pad.left, y, width: fullW, height: 14, rx: 3, fill: "#1e1e38",
    }));
    let cursor = pad.left;
    keys.forEach((key) => {
      const value = row[key] || 0;
      if (value <= 0) return;
      const w = (value / max) * plotW;
      const rect = el("rect", {
        x: cursor + 1, y, width: Math.max(1, w - 2), height: 14,
        fill: fill(colors[key]),
      });
      bindTip(rect, tipBody(row.group, [
        ...keys.map((k) => tipRow(labels[k], compact(row[k] || 0), colors[k])),
        tipRow("Total", compact(total)),
      ]));
      if (onSelect) {
        rect.style.cursor = "pointer";
        rect.addEventListener("click", () => onSelect(row));
      }
      svg.appendChild(rect);
      cursor += w;
    });

    const denom = row.compliant + row.non_compliant + row.unknown + row.stale;
    const text = withPct && denom
      ? `${((row.compliant / denom) * 100).toFixed(1)}%`
      : compact(total);
    svg.appendChild(el("text", {
      x: width - 4, y: y + 11, "text-anchor": "end", class: "axis-tick",
      style: `fill:${INK};font-weight:600`,
    }, text));
  });
}

/* ------------------------------------------------------------------ donut */
export function donut(container, options) {
  const { rows, centerLabel = "Servers", centerValue } = options;
  const total = rows.reduce((s, r) => s + (r.value || 0), 0);
  if (!total) {
    emptyState(container, "No compliance data available.");
    return;
  }
  container.innerHTML = "";
  const size = 230;
  const svg = el("svg", { viewBox: `0 0 ${size} ${size}`, class: "chart", role: "img" });
  svg.setAttribute("aria-label",
    `Compliance breakdown: ${rows.map((r) => `${r.label} ${r.value}`).join(", ")}`);
  svg.style.height = `${size}px`;
  svg.style.width = `${size}px`;
  const cx = size / 2;
  const cy = size / 2;
  const radius = 86;
  const stroke = 26;
  const circumference = 2 * Math.PI * radius;
  const gap = 2.5;
  const paint = gradientFactory(svg);
  let offset = 0;

  rows.forEach((row) => {
    if (!row.value) return;
    const frac = row.value / total;
    const length = Math.max(0, frac * circumference - gap);
    const arc = el("circle", {
      cx, cy, r: radius, fill: "none", stroke: paint(row.color),
      "stroke-width": stroke,
      "stroke-dasharray": `${length} ${circumference - length}`,
      "stroke-dashoffset": -offset,
      transform: `rotate(-90 ${cx} ${cy})`,
      "stroke-linecap": "butt",
    });
    bindTip(arc, tipBody(row.label, [
      tipRow("Servers", compact(row.value), row.color),
      tipRow("Share", `${(frac * 100).toFixed(1)}%`),
    ]));
    svg.appendChild(arc);
    offset += frac * circumference;
  });

  svg.appendChild(el("text", {
    x: cx, y: cy - 6, "text-anchor": "middle",
    style: `fill:${INK};font-size:26px;font-weight:650;letter-spacing:-.5px`,
  }, centerValue ?? compact(total)));
  svg.appendChild(el("text", {
    x: cx, y: cy + 16, "text-anchor": "middle",
    style: `fill:${MUTED};font-size:11.5px`,
  }, centerLabel));
  container.appendChild(svg);
}

/* ------------------------------------------------------------------ line + target */
export function trendLine(container, options) {
  if (!container.isConnected) {
    queueChartDraw(container, () => {
      if (container.isConnected) trendLine(container, options);
    });
    return;
  }
  const { points, target, height = 220, unit = "%", empty } = options;
  if (!points || points.length < 2) {
    emptyState(container, empty
      || "Not enough daily snapshots yet. The trend is built from one snapshot per day.");
    return;
  }
  const width = widthFor(container);
  const pad = { top: 18, right: 54, bottom: 30, left: 40 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const values = points.map((p) => Number(p.value));
  const maxY = 100;
  const minY = Math.max(0, Math.min(...values, target ?? 100) - 10);
  const firstPoint = points[0];
  const lastPoint = points[points.length - 1];
  const svg = frame(container, height,
    `Compliance trend from ${firstPoint.label} at ${Number(firstPoint.value).toFixed(1)}${unit} `
    + `to ${lastPoint.label} at ${Number(lastPoint.value).toFixed(1)}${unit}`
    + (target !== null && target !== undefined ? `, target ${target}${unit}` : ""), width);
  const x = (i) => pad.left + (i / (points.length - 1)) * plotW;
  const y = (v) => pad.top + plotH - ((v - minY) / (maxY - minY)) * plotH;

  for (let i = 0; i <= 4; i += 1) {
    const value = minY + ((maxY - minY) / 4) * i;
    const yy = y(value);
    svg.appendChild(el("line", {
      x1: pad.left, x2: width - pad.right, y1: yy, y2: yy, class: "gridline",
    }));
    svg.appendChild(el("text", {
      x: pad.left - 8, y: yy + 4, "text-anchor": "end", class: "axis-tick",
    }, `${Math.round(value)}${unit}`));
  }

  if (target !== null && target !== undefined) {
    svg.appendChild(el("line", {
      x1: pad.left, x2: width - pad.right, y1: y(target), y2: y(target),
      stroke: "#ec835a", "stroke-width": 1.5, "stroke-dasharray": "5 4",
    }));
    svg.appendChild(el("text", {
      x: width - pad.right + 6, y: y(target) + 4, class: "axis-tick",
      style: "fill:#ec835a",
    }, `Target ${target}${unit}`));
  }

  const path = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(Number(p.value))}`)
    .join(" ");
  svg.appendChild(el("path", {
    d: path, fill: "none", stroke: ACCENT, "stroke-width": 2,
    "stroke-linejoin": "round", "stroke-linecap": "round",
  }));

  points.forEach((point, i) => {
    const dot = el("circle", {
      cx: x(i), cy: y(Number(point.value)), r: 4.5, fill: ACCENT,
      stroke: SURFACE, "stroke-width": 2,
    });
    bindTip(dot, tipBody(point.label, [
      tipRow("Compliance", `${Number(point.value).toFixed(1)}${unit}`, ACCENT),
      target ? tipRow("Target", `${target}${unit}`, "#ec835a") : null,
    ]));
    svg.appendChild(dot);
    const stride = Math.ceil(points.length / (width < 480 ? 5 : 7));
    if (i % stride === 0 || i === points.length - 1) {
      svg.appendChild(el("text", {
        x: x(i), y: height - 10, "text-anchor": "middle", class: "axis-label",
      }, point.label));
    }
  });

  const last = points[points.length - 1];
  svg.appendChild(el("text", {
    x: x(points.length - 1), y: y(Number(last.value)) - 10,
    "text-anchor": "end", class: "axis-tick",
    style: `fill:${INK};font-weight:600`,
  }, `${Number(last.value).toFixed(1)}${unit}`));
}

/* ------------------------------------------------------------------ columns + target */
export function targetColumns(container, options) {
  if (!container.isConnected) {
    queueChartDraw(container, () => {
      if (container.isConnected) targetColumns(container, options);
    });
    return;
  }
  const { points, target, height = 220, unit = "%", empty } = options;
  if (!points || points.length === 0) {
    emptyState(container, empty || "No quarterly snapshots captured yet.");
    return;
  }
  const width = widthFor(container);
  const pad = { top: 20, right: 54, bottom: 34, left: 40 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const maxY = 100;
  const svg = frame(container, height,
    `Compliance by period: ${points.map((p) => `${p.label} ${Number(p.value).toFixed(1)}${unit}`).join(", ")}`
    + (target !== null && target !== undefined ? `, target ${target}${unit}` : ""), width);
  const band = plotW / points.length;
  const barW = Math.min(46, band * 0.5);

  for (let i = 0; i <= 4; i += 1) {
    const value = (maxY / 4) * i;
    const yy = pad.top + plotH - (value / maxY) * plotH;
    svg.appendChild(el("line", {
      x1: pad.left, x2: width - pad.right, y1: yy, y2: yy, class: "gridline",
    }));
    svg.appendChild(el("text", {
      x: pad.left - 8, y: yy + 4, "text-anchor": "end", class: "axis-tick",
    }, `${Math.round(value)}${unit}`));
  }

  points.forEach((point, i) => {
    const value = Number(point.value);
    const cx = pad.left + band * i + band / 2;
    const h = (value / maxY) * plotH;
    const y = pad.top + plotH - h;
    // Bars carry the compliance state; the label beneath carries identity.
    const met = target === null || value >= target;
    const bar = el("rect", {
      x: cx - barW / 2, y, width: barW, height: h,
      fill: met ? STATUS.compliant : SEVERITY.important,
      rx: 4, ry: 4,
    });
    bindTip(bar, tipBody(point.label, [
      tipRow("Compliance", `${value.toFixed(1)}${unit}`,
        met ? STATUS.compliant : SEVERITY.important),
      target ? tipRow("Target", `${target}${unit}`, "#ec835a") : null,
    ]));
    svg.appendChild(bar);
    svg.appendChild(el("text", {
      x: cx, y: y - 7, "text-anchor": "middle", class: "axis-tick",
      style: `fill:${INK};font-weight:600`,
    }, `${value.toFixed(1)}${unit}`));
    // Narrow panes cannot fit every period label without overlap.
    const labelled = band >= 46 || i % 2 === 0 || i === points.length - 1;
    if (labelled) {
      svg.appendChild(el("text", {
        x: cx, y: height - 16, "text-anchor": "middle", class: "axis-label",
      }, point.label));
    }
    if (point.sub && labelled) {
      svg.appendChild(el("text", {
        x: cx, y: height - 3, "text-anchor": "middle", class: "axis-label",
        style: "font-size:9.5px",
      }, point.sub));
    }
  });

  if (target !== null && target !== undefined) {
    const yy = pad.top + plotH - (target / maxY) * plotH;
    svg.appendChild(el("line", {
      x1: pad.left, x2: width - pad.right, y1: yy, y2: yy,
      stroke: "#ec835a", "stroke-width": 1.5, "stroke-dasharray": "5 4",
    }));
    svg.appendChild(el("text", {
      x: width - pad.right + 6, y: yy + 4, class: "axis-tick",
      style: "fill:#ec835a",
    }, `Target ${target}${unit}`));
  }
}

/* ------------------------------------------------------------------ severity bars */
export function severityBars(container, rows) {
  if (!container.isConnected) {
    queueChartDraw(container, () => {
      if (container.isConnected) severityBars(container, rows);
    });
    return;
  }
  const data = rows.filter((r) => r.count > 0);
  if (data.length === 0) {
    emptyState(container, "No outstanding patch findings recorded.");
    return;
  }
  const height = data.length * 32 + 6;
  const width = widthFor(container);
  const pad = { left: 84, right: 52 };
  const plotW = width - pad.left - pad.right;
  const max = Math.max(...data.map((r) => r.count));
  container.innerHTML = "";
  const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, class: "chart", role: "img" });
  svg.setAttribute("aria-label",
    `Outstanding findings by severity: ${data.map((r) => `${r.severity} ${r.count}`).join(", ")}`);
  svg.style.height = `${height}px`;
  const paint = gradientFactory(svg, false);
  data.forEach((row, i) => {
    const y = i * 32 + 4;
    const w = (row.count / max) * plotW;
    svg.appendChild(el("text", {
      x: pad.left - 10, y: y + 13, "text-anchor": "end", class: "axis-label",
      style: `fill:${INK_2};text-transform:capitalize`,
    }, row.severity));
    const bar = el("rect", {
      x: pad.left, y, width: Math.max(2, w), height: 16, rx: 3,
      fill: paint(SEVERITY[row.severity] || MUTED),
    });
    bindTip(bar, tipBody(row.severity, [
      tipRow("Outstanding patches", compact(row.count), SEVERITY[row.severity]),
    ]));
    svg.appendChild(bar);
    svg.appendChild(el("text", {
      x: width - 4, y: y + 13, "text-anchor": "end", class: "axis-tick",
      style: `fill:${INK};font-weight:600`,
    }, compact(row.count)));
  });
  container.appendChild(svg);
}

/* -------------------------------------------------- concentration curve */
/* Cumulative share of outstanding patches against share of affected servers.
 * The diagonal is perfect evenness; the further above it the curve sits, the
 * more the backlog is concentrated in a few servers. */
export function concentrationCurve(container, points) {
  if (!container.isConnected) {
    queueChartDraw(container, () => {
      if (container.isConnected) concentrationCurve(container, points);
    });
    return;
  }
  const data = (points || []).filter((p) => p && p.patch_share !== undefined);
  if (data.length < 2) {
    emptyState(container, "No outstanding patches to plot.");
    return;
  }
  const height = 220;
  const width = widthFor(container);
  const pad = { left: 44, right: 16, top: 10, bottom: 30 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const x = (share) => pad.left + (share / 100) * plotW;
  const y = (share) => pad.top + plotH - (share / 100) * plotH;
  container.innerHTML = "";
  const svg = el("svg", {
    viewBox: `0 0 ${width} ${height}`, class: "chart", role: "img",
  });
  svg.setAttribute("aria-label",
    `Concentration: the top ${data[0].server_share}% of affected servers hold ` +
    `${data[0].patch_share}% of outstanding patches; the top 10% hold ` +
    `${data[9] ? data[9].patch_share : "—"}%.`);
  svg.style.height = `${height}px`;
  for (const tick of [0, 25, 50, 75, 100]) {
    svg.appendChild(el("line", {
      x1: pad.left, y1: y(tick), x2: width - pad.right, y2: y(tick),
      stroke: BORDER, "stroke-width": 1,
    }));
    svg.appendChild(el("text", {
      x: pad.left - 8, y: y(tick) + 4, "text-anchor": "end",
      class: "axis-tick", style: `fill:${MUTED}`,
    }, `${tick}%`));
    svg.appendChild(el("text", {
      x: x(tick), y: height - 10, "text-anchor": "middle",
      class: "axis-tick", style: `fill:${MUTED}`,
    }, `${tick}%`));
  }
  svg.appendChild(el("line", {
    x1: x(0), y1: y(0), x2: x(100), y2: y(100),
    stroke: MUTED, "stroke-width": 1, "stroke-dasharray": "4 4",
  }));
  const path = data.map((p, i) =>
    `${i === 0 ? "M" : "L"}${x(p.server_share)},${y(p.patch_share)}`).join(" ");
  svg.appendChild(el("path", {
    d: `${path} L${x(100)},${y(0)} Z`, fill: "rgba(255,45,149,.14)",
  }));
  svg.appendChild(el("path", {
    d: path, fill: "none", stroke: ACCENT, "stroke-width": 2.5,
    "stroke-linejoin": "round", "stroke-linecap": "round",
  }));
  for (const point of data) {
    const dot = el("circle", {
      cx: x(point.server_share), cy: y(point.patch_share), r: 3.5,
      fill: ACCENT, stroke: SURFACE, "stroke-width": 2,
    });
    bindTip(dot, {
      title: `Top ${point.server_share}% of affected servers`,
      rows: [{
        label: "Outstanding patches", value: `${point.patch_share}%`,
        color: ACCENT,
      }, { label: "Servers", value: String(point.servers) }],
    });
    svg.appendChild(dot);
  }
  container.appendChild(svg);
}

/* ------------------------------------------------------------------ sparkline */
export function sparkline(container, points, color = ACCENT) {
  if (!points || points.length < 2) {
    container.innerHTML = "";
    return;
  }
  const w = 120;
  const h = 30;
  const values = points.map(Number);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const d = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * w;
      const y = h - ((v - min) / span) * (h - 4) - 2;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  container.replaceChildren();
  const svg = el("svg", {
    viewBox: `0 0 ${w} ${h}`, width: 120, height: 30, "aria-hidden": "true",
  });
  svg.appendChild(el("path", {
    d, fill: "none", stroke: color, "stroke-width": 2,
    "stroke-linecap": "round", "stroke-linejoin": "round",
  }));
  container.appendChild(svg);
}
