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

const SURFACE = "#171b21";
const INK = "#f0f3f6";
const INK_2 = "#b3bdc8";
const MUTED = "#7f8b98";
const BORDER = "#2c333d";

export const STATUS = {
  compliant: "#0ca30c",
  non_compliant: "#d03b3b",
  unknown: "#fab219",
  stale: "#7f8b98",
  excluded: "#4a5560",
};

export const SEVERITY = {
  critical: "#d03b3b",
  important: "#ec835a",
  moderate: "#fab219",
  low: "#3987e5",
  unknown: "#7f8b98",
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

function showTip(html, event) {
  const tip = tooltip();
  tip.innerHTML = html;
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

function bindTip(node, html) {
  node.addEventListener("mouseenter", (e) => showTip(html, e));
  node.addEventListener("mousemove", (e) => showTip(html, e));
  node.addEventListener("mouseleave", hideTip);
  node.setAttribute("tabindex", "0");
  node.addEventListener("focus", (e) => {
    const box = node.getBoundingClientRect();
    showTip(html, { clientX: box.left + box.width / 2, clientY: box.top });
  });
  node.addEventListener("blur", hideTip);
}

function tipRow(label, value, color) {
  const swatch = color
    ? `<span class="legend-swatch" style="display:inline-block;background:${color}"></span> `
    : "";
  return `<div class="tt-row"><span>${swatch}${label}</span><strong>${value}</strong></div>`;
}

export function legend(container, entries) {
  container.innerHTML = "";
  for (const entry of entries) {
    const span = document.createElement("span");
    span.className = "legend-item";
    span.innerHTML =
      `<span class="legend-swatch" style="background:${entry.color}"></span>${entry.label}`;
    container.appendChild(span);
  }
}

function emptyState(container, message) {
  container.innerHTML = `<div class="chart-empty">${message}</div>`;
}

function frame(container, height) {
  container.innerHTML = "";
  const svg = el("svg", {
    class: "chart",
    viewBox: `0 0 640 ${height}`,
    preserveAspectRatio: "none",
    role: "img",
  });
  svg.style.height = `${height}px`;
  container.appendChild(svg);
  return svg;
}

/* ------------------------------------------------------------------ stacked columns */
export function stackedColumns(container, options) {
  const {
    rows, keys, labels, colors, height = 220,
    valueLabel = "servers", showTotals = true,
  } = options;
  if (!rows || rows.length === 0) {
    emptyState(container, "No servers match the current filters.");
    return;
  }
  const width = 640;
  const pad = { top: 22, right: 12, bottom: 34, left: 44 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const totals = rows.map((r) => keys.reduce((sum, k) => sum + (r[k] || 0), 0));
  const max = niceMax(Math.max(...totals));
  const svg = frame(container, height);

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
        fill: colors[segment.key],
        rx: isTop ? 4 : 0,
        ry: isTop ? 4 : 0,
      });
      bindTip(rect, [
        `<strong>${row.group}</strong>`,
        ...keys.map((k) =>
          tipRow(labels[k], compact(row[k] || 0), colors[k])),
        tipRow(valueLabel, compact(total)),
      ].join(""));
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
        style: "fill:#f0f3f6;font-weight:600",
      }, compact(total)));
    }
    // Wide enough for the longest platform name ("Microsoft Windows"); the
    // old 16-character cut rendered it as "Microsoft Windo…".
    const label = String(row.group).length > 24
      ? `${String(row.group).slice(0, 23)}…` : row.group;
    svg.appendChild(el("text", {
      x: cx, y: height - 12, "text-anchor": "middle", class: "axis-label",
    }, label));
  });
}

/* ------------------------------------------------------------------ stacked rows */
export function stackedRows(container, options) {
  const {
    rows, keys, labels, colors, maxRows = 7, withPct = false,
  } = options;
  if (!rows || rows.length === 0) {
    emptyState(container, "No servers match the current filters.");
    return;
  }
  const shown = rows.slice(0, maxRows);
  const rowH = 30;
  const height = shown.length * rowH + 8;
  const width = 640;
  const pad = { left: 148, right: withPct ? 66 : 46 };
  const plotW = width - pad.left - pad.right;
  const max = Math.max(
    1, ...shown.map((r) => keys.reduce((s, k) => s + (r[k] || 0), 0))
  );
  const svg = frame(container, height);

  shown.forEach((row, index) => {
    const y = index * rowH + 6;
    const total = keys.reduce((s, k) => s + (row[k] || 0), 0);
    const label = String(row.group).length > 22
      ? `${String(row.group).slice(0, 21)}…` : row.group;
    svg.appendChild(el("text", {
      x: pad.left - 10, y: y + 15, "text-anchor": "end", class: "axis-label",
      style: "fill:#b3bdc8",
    }, label));

    const fullW = (total / max) * plotW;
    svg.appendChild(el("rect", {
      x: pad.left, y, width: fullW, height: 14, rx: 3, fill: "#242b34",
    }));
    let cursor = pad.left;
    keys.forEach((key) => {
      const value = row[key] || 0;
      if (value <= 0) return;
      const w = (value / max) * plotW;
      const rect = el("rect", {
        x: cursor + 1, y, width: Math.max(1, w - 2), height: 14, fill: colors[key],
      });
      bindTip(rect, [
        `<strong>${row.group}</strong>`,
        ...keys.map((k) => tipRow(labels[k], compact(row[k] || 0), colors[k])),
        tipRow("Total", compact(total)),
      ].join(""));
      svg.appendChild(rect);
      cursor += w;
    });

    const denom = row.compliant + row.non_compliant + row.unknown + row.stale;
    const text = withPct && denom
      ? `${((row.compliant / denom) * 100).toFixed(1)}%`
      : compact(total);
    svg.appendChild(el("text", {
      x: width - 4, y: y + 11, "text-anchor": "end", class: "axis-tick",
      style: "fill:#f0f3f6;font-weight:600",
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
  svg.style.height = `${size}px`;
  svg.style.width = `${size}px`;
  const cx = size / 2;
  const cy = size / 2;
  const radius = 86;
  const stroke = 26;
  const circumference = 2 * Math.PI * radius;
  const gap = 2.5;
  let offset = 0;

  rows.forEach((row) => {
    if (!row.value) return;
    const frac = row.value / total;
    const length = Math.max(0, frac * circumference - gap);
    const arc = el("circle", {
      cx, cy, r: radius, fill: "none", stroke: row.color,
      "stroke-width": stroke,
      "stroke-dasharray": `${length} ${circumference - length}`,
      "stroke-dashoffset": -offset,
      transform: `rotate(-90 ${cx} ${cy})`,
      "stroke-linecap": "butt",
    });
    bindTip(arc, [
      `<strong>${row.label}</strong>`,
      tipRow("Servers", compact(row.value), row.color),
      tipRow("Share", `${(frac * 100).toFixed(1)}%`),
    ].join(""));
    svg.appendChild(arc);
    offset += frac * circumference;
  });

  svg.appendChild(el("text", {
    x: cx, y: cy - 6, "text-anchor": "middle",
    style: "fill:#f0f3f6;font-size:26px;font-weight:650;letter-spacing:-.5px",
  }, centerValue ?? compact(total)));
  svg.appendChild(el("text", {
    x: cx, y: cy + 16, "text-anchor": "middle",
    style: "fill:#7f8b98;font-size:11.5px",
  }, centerLabel));
  container.appendChild(svg);
}

/* ------------------------------------------------------------------ line + target */
export function trendLine(container, options) {
  const { points, target, height = 220, unit = "%", empty } = options;
  if (!points || points.length < 2) {
    emptyState(container, empty
      || "Not enough daily snapshots yet. The trend is built from one snapshot per day.");
    return;
  }
  const width = 640;
  const pad = { top: 18, right: 54, bottom: 30, left: 40 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const values = points.map((p) => Number(p.value));
  const maxY = 100;
  const minY = Math.max(0, Math.min(...values, target ?? 100) - 10);
  const svg = frame(container, height);
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
    d: path, fill: "none", stroke: "#3987e5", "stroke-width": 2,
    "stroke-linejoin": "round", "stroke-linecap": "round",
  }));

  points.forEach((point, i) => {
    const dot = el("circle", {
      cx: x(i), cy: y(Number(point.value)), r: 4.5, fill: "#3987e5",
      stroke: SURFACE, "stroke-width": 2,
    });
    bindTip(dot, `<strong>${point.label}</strong>${tipRow(
      "Compliance", `${Number(point.value).toFixed(1)}${unit}`, "#3987e5")}${
      target ? tipRow("Target", `${target}${unit}`, "#ec835a") : ""}`);
    svg.appendChild(dot);
    const stride = Math.ceil(points.length / 7);
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
    style: "fill:#f0f3f6;font-weight:600",
  }, `${Number(last.value).toFixed(1)}${unit}`));
}

/* ------------------------------------------------------------------ columns + target */
export function targetColumns(container, options) {
  const { points, target, height = 220, unit = "%", empty } = options;
  if (!points || points.length === 0) {
    emptyState(container, empty || "No quarterly snapshots captured yet.");
    return;
  }
  const width = 640;
  const pad = { top: 20, right: 54, bottom: 34, left: 40 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const maxY = 100;
  const svg = frame(container, height);
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
    bindTip(bar, `<strong>${point.label}</strong>${tipRow(
      "Compliance", `${value.toFixed(1)}${unit}`, met ? STATUS.compliant : SEVERITY.important,
    )}${target ? tipRow("Target", `${target}${unit}`, "#ec835a") : ""}`);
    svg.appendChild(bar);
    svg.appendChild(el("text", {
      x: cx, y: y - 7, "text-anchor": "middle", class: "axis-tick",
      style: "fill:#f0f3f6;font-weight:600",
    }, `${value.toFixed(1)}${unit}`));
    svg.appendChild(el("text", {
      x: cx, y: height - 16, "text-anchor": "middle", class: "axis-label",
    }, point.label));
    if (point.sub) {
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
  const data = rows.filter((r) => r.count > 0);
  if (data.length === 0) {
    emptyState(container, "No outstanding patch findings recorded.");
    return;
  }
  const height = data.length * 32 + 6;
  const width = 640;
  const pad = { left: 84, right: 52 };
  const plotW = width - pad.left - pad.right;
  const max = Math.max(...data.map((r) => r.count));
  container.innerHTML = "";
  const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, class: "chart", role: "img" });
  svg.style.height = `${height}px`;
  data.forEach((row, i) => {
    const y = i * 32 + 4;
    const w = (row.count / max) * plotW;
    svg.appendChild(el("text", {
      x: pad.left - 10, y: y + 13, "text-anchor": "end", class: "axis-label",
      style: "fill:#b3bdc8;text-transform:capitalize",
    }, row.severity));
    const bar = el("rect", {
      x: pad.left, y, width: Math.max(2, w), height: 16, rx: 3,
      fill: SEVERITY[row.severity] || MUTED,
    });
    bindTip(bar, `<strong>${row.severity}</strong>${tipRow(
      "Outstanding patches", compact(row.count), SEVERITY[row.severity])}`);
    svg.appendChild(bar);
    svg.appendChild(el("text", {
      x: width - 4, y: y + 13, "text-anchor": "end", class: "axis-tick",
      style: "fill:#f0f3f6;font-weight:600",
    }, compact(row.count)));
  });
  container.appendChild(svg);
}

/* ------------------------------------------------------------------ sparkline */
export function sparkline(container, points, color = "#3987e5") {
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
  container.innerHTML =
    `<svg viewBox="0 0 ${w} ${h}" width="120" height="30" aria-hidden="true">` +
    `<path d="${d}" fill="none" stroke="${color}" stroke-width="2" ` +
    `stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}
