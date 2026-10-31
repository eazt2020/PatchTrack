/* DOM helpers — kept deliberately small, no framework. */

import { flushCharts } from "/static/charts.js";

export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "html") node.innerHTML = value;
    else if (key === "style" && typeof value === "object") {
      Object.assign(node.style, value);
    } else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "dataset") {
      Object.assign(node.dataset, value);
    } else {
      node.setAttribute(key, value === true ? "" : String(value));
    }
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function mount(node, ...children) {
  clear(node);
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  // Charts built while detached can now measure their pane and draw.
  flushCharts();
  return node;
}

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

export function fmtNumber(value) {
  if (value === null || value === undefined || value === "") return "—";
  return Number(value).toLocaleString();
}

export function fmtPct(value, digits = 1) {
  if (value === null || value === undefined) return "n/a";
  return `${Number(value).toFixed(digits)}%`;
}

/* The deployment's timezone, set once at boot and after a Settings change.
 * Instants arrive in UTC; calendar labels and clock times are shown in the
 * timezone the rest of PatchTrack uses for periods and snapshots. */
let displayTimeZone = null;

export function setDisplayTimeZone(zone) {
  displayTimeZone = zone || null;
}

function zonedCalendar(date) {
  if (displayTimeZone) {
    try {
      const parts = new Intl.DateTimeFormat("en-CA", {
        timeZone: displayTimeZone, year: "numeric", month: "2-digit",
      }).formatToParts(date);
      const value = Object.fromEntries(parts.map((p) => [p.type, p.value]));
      return { year: Number(value.year), month: Number(value.month) };
    } catch { /* an unknown zone falls back to the browser clock */ }
  }
  return { year: date.getFullYear(), month: date.getMonth() + 1 };
}

export function fmtDate(value, withTime = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const opts = { year: "numeric", month: "short", day: "2-digit" };
  if (withTime) Object.assign(opts, { hour: "2-digit", minute: "2-digit" });
  if (displayTimeZone) opts.timeZone = displayTimeZone;
  return date.toLocaleString(undefined, opts);
}

export function fmtTime(value = new Date()) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const opts = { hour: "2-digit", minute: "2-digit", second: "2-digit" };
  if (displayTimeZone) opts.timeZone = displayTimeZone;
  return date.toLocaleTimeString(undefined, opts);
}

export function fmtRelative(value) {
  if (!value) return "never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const diff = (Date.now() - date.getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} h ago`;
  return `${Math.floor(diff / 86400)} d ago`;
}

const STATE_LABELS = {
  compliant: "Compliant",
  non_compliant: "Non-compliant",
  unknown: "Unknown",
  stale: "Stale",
  excluded: "Excluded",
};

export function statePill(state) {
  const key = state || "unknown";
  return h("span", { class: `pill ${key}` },
    h("span", { class: "dot", style: {
      background: key === "compliant" ? "#22e6a6"
        : key === "non_compliant" ? "#ff3b6b"
          : key === "excluded" ? "#4b4a63" : "#ffb020",
    } }),
    STATE_LABELS[key] || key);
}

export function pill(value, label) {
  return h("span", { class: `pill ${value}` }, label || value);
}

export function statTile({ label, value, sub, delta, tone, meter, foot, accent,
  href }) {
  const card = h("div", { class: "card stat" });
  // The headline number takes the tile's accent, so the KPI row reads as the
  // reference board's row of differently coloured instruments.
  if (accent) card.style.setProperty("--stat-accent", accent);
  const labelRow = h("div", { class: "stat-label" });
  if (accent) {
    labelRow.appendChild(h("span", { class: "dot", style: { background: accent } }));
  }
  labelRow.appendChild(document.createTextNode(label));
  card.appendChild(labelRow);

  const row = h("div", { class: "stat-row" });
  row.appendChild(h("div", { class: "stat-value" }, value));
  if (sub) row.appendChild(h("div", { class: "stat-of" }, sub));
  if (delta) {
    row.appendChild(h("div", { class: `stat-delta ${tone || "neutral"}` }, delta));
  }
  card.appendChild(row);

  if (meter !== undefined && meter !== null) {
    const bar = h("div", { class: "meter" },
      h("span", { style: {
        width: `${Math.max(0, Math.min(100, meter))}%`,
        background: accent || "#ff2d95",
      } }));
    card.appendChild(bar);
  }
  if (foot) card.appendChild(h("div", { class: "stat-foot" }, foot));
  // A tile that opens the list behind its number carries the whole card, so
  // the target is the same size as the thing that looked clickable.
  if (href) {
    return h("a", { class: "stat-link", href, "aria-label": `${label}: ${value}` },
      card);
  }
  return card;
}

export function card(title, subtitle, body, extra, options = {}) {
  const heading = h("div", { class: "card-title-row" });
  if (options.icon) {
    const icon = h("span", { class: "card-icon", html: options.icon, "aria-hidden": "true" });
    if (options.accent) icon.style.color = options.accent;
    heading.appendChild(icon);
  }
  heading.appendChild(h("span", { class: "card-title" }, title));
  const head = h("div", { class: "card-head" },
    h("div", {},
      heading,
      subtitle ? h("div", { class: "card-sub" }, subtitle) : null),
    extra || null);
  return h("div", { class: "card" }, head, body);
}

export function alert(kind, text) {
  // Errors and warnings interrupt; confirmations are announced politely.
  return h("div", {
    class: `alert ${kind}`,
    role: kind === "info" ? "status" : "alert",
    "aria-live": kind === "info" ? "polite" : "assertive",
  },
  h("span", { class: "ico", "aria-hidden": "true" },
    kind === "error" ? "✕" : kind === "warn" ? "!" : "✓"),
  h("div", {}, text));
}

export function spinnerRows(rows = 4) {
  return h("div", { class: "stack" },
    ...Array.from({ length: rows }, () =>
      h("div", { class: "skeleton", style: { height: "46px" } })));
}

/* ------------------------------------------------------------------ CSV export */
/* A table cell is either raw data (`key`) or a rendered node. CSV wants the
 * text a person sees, except where the cell is only an action button: an
 * "Edit" button is not a column of data. `export: false` opts a column out
 * explicitly. */
function csvCell(column, row) {
  if (column.export === false) return null;
  let value = column.key !== undefined ? row[column.key] : undefined;
  if (value === undefined && column.render) value = column.render(row);
  if (value instanceof Node) {
    if (value.matches?.("button, input, select, textarea")
        || value.querySelector?.("button, input, select, textarea")) return null;
    // A link or pill renders its label; that label is the value.
    value = value.textContent;
  }
  if (value === null || value === undefined) return "";
  return String(value).replace(/\s+/g, " ").trim();
}

function csvText(columns, rows) {
  // Action-only columns are dropped entirely, header included, rather than
  // exported as a nameless column of empty cells.
  const exported = columns.filter((col) => col.export !== false);
  // A spreadsheet treats a cell beginning with one of these as a formula,
  // even when it arrived in a CSV quoted field. Prefixing an apostrophe makes
  // the value literal text, so an imported hostname cannot execute against
  // the analyst who opens the export.
  const literal = (value) =>
    /^[=+\-@\t\r]/.test(value) ? `'${value}` : value;
  const escape = (original) => {
    const value = literal(original);
    return /[",\n\r]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
  };
  const lines = [exported.map((col) => escape(col.label)).join(",")];
  for (const row of rows) {
    lines.push(exported.map((col) => {
      const value = csvCell(col, row);
      return value === null ? "" : escape(value);
    }).join(","));
  }
  return lines.join("\r\n");
}

function csvFilename(button, fallback) {
  const title = button.closest(".card")?.querySelector(".card-title")?.textContent;
  const slug = (title || fallback).toLowerCase()
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  return `${slug || fallback}-${new Date().toISOString().slice(0, 10)}.csv`;
}

function downloadCsv(name, columns, rows) {
  // The BOM keeps Excel from mangling non-ASCII names in the register.
  const blob = new Blob(["﻿", csvText(columns, rows)],
    { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const anchor = h("a", { href: url, download: name });
  anchor.click();
  URL.revokeObjectURL(url);
}

export function table(columns, rows, options = {}) {
  const { empty = "No records found.", rowKey, export: allowExport = true } = options;
  const thead = h("thead", {}, h("tr", {},
    ...columns.map((col) => h("th", {
      class: col.numeric ? "num" : "",
      scope: "col",
      style: col.width ? { width: col.width } : undefined,
    }, col.label))));
  const tbody = h("tbody");
  if (!rows || rows.length === 0) {
    tbody.appendChild(h("tr", {},
      h("td", { colspan: columns.length, class: "muted", style: { padding: "18px 10px" } },
        empty)));
  }
  for (const row of rows || []) {
    const tr = h("tr", rowKey ? { dataset: { key: rowKey(row) } } : {});
    for (const col of columns) {
      const value = col.render ? col.render(row) : row[col.key];
      const td = h("td", {
        class: `${col.numeric ? "num" : ""} ${col.mono ? "mono" : ""} ${col.class || ""}`.trim(),
      });
      // Strings are inserted as text, never as markup: cells frequently carry
      // values sourced from external systems (hostnames, advisory titles).
      if (value instanceof Node) td.appendChild(value);
      else td.textContent = value === null || value === undefined ? "—" : String(value);
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  const wrap = h("div", { class: "table-wrap" });
  // Every table is a list a reader may need outside the console. A server-paged
  // register supplies `exportRows` so the file contains every matching row, not
  // just the page on screen.
  if (allowExport && ((rows && rows.length) || options.exportRows)) {
    const button = h("button", {
      type: "button", class: "btn btn-sm table-export",
      title: "Download this list as CSV",
    }, "Export CSV");
    button.addEventListener("click", async () => {
      const label = button.textContent;
      button.disabled = true;
      button.textContent = "Exporting…";
      try {
        const data = options.exportRows ? await options.exportRows() : rows;
        downloadCsv(options.exportName || csvFilename(button, "patchtrack-list"),
          columns, data || []);
        button.textContent = label;
      } catch {
        button.textContent = "Export failed";
        setTimeout(() => { button.textContent = label; }, 2000);
      } finally {
        button.disabled = false;
      }
    });
    wrap.appendChild(h("div", { class: "table-tools" }, button));
  }
  wrap.appendChild(
    h("table", { class: `data ${options.compact ? "compact" : ""}`.trim() }, thead, tbody));

  // Tables can be wider than a phone or a narrow pane. Say so rather than
  // clipping columns silently, and let keyboard users scroll the region.
  const frame = h("div", { class: "table-frame" },
    wrap,
    h("span", { class: "table-scroll-hint", "aria-hidden": "true" }, "Scroll →"));
  const syncScrollState = () => {
    const overflow = wrap.scrollWidth - wrap.clientWidth;
    const scrollable = overflow > 1;
    const atEnd = wrap.scrollLeft >= overflow - 1;
    frame.classList.toggle("scrollable", scrollable);
    frame.classList.toggle("at-end", atEnd);
    if (scrollable) {
      wrap.tabIndex = 0;
      wrap.setAttribute("role", "region");
      wrap.setAttribute("aria-label", options.tableLabel || "Table");
    } else {
      wrap.removeAttribute("tabindex");
      wrap.removeAttribute("role");
      wrap.removeAttribute("aria-label");
    }
  };
  wrap.addEventListener("scroll", syncScrollState, { passive: true });
  if (typeof ResizeObserver === "function") {
    new ResizeObserver(syncScrollState).observe(wrap);
  }
  requestAnimationFrame(syncScrollState);
  return frame;
}

export function select({ label, value, options, onChange, allLabel = "All" }) {
  const id = `sel-${Math.random().toString(36).slice(2, 9)}`;
  const element = h("select", { id, onchange: (e) => onChange(e.target.value) });
  if (allLabel) element.appendChild(h("option", { value: "" }, allLabel));
  // An option may name a `group`; consecutive options with the same group are
  // collected into one <optgroup>, which keeps long pickers navigable without
  // changing how the caller describes them.
  const groups = new Map();
  for (const option of options || []) {
    const optValue = typeof option === "string" ? option : option.value;
    const optLabel = typeof option === "string" ? option : option.label;
    const opt = h("option", { value: optValue }, optLabel);
    if (String(value ?? "") === String(optValue)) opt.selected = true;
    const group = typeof option === "object" ? option.group : null;
    if (!group) {
      element.appendChild(opt);
      continue;
    }
    let parent = groups.get(group);
    if (!parent) {
      parent = h("optgroup", { label: group });
      groups.set(group, parent);
      element.appendChild(parent);
    }
    parent.appendChild(opt);
  }
  return h("div", { class: "field" },
    h("label", { for: id }, label), element);
}

/**
 * Show a modal dialog.
 *
 * `dismissible: false` removes the Close button and the backdrop click. Use it
 * only when the dialog is the sole way forward and closing it would strand the
 * user — a forced password change, for example.
 */
export function modal(title, content, actions, { dismissible = true } = {}) {
  const root = document.getElementById("modal-root");
  const previousFocus = document.activeElement;
  const titleId = `modal-title-${Math.random().toString(36).slice(2, 9)}`;
  const close = () => {
    root.innerHTML = "";
    // Return the reader to the control that opened the dialog.
    if (previousFocus instanceof HTMLElement) {
      previousFocus.focus({ preventScroll: true });
    }
  };
  const dialog = h("div", {
    class: "modal", role: "dialog", "aria-modal": "true",
    "aria-labelledby": titleId, tabindex: "-1",
  },
    h("div", { class: "row between" },
      h("h2", { id: titleId }, title),
      dismissible
        ? h("button", { class: "btn btn-sm", onclick: close, "aria-label": "Close" }, "Close")
        : null),
    content,
    actions && actions.length
      ? h("div", { class: "row", style: { marginTop: "16px" } }, ...actions)
      : null);
  const backdrop = h("div", {
    class: "modal-backdrop",
    onclick: (event) => {
      if (dismissible && event.target === backdrop) close();
    },
  }, dialog);
  const focusable = () => Array.from(dialog.querySelectorAll(
    "a[href], button:not([disabled]), input:not([disabled]), " +
    "select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])"
  )).filter((element) => element.getClientRects().length > 0);

  dialog.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      // A forced dialog is the only route forward; swallowing Escape is
      // friendlier than letting it fall through to the kiosk toggle.
      event.preventDefault();
      event.stopPropagation();
      if (dismissible) close();
      return;
    }
    if (event.key !== "Tab") return;
    const items = focusable();
    if (items.length === 0) {
      event.preventDefault();
      dialog.focus();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  root.innerHTML = "";
  root.appendChild(backdrop);
  // Land on the first input so the dialog can be completed from the keyboard;
  // a content-only dialog focuses itself.
  const target = dialog.querySelector(
    "input:not([disabled]), select:not([disabled]), textarea:not([disabled])")
    || focusable()[0] || dialog;
  requestAnimationFrame(() => target.focus());
  return close;
}

export function debounce(fn, wait = 250) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

export function toMonthInput(date = new Date()) {
  const { year, month } = zonedCalendar(date);
  return `${year}-${String(month).padStart(2, "0")}`;
}

export function quarterLabel(date = new Date()) {
  const { year, month } = zonedCalendar(date);
  return `${year}-Q${Math.floor((month - 1) / 3) + 1}`;
}
