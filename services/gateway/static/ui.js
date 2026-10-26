/* DOM helpers — kept deliberately small, no framework. */

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

export function fmtDate(value, withTime = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const opts = { year: "numeric", month: "short", day: "2-digit" };
  if (withTime) Object.assign(opts, { hour: "2-digit", minute: "2-digit" });
  return date.toLocaleString(undefined, opts);
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
      background: key === "compliant" ? "#0ca30c"
        : key === "non_compliant" ? "#d03b3b"
          : key === "excluded" ? "#4a5560" : "#fab219",
    } }),
    STATE_LABELS[key] || key);
}

export function pill(value, label) {
  return h("span", { class: `pill ${value}` }, label || value);
}

export function statTile({ label, value, sub, delta, tone, meter, foot, accent }) {
  const card = h("div", { class: "card stat" });
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
        background: accent || "#3987e5",
      } }));
    card.appendChild(bar);
  }
  if (foot) card.appendChild(h("div", { class: "stat-foot" }, foot));
  return card;
}

export function card(title, subtitle, body, extra) {
  const head = h("div", { class: "card-head" },
    h("div", {},
      h("div", { class: "card-title" }, title),
      subtitle ? h("div", { class: "card-sub" }, subtitle) : null),
    extra || null);
  return h("div", { class: "card" }, head, body);
}

export function alert(kind, text) {
  return h("div", { class: `alert ${kind}` },
    h("span", { class: "ico" }, kind === "error" ? "✕" : kind === "warn" ? "!" : "✓"),
    h("div", {}, text));
}

export function spinnerRows(rows = 4) {
  return h("div", { class: "stack" },
    ...Array.from({ length: rows }, () =>
      h("div", { class: "skeleton", style: { height: "46px" } })));
}

export function table(columns, rows, options = {}) {
  const { empty = "No records found.", rowKey } = options;
  const thead = h("thead", {}, h("tr", {},
    ...columns.map((col) => h("th", {
      class: col.numeric ? "num" : "",
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
  return h("div", { class: "table-wrap" },
    h("table", { class: `data ${options.compact ? "compact" : ""}`.trim() }, thead, tbody));
}

export function select({ label, value, options, onChange, allLabel = "All" }) {
  const id = `sel-${Math.random().toString(36).slice(2, 9)}`;
  const element = h("select", { id, onchange: (e) => onChange(e.target.value) });
  if (allLabel) element.appendChild(h("option", { value: "" }, allLabel));
  for (const option of options || []) {
    const optValue = typeof option === "string" ? option : option.value;
    const optLabel = typeof option === "string" ? option : option.label;
    const opt = h("option", { value: optValue }, optLabel);
    if (String(value ?? "") === String(optValue)) opt.selected = true;
    element.appendChild(opt);
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
  const close = () => { root.innerHTML = ""; };
  const dialog = h("div", { class: "modal", role: "dialog", "aria-modal": "true" },
    h("div", { class: "row between" },
      h("h2", {}, title),
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
  root.innerHTML = "";
  root.appendChild(backdrop);
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
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}

export function quarterLabel(date = new Date()) {
  return `${date.getFullYear()}-Q${Math.floor(date.getMonth() / 3) + 1}`;
}
