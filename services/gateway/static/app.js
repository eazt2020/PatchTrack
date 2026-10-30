/* PatchTrack console — vanilla ES-module SPA.
 *
 * Authorization is enforced by the services; the UI only hides controls the
 * signed-in role cannot use. Deep links are hash routes so a refresh keeps
 * the user on the same view.
 */

import { api, download } from "/static/api.js";
import {
  STATUS, donut, legend, severityBars,
  stackedColumns, stackedRows, targetColumns, trendLine,
} from "/static/charts.js";
import {
  alert, card, debounce, escapeHtml, fmtDate, fmtNumber, fmtPct, fmtRelative,
  fmtTime,
  h, modal, mount, pill, select, setDisplayTimeZone, spinnerRows, statePill,
  statTile, table, toMonthInput,
} from "/static/ui.js";

const BASIS_CHOICES = [
  { value: "monthly", label: "Monthly" },
  { value: "quarterly", label: "Quarterly" },
  { value: "yearly", label: "Yearly" },
];
const REFRESH_CHOICES = [
  { value: "0", label: "Off" },
  { value: "30", label: "30 seconds" },
  { value: "60", label: "1 minute" },
  { value: "300", label: "5 minutes" },
];
const BASIS_POINTS = { monthly: 12, quarterly: 8, yearly: 5 };
const BASIS_NOUNS = { monthly: "months", quarterly: "quarters", yearly: "years" };

function stored(key, fallback) {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;   // private mode / storage disabled
  }
}

function remember(key, value) {
  try { localStorage.setItem(key, String(value)); } catch { /* optional */ }
}

function fmtInterval(seconds) {
  const value = Number(seconds) || 0;
  if (value >= 86400 && value % 86400 === 0) {
    const days = value / 86400;
    return `${days} day${days === 1 ? "" : "s"}`;
  }
  if (value >= 3600 && value % 3600 === 0) {
    const hours = value / 3600;
    return `${hours} hour${hours === 1 ? "" : "s"}`;
  }
  if (value >= 60 && value % 60 === 0) {
    const minutes = value / 60;
    return `${minutes} minute${minutes === 1 ? "" : "s"}`;
  }
  return `${value} second${value === 1 ? "" : "s"}`;
}

function fmtBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GiB`;
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  if (value >= 1024) return `${(value / 1024).toFixed(0)} KiB`;
  return `${value} B`;
}

/* A tab strip with one panel visible at a time. `panels` is a list of
 * [key, label, content]; `state.key` remembers the selection for the session so
 * navigating away and back does not reset it. Keyboard: arrows, Home and End. */
function tabbedPanels(panels, { ariaLabel = "Sections", state: tabState = {} } = {}) {
  const tabbar = h("div", {
    class: "tabbar", role: "tablist", "aria-label": ariaLabel,
  });
  const panelHost = h("div", { class: "tabpanels" });
  const keys = panels.map(([key]) => key);
  const choose = (selected) => {
    tabState.key = selected;
    for (const [key] of panels) {
      const tab = tabbar.querySelector(`#tab-${key}`);
      if (tab) {
        tab.setAttribute("aria-selected", key === selected ? "true" : "false");
        tab.tabIndex = key === selected ? 0 : -1;
      }
      panelHost.querySelector(`#panel-${key}`)?.toggleAttribute(
        "hidden", key !== selected);
    }
  };
  for (const [key, label, content] of panels) {
    tabbar.appendChild(h("button", {
      type: "button", class: "tab", role: "tab", id: `tab-${key}`,
      "aria-controls": `panel-${key}`, "aria-selected": "false",
      tabindex: "-1", onclick: () => choose(key),
    }, label));
    panelHost.appendChild(h("div", {
      role: "tabpanel", id: `panel-${key}`, "aria-labelledby": `tab-${key}`,
      hidden: true,
    }, content));
  }
  tabbar.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    let index = keys.indexOf(tabState.key);
    if (event.key === "Home") index = 0;
    else if (event.key === "End") index = keys.length - 1;
    else if (event.key === "ArrowLeft") index = (index - 1 + keys.length) % keys.length;
    else index = (index + 1) % keys.length;
    choose(keys[index]);
    tabbar.querySelector(`#tab-${keys[index]}`)?.focus();
  });
  choose(keys.includes(tabState.key) ? tabState.key : keys[0]);
  return h("div", { class: "stack" }, tabbar, panelHost);
}

const state = {
  user: null,
  route: "dashboard",
  filters: {
    environment: "", business_group: "", os_family: "", application_group: "",
    patch_source: "",
  },
  dimensions: {},
  // The executive dashboard's own preferences. They are per browser, not per
  // account: a wall display should keep its basis and refresh rate after a
  // reload without touching anyone else's view.
  dashboard: {
    basis: BASIS_CHOICES.some((c) => c.value === stored("patchtrack.basis", "monthly"))
      ? stored("patchtrack.basis", "monthly") : "monthly",
    refreshSeconds: REFRESH_CHOICES.some(
      (c) => c.value === stored("patchtrack.refresh", "60"))
      ? stored("patchtrack.refresh", "60") : "60",
    // "" means Live. Any other value is a period key ("2026-10", "2026-Q4",
    // "2026") and is validated against the recorded snapshots on every render,
    // so a key for a period that no longer exists falls back to Live.
    period: stored("patchtrack.period", ""),
  },
  kiosk: stored("patchtrack.kiosk", "0") === "1",
};

const ROUTES = [
  { group: "Compliance" },
  { id: "dashboard", label: "Executive dashboard", icon: "▦", perm: "dashboard:view" },
  { id: "servers", label: "Non-compliant servers", icon: "▤", perm: "compliance:view" },
  { id: "findings", label: "Remediation", icon: "⚑", perm: "findings:view" },
  { id: "trends", label: "History & trends", icon: "◷", perm: "dashboard:view" },
  { group: "Inventory" },
  { id: "inventory", label: "Server inventory", icon: "▣", perm: "inventory:view" },
  { id: "reconciliation", label: "Reconciliation", icon: "⇄", perm: "inventory:view" },
  { group: "Reporting" },
  { id: "reports", label: "Reports", icon: "▧", perm: "reports:view" },
  { id: "schedules", label: "Schedules", icon: "◔", perm: "reports:view" },
  { group: "Administration" },
  { id: "integrations", label: "Integrations", icon: "⇌", perm: "integrations:view" },
  { id: "policies", label: "Compliance policy", icon: "◎", perm: "compliance:view" },
  { id: "users", label: "Users & roles", icon: "☖", perm: "users:manage" },
  { id: "audit", label: "Audit trail", icon: "⌸", perm: "audit:view" },
  { id: "health", label: "Operational health", icon: "♡", perm: "integrations:view" },
  { id: "backups", label: "Data backups", icon: "⛁", perm: "settings:manage" },
  { id: "settings", label: "Settings", icon: "⚙", perm: "settings:manage" },
];

const view = document.getElementById("view");
const nav = document.getElementById("nav");
const pageTitle = document.getElementById("page-title");
const pageContext = document.getElementById("page-context");
const lastRefresh = document.getElementById("last-refresh");

const can = (permission) =>
  state.user?.permissions?.includes("*") || state.user?.permissions?.includes(permission);

/* ------------------------------------------------------------------ router */
function parseHash() {
  const raw = window.location.hash.replace(/^#\/?/, "");
  const [path, query = ""] = raw.split("?");
  return { path: path || "dashboard", params: new URLSearchParams(query) };
}

function navigate(path, params) {
  const query = params ? `?${new URLSearchParams(params)}` : "";
  window.location.hash = `#/${path}${query}`;
}

/* A route render is asynchronous. If the operator changes the hash again while
 * a fetch is in flight, the older render must not paint over the newer one. */
let renderToken = 0;

async function renderRoute() {
  renderToken += 1;
  const { path, params } = parseHash();
  // The dashboard arms its own timer; any other route must not inherit it.
  stopDashboardRefresh();
  stopBackupRefresh();
  // A wall display can be bookmarked as #/dashboard?tv=1. The parameter wins
  // over the stored preference for this visit but does not overwrite it.
  if (params.get("tv") === "1") setKiosk(true, { persist: false });
  const route = ROUTES.find((r) => r.id === path) || ROUTES.find((r) => r.id === "dashboard");
  if (route.perm && !can(route.perm)) {
    mount(view, alert("error", "Your role does not permit access to this area."));
    return;
  }
  state.route = route.id;
  renderNav();
  pageTitle.textContent = route.label;
  setActiveNav(route.id);
  try {
    const renderer = RENDERERS[route.id];
    if (!renderer) throw new Error(`Unknown view ${route.id}`);
    await renderer(params);
  } catch (error) {
    mount(view, alert("error",
      `This view could not be loaded: ${error.message || error}`));
  }
}

function renderNav() {
  mount(nav, ...ROUTES.filter((r) => r.group || can(r.perm)).map((route) => {
    if (route.group) return h("div", { class: "nav-group-label" }, route.group);
    return h("a", {
      href: `#/${route.id}`,
      dataset: { route: route.id },
    }, h("span", { class: "ico" }, route.icon), route.label);
  }));
}

function setActiveNav(id) {
  for (const link of nav.querySelectorAll("a")) {
    const active = link.dataset.route === id;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

/* Below 1000px the sidebar is an off-canvas drawer; this is the only way to
   reach another area on a phone or narrow window. */
function bindNavToggle() {
  const sidebar = document.getElementById("sidebar");
  const toggle = document.getElementById("nav-toggle");
  if (!sidebar || !toggle) return;
  const setOpen = (open) => {
    document.body.classList.toggle("nav-open", open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    toggle.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
    if (open) sidebar.querySelector("a")?.focus();
  };
  toggle.addEventListener("click", () => {
    setOpen(!document.body.classList.contains("nav-open"));
  });
  document.getElementById("nav-backdrop")?.addEventListener("click", () => setOpen(false));
  sidebar.addEventListener("click", (event) => {
    if (event.target.closest("a")) setOpen(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.body.classList.contains("nav-open")) {
      setOpen(false);
      toggle.focus();
    }
  });
  // A drawer left open past the breakpoint would otherwise cover the desktop
  // layout with no way to close it.
  window.matchMedia("(min-width: 1001px)").addEventListener("change", (event) => {
    if (event.matches) setOpen(false);
  });
}

/* ------------------------------------------------------------------ shared pieces */
let dimensionsPromise = null;

async function loadDimensions(force = false) {
  if (!dimensionsPromise || force) {
    dimensionsPromise = api("inventory/dimensions").catch(() => ({}));
  }
  state.dimensions = await dimensionsPromise;
  return state.dimensions;
}

function filterBar(onChange, options = {}) {
  const dims = state.dimensions || {};
  const fields = [
    ["environment", "Environment"],
    ["business_group", "Business service"],
    ["application_group", "Application group"],
    ["os_family", "Operating system"],
    ["patch_source", "Patch source"],
  ];
  const bar = h("div", { class: "filterbar" });
  for (const [key, label] of fields) {
    if (options.skip?.includes(key)) continue;
    bar.appendChild(select({
      label,
      value: state.filters[key],
      options: dims[key] || [],
      allLabel: "All",
      onChange: (value) => {
        state.filters[key] = value;
        onChange();
      },
    }));
  }
  if (options.note) {
    bar.appendChild(h("span", {
      class: "muted", style: { alignSelf: "center", fontSize: "12px" },
    }, options.note));
  }
  if (options.extra) bar.appendChild(options.extra);
  if (Object.values(state.filters).some(Boolean)) {
    bar.appendChild(h("button", {
      class: "btn btn-sm",
      style: { marginBottom: "1px" },
      onclick: () => {
        for (const key of Object.keys(state.filters)) state.filters[key] = "";
        onChange();
      },
    }, "Clear filters"));
  }
  return bar;
}

function filterQuery() {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(state.filters)) {
    if (value) params.set(key, value);
  }
  return params.toString();
}

/* Server-paged registers show 25 rows at a time. An export must cover every
 * row the filters select, so this walks the pages with the largest page size
 * the service allows and stops at the total it reports. */
async function allPages(path, baseQuery) {
  const query = new URLSearchParams(baseQuery);
  query.set("page_size", "200");
  const rows = [];
  for (let page = 1; ; page += 1) {
    query.set("page", String(page));
    const data = await api(`${path}?${query.toString()}`);
    rows.push(...(data.items || []));
    if (!data.items?.length || rows.length >= (data.total || 0)) return rows;
  }
}

/* --------------------------------------------------------------- list tools */
/* Every register in the console is a list an operator needs to narrow and
 * re-order. These controls work on the rows the view has already fetched, so a
 * filter never changes a total: the count line says "showing X of Y" and the
 * unfiltered totals stay visible in the card subtitle. Server-side lists
 * (server list, inventory) pass the sort to the API instead, because the page
 * only holds one page of rows.
 *
 * A facet is { field, label, format?, value? }; a sort is
 * { field, label, numeric?, rank?, dir? }. `rank` orders enumerated values such
 * as severity by meaning rather than alphabetically. */
const LIST_STATE = new Map();

function listState(key, defaults) {
  if (!LIST_STATE.has(key)) LIST_STATE.set(key, { ...defaults });
  const stateForList = LIST_STATE.get(key);
  for (const [name, value] of Object.entries(defaults)) {
    if (!(name in stateForList)) stateForList[name] = value;
  }
  return stateForList;
}

function rowValue(row, field) {
  return field.split(".").reduce(
    (value, part) => (value === null || value === undefined ? value : value[part]), row);
}

function facetValue(row, facet) {
  if (facet.value) return String(facet.value(row));
  const value = rowValue(row, facet.field);
  return value === null || value === undefined ? "" : String(value);
}

function compareRows(a, b, sort, direction) {
  const av = sort.value ? sort.value(a) : rowValue(a, sort.field);
  const bv = sort.value ? sort.value(b) : rowValue(b, sort.field);
  const blankA = av === null || av === undefined || av === "";
  const blankB = bv === null || bv === undefined || bv === "";
  // Rows without a value sort last in both directions: "never" is not "oldest".
  if (blankA || blankB) return blankA && blankB ? 0 : blankA ? 1 : -1;
  let result;
  if (sort.rank) {
    const ai = sort.rank.indexOf(String(av));
    const bi = sort.rank.indexOf(String(bv));
    result = (ai < 0 ? sort.rank.length : ai) - (bi < 0 ? sort.rank.length : bi);
  } else if (sort.numeric) {
    result = Number(av) - Number(bv);
  } else {
    result = String(av).localeCompare(String(bv), undefined,
      { numeric: true, sensitivity: "base" });
  }
  return direction === "desc" ? -result : result;
}

/* Search reads every value in the row, not one nominated column. A single term
 * can hit a hostname, an operating system, an environment, a status, a title or
 * a note, which is what an operator means by "search". Nested objects and
 * arrays are walked so a report's delivery status or a schedule's recipients
 * are searchable too. Values are only ever compared, never rendered. */
function searchableText(value) {
  const parts = [];
  const visit = (item) => {
    if (item === null || item === undefined) return;
    if (Array.isArray(item)) { item.forEach(visit); return; }
    if (typeof item === "object") { Object.values(item).forEach(visit); return; }
    parts.push(String(item));
  };
  visit(value);
  return parts.join(" ").toLowerCase();
}

function applyList(rows, st, facets, sorts) {
  let out = rows.slice();
  for (const facet of facets) {
    const wanted = st[facet.field] || "";
    if (!wanted) continue;
    out = out.filter((row) => facetValue(row, facet) === wanted);
  }
  const needle = st.q.trim().toLowerCase();
  if (needle) {
    // One flattening per row per keystroke; the rows are pages, not estates.
    const text = new Map();
    const haystack = (row) => {
      if (!text.has(row)) text.set(row, searchableText(row));
      return text.get(row);
    };
    out = out.filter((row) => haystack(row).includes(needle));
  }
  const sort = sorts.find((entry) => entry.field === st.sort);
  if (sort) out.sort((a, b) => compareRows(a, b, sort, st.dir));
  return out;
}

function listPanel(key, {
  title, subtitle, columns, rows, empty = "No records found.",
  exportable = true,
  searchLabel = "Search", searchPlaceholder = "Search every column…",
  facets = [], sorts = [], extra = null, note = null, selection = null,
  live = null,
}) {
  let source = rows || [];
  const st = listState(key, {
    q: "",
    sort: sorts[0]?.field || "",
    dir: sorts[0]?.dir || "asc",
    ...Object.fromEntries(facets.map((facet) => [facet.field, ""])),
  });

  const body = h("div");
  const count = h("div", { class: "list-count muted small" });
  const direction = h("button", {
    type: "button", class: "btn btn-sm", title: "Reverse the sort direction",
    onclick: () => { st.dir = st.dir === "asc" ? "desc" : "asc"; paint(); },
  });

  const tools = [h("div", { class: "field list-search" },
    h("label", {}, searchLabel),
    h("input", {
      type: "search", placeholder: searchPlaceholder, value: st.q,
      "aria-label": `${searchLabel} ${title}`,
      oninput: debounce((event) => { st.q = event.target.value; paint(); }, 120),
    }))];

  /* Selection is opt-in. A panel that offers it gets a checkbox per row and a
   * header checkbox that applies to the rows currently shown, so "select all"
   * means "all that match my search and filters", not "everything in the
   * database" — the operator can see exactly what they are about to act on. */
  const selected = new Set();
  let lastShown = [];
  let headerCheck = null;
  let deleteButton = null;
  let tableColumns = columns;
  let keyOf = null;
  if (selection) {
    keyOf = selection.key || ((row) => row.id);
    const canSelect = selection.selectable || (() => true);
    headerCheck = h("input", {
      type: "checkbox", title: "Select every row shown",
      "aria-label": "Select every row shown",
    });
    headerCheck.addEventListener("change", () => {
      for (const row of lastShown) {
        if (!canSelect(row)) continue;
        if (headerCheck.checked) selected.add(keyOf(row));
        else selected.delete(keyOf(row));
      }
      paint();
    });
    tableColumns = [
      {
        label: headerCheck, export: false, class: "select-cell",
        render: (row) => {
          if (!canSelect(row)) return h("span", { class: "muted" }, "—");
          const box = h("input", {
            type: "checkbox",
            "aria-label": `Select ${selection.label
              ? selection.label(row) : keyOf(row)}`,
          });
          box.checked = selected.has(keyOf(row));
          box.addEventListener("change", () => {
            if (box.checked) selected.add(keyOf(row));
            else selected.delete(keyOf(row));
            paint();
          });
          return box;
        },
      },
      ...columns,
    ];
    deleteButton = h("button", {
      type: "button", class: "btn btn-sm btn-danger", disabled: true,
    }, "Delete selected");
    deleteButton.addEventListener("click", async () => {
      const rows = source.filter((row) => selected.has(keyOf(row)));
      if (!rows.length) return;
      deleteButton.disabled = true;
      const label = deleteButton.textContent;
      deleteButton.textContent = "Deleting…";
      try {
        // `false` means the operator cancelled: keep the ticks so the
        // selection is not silently discarded.
        if (await selection.onDelete(rows) !== false) {
          for (const row of rows) selected.delete(keyOf(row));
        }
      } catch (error) {
        alert("error", error.message);
      } finally {
        deleteButton.textContent = label;
        paint();
      }
    });
    tools.push(deleteButton);
  }

  for (const facet of facets) {
    const seen = new Map();
    for (const row of source) {
      const value = facetValue(row, facet);
      if (value && !seen.has(value)) {
        seen.set(value, facet.format ? facet.format(value) : value);
      }
    }
    tools.push(select({
      label: facet.label,
      value: st[facet.field],
      options: [...seen.entries()]
        .sort((a, b) => String(a[1]).localeCompare(String(b[1]), undefined, { numeric: true }))
        .map(([value, label]) => ({ value, label })),
      allLabel: "All",
      onChange: (value) => { st[facet.field] = value; paint(); },
    }));
  }

  if (sorts.length) {
    tools.push(select({
      label: "Sort by",
      value: st.sort,
      options: sorts.map((sort) => ({ value: sort.field, label: sort.label })),
      allLabel: null,
      onChange: (value) => {
        st.sort = value;
        st.dir = sorts.find((sort) => sort.field === value)?.dir || "asc";
        paint();
      },
    }));
    tools.push(h("div", { class: "field" },
      h("span", { class: "field-label" }, "Order"), direction));
  }

  function paint() {
    const shown = applyList(source, st, facets, sorts);
    lastShown = shown;
    if (selection) {
      const canSelect = selection.selectable || (() => true);
      const eligible = shown.filter(canSelect);
      const chosen = eligible.filter((row) => selected.has(keyOf(row))).length;
      headerCheck.checked = eligible.length > 0 && chosen === eligible.length;
      headerCheck.indeterminate = chosen > 0 && chosen < eligible.length;
      headerCheck.disabled = eligible.length === 0;
      deleteButton.disabled = selected.size === 0;
      deleteButton.textContent = selected.size
        ? `Delete selected (${selected.size})` : "Delete selected";
    }
    direction.textContent = st.dir === "asc" ? "↑ Ascending" : "↓ Descending";
    direction.setAttribute("aria-label",
      `Sort ${st.dir === "asc" ? "ascending" : "descending"}; click to reverse`);
    body.replaceChildren(table(tableColumns, shown,
      { compact: true, empty, export: exportable }));
    const noun = source.length === 1 ? "row" : "rows";
    count.textContent = shown.length === source.length
      ? `${fmtNumber(source.length)} ${noun}`
      : `Showing ${fmtNumber(shown.length)} of ${fmtNumber(source.length)} ${noun}`;
  }

  paint();
  const panel = card(title, subtitle,
    h("div", {},
      h("div", { class: "list-tools" }, ...tools),
      note || null,
      body,
      count),
    extra);

  /* A register whose rows change on their own — a report moving from queued to
   * completed — refreshes its table in place. Re-rendering the whole screen
   * would throw away the operator's search, sort and scroll position, and
   * making them reload to see a finished report is the wrong answer. The poll
   * stops when `done` says nothing is in flight, and a detached panel (the
   * operator navigated away) stops on its next tick. */
  if (live) {
    const interval = live.interval ?? 3000;
    let stopped = false;
    const tick = async () => {
      if (stopped || !panel.isConnected) return;
      try {
        const next = await live.fetch();
        if (stopped || !panel.isConnected) return;
        if (Array.isArray(next)) {
          source = next;
          paint();
        }
        if (live.done && live.done(source)) return;
      } catch {
        // A transient failure must not pin the previous "Generating" state
        // forever; try again on the next tick.
      }
      setTimeout(tick, interval);
    };
    setTimeout(tick, interval);
  }
  return panel;
}

/* ----------------------------------------------------- server-side search box */
/* The two paged registers re-fetch on a debounced keystroke, which replaces the
 * input the operator is typing into. The box therefore owns the latest text and
 * caret it was given, and the render that replaces it puts both back and
 * restores focus — otherwise the caret vanishes at every pause. */
const SEARCH_DRAFT = { route: "", value: "", caret: null, active: false };

function searchBox({ route, value, placeholder, minWidth = "220px", onChange }) {
  return h("input", {
    type: "search", placeholder,
    value: SEARCH_DRAFT.route === route ? SEARCH_DRAFT.value : value,
    style: { minWidth },
    oninput: (event) => {
      SEARCH_DRAFT.route = route;
      SEARCH_DRAFT.value = event.target.value;
      SEARCH_DRAFT.caret = event.target.selectionStart;
      SEARCH_DRAFT.active = true;
      onChange(event.target.value);
    },
    onblur: () => {
      // Replacing the focused input fires blur too. Give the browser a tick:
      // if focus moved to another control the operator has moved on, and the
      // next render must not pull it back.
      setTimeout(() => {
        if (document.activeElement && document.activeElement !== document.body) {
          SEARCH_DRAFT.active = false;
        }
      }, 0);
    },
  });
}

function restoreSearchFocus(route, input) {
  if (SEARCH_DRAFT.route !== route || !SEARCH_DRAFT.active) return;
  if (input.value !== SEARCH_DRAFT.value) input.value = SEARCH_DRAFT.value;
  input.focus({ preventScroll: true });
  const caret = Math.min(SEARCH_DRAFT.caret ?? input.value.length, input.value.length);
  try {
    input.setSelectionRange(caret, caret);
  } catch {
    // Some input types do not expose selection; the focus restore still stands.
  }
}

/* A debounced keystroke from a render that has since been replaced must not
 * navigate with stale text: only the latest value the operator typed, on the
 * route it was typed on, counts. */
function searchAction(route, term, run) {
  if (state.route !== route || term !== SEARCH_DRAFT.value) return;
  run();
}

/* ------------------------------------------------------------- TV / wall mode */
/* The left navigation and the surrounding chrome are noise on a NOC/SOC wall.
 * Kiosk mode hides the navigation and lets the content use the full width; it
 * is a presentation preference only, never an authorization change. */
function setKiosk(on, { persist = true } = {}) {
  state.kiosk = Boolean(on);
  document.body.classList.toggle("kiosk", state.kiosk);
  const button = document.getElementById("kiosk-toggle");
  if (button) {
    button.textContent = state.kiosk ? "⛶ Exit TV" : "⛶ TV mode";
    button.title = state.kiosk
      ? "Show the navigation (Esc)"
      : "Hide the navigation for a wall display (K)";
    button.setAttribute("aria-pressed", state.kiosk ? "true" : "false");
  }
  if (persist) remember("patchtrack.kiosk", state.kiosk ? "1" : "0");
  // The header hides in TV mode. If the toggle button keeps focus, :focus-within
  // would pin it on screen, so hand focus to the view; Tab still reveals the
  // header when an operator needs its controls.
  if (state.kiosk && document.querySelector(".topbar")?.contains(document.activeElement)) {
    document.getElementById("view")?.focus({ preventScroll: true });
  }
}

function bindKiosk() {
  setKiosk(state.kiosk, { persist: false });
  document.getElementById("kiosk-toggle").addEventListener("click", () => {
    setKiosk(!state.kiosk);
  });
  document.addEventListener("keydown", (event) => {
    const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(
      event.target?.tagName) || event.target?.isContentEditable;
    if (typing || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === "k" || event.key === "K") {
      event.preventDefault();
      setKiosk(!state.kiosk);
    } else if (event.key === "Escape" && state.kiosk
               && !document.getElementById("modal-root").childElementCount) {
      setKiosk(false);
    }
  });
}

/* ------------------------------------------------------ backup list refresh */
/* A queued or running backup is short-lived. Polling while one is active makes
   the result appear without a manual reload; the route check and the render
   token prevent an old timer from painting over a different view. */
let backupRefreshTimer = null;

function stopBackupRefresh() {
  if (backupRefreshTimer) {
    window.clearTimeout(backupRefreshTimer);
    backupRefreshTimer = null;
  }
}

function scheduleBackupRefresh() {
  stopBackupRefresh();
  const token = renderToken;
  backupRefreshTimer = window.setTimeout(() => {
    backupRefreshTimer = null;
    if (renderToken !== token || parseHash().path !== "backups") return;
    if (document.hidden) {
      scheduleBackupRefresh();
      return;
    }
    renderBackups();
  }, 5000);
}

/* --------------------------------------------------------- dashboard refresh */
let dashboardTimer = null;

function stopDashboardRefresh() {
  if (dashboardTimer) {
    clearInterval(dashboardTimer);
    dashboardTimer = null;
  }
}

function startDashboardRefresh(seconds) {
  stopDashboardRefresh();
  if (!seconds) return;
  dashboardTimer = setInterval(() => {
    // Do not pull the page out from under an operator who is reading it in a
    // hidden tab or filling in a dialog. The next tick catches up.
    if (document.hidden || state.route !== "dashboard") return;
    if (document.getElementById("modal-root").childElementCount) return;
    renderDashboard({ silent: true }).catch(() => {
      lastRefresh.textContent =
        `Refresh failed at ${fmtTime()} — retrying`;
    });
  }, seconds * 1000);
}

function freshnessLine(freshness) {
  return h("span", {},
    `Inventory: ${fmtRelative(freshness?.inventory_synced_at)} · `,
    `Patch data: ${fmtRelative(freshness?.patch_synced_at)}`);
}

/* The timeframe picker is the single control for both questions: which point
 * in time the tiles describe, and how the trend is grouped. A recorded period
 * implies its own granularity; Live has three explicit variants so the trend
 * basis is never a hidden second setting. */
const BASIS_BY_KIND = { month: "monthly", quarter: "quarterly", year: "yearly" };
const PERIOD_GROUP_LABELS = {
  month: "Recorded months", quarter: "Recorded quarters", year: "Recorded years",
};

function timeframeOptions(periods) {
  return [
    ...BASIS_CHOICES.map((choice) => ({
      value: `live:${choice.value}`,
      label: `Live · ${choice.label.toLowerCase()} trend`,
    })),
    ...periods.map((period) => ({
      value: period.key,
      label: period.label,
      group: PERIOD_GROUP_LABELS[period.kind] || "Recorded periods",
    })),
  ];
}

/* A historical view is a frozen record, so the line above the tiles names the
 * record rather than the age of the latest collection. */
function periodLine(period) {
  const samples = fmtNumber(period.samples);
  return h("span", {},
    `Period: ${period.label} · snapshot captured ${fmtDate(period.captured_at, true)} · `,
    `${samples} daily snapshot${Number(period.samples) === 1 ? "" : "s"} in the period`);
}

function periodNote(period, dimensions) {
  const hasOs = (dimensions || []).includes("os_family");
  return card("About this period",
    `${period.label} · snapshot ${String(period.snapshot_id).slice(0, 8)}`,
    h("div", { class: "stack", style: { maxWidth: "860px" } },
      h("p", { class: "muted" },
        "These tiles are not today's estate: they are the immutable daily ",
        "snapshot captured inside the period, shown as recorded. Historical ",
        "figures are never recalculated after the fact."),
      h("p", { class: "muted" },
        "The trend chart averages every daily snapshot recorded in the period, ",
        "while these tiles show the latest one; the two can therefore differ."),
      h("p", { class: "muted" },
        "Dimension filters apply to the live estate only, because a snapshot ",
        "stores group totals rather than server rows. Choose Live to filter."),
      hasOs ? null : h("p", { class: "muted" },
        "This snapshot predates the operating-system family breakdown, so that ",
        "panel has no recorded data. It is left empty rather than filled from ",
        "the current inventory.")));
}

function kpiRow(summary) {
  const k = summary.kpis;
  const target = k.target_pct;
  const gap = k.gap_pct;
  const met = gap !== null && gap >= 0;
  return h("div", { class: "grid grid-kpi" },
    statTile({
      label: "Total servers (in scope)",
      value: fmtNumber(k.in_scope),
      sub: k.excluded
        ? `${fmtNumber(k.excluded)} excluded of ${fmtNumber(k.total_servers)}`
        : "",
      foot: "Active lifecycle, in compliance scope",
      accent: "#3987e5",
    }),
    statTile({
      label: "Compliant",
      value: fmtNumber(k.compliant),
      // The share belongs in one place: repeating it as a delta implied a
      // comparison that this tile does not make.
      sub: fmtPct(k.compliance_pct),
      tone: "good",
      meter: k.in_scope ? (k.compliant / k.in_scope) * 100 : 0,
      accent: STATUS.compliant,
      foot: "No outstanding blocking findings",
    }),
    statTile({
      label: "Non-compliant",
      value: fmtNumber(k.non_compliant),
      delta: k.in_scope ? fmtPct((k.non_compliant / k.in_scope) * 100) : null,
      tone: "bad",
      meter: k.in_scope ? (k.non_compliant / k.in_scope) * 100 : 0,
      accent: STATUS.non_compliant,
      foot: "Blocking findings outstanding",
    }),
    statTile({
      label: "Unknown / no data",
      value: fmtNumber(k.unknown + k.stale),
      sub: `${fmtNumber(k.unknown)} unknown · ${fmtNumber(k.stale)} stale`,
      delta: k.in_scope
        ? fmtPct(((k.unknown + k.stale) / k.in_scope) * 100) : null,
      tone: "neutral",
      meter: k.in_scope ? ((k.unknown + k.stale) / k.in_scope) * 100 : 0,
      accent: STATUS.unknown,
      foot: "Counted against compliance, never as compliant",
    }),
    statTile({
      label: "Compliance vs target",
      value: fmtPct(k.compliance_pct),
      sub: `Target ${fmtPct(target, 0)}`,
      delta: gap === null ? null : `${gap >= 0 ? "+" : ""}${gap.toFixed(1)} pp`,
      tone: gap === null ? "neutral" : met ? "good" : "bad",
      meter: k.compliance_pct ?? 0,
      accent: met ? STATUS.compliant : STATUS.critical,
      foot: summary.policy?.name || "Baseline policy",
    }),
  );
}

const STATUS_KEYS = ["compliant", "non_compliant", "unknown"];
const STATUS_LABELS = {
  compliant: "Compliant", non_compliant: "Non-compliant", unknown: "Unknown / stale",
};

/* The engine groups by os_family, which stores the normalised key. The panel
 * is read by people, so it shows the platform name, not the key. */
const OS_FAMILY_LABELS = {
  rhel: "RHEL",
  windows: "Microsoft Windows",
  solaris: "Solaris",
  other: "Other",
};

function statusChartCard(title, subtitle, rows, mode = "columns") {
  // A vertical list shows every group. It used to stop at seven, so an
  // eighth business service simply disappeared from the panel with no
  // indication that anything was missing. Long lists scroll instead.
  const body = h("div", { class: mode === "rows" ? "chart-scroll" : "" });
  const legendBox = h("div", { class: "legend" });
  const cardEl = card(title, subtitle, h("div", {}, body, legendBox));
  if (mode === "rows") {
    stackedRows(body, {
      rows: rows.map((r) => ({ ...r, unknown: (r.unknown || 0) + (r.stale || 0) })),
      keys: STATUS_KEYS, labels: STATUS_LABELS, colors: STATUS, withPct: true,
      maxRows: rows.length,
    });
  } else {
    stackedColumns(body, {
      rows: rows.map((r) => ({ ...r, unknown: (r.unknown || 0) + (r.stale || 0) })),
      keys: STATUS_KEYS, labels: STATUS_LABELS, colors: STATUS, height: 230,
    });
  }
  legend(legendBox, STATUS_KEYS.map((key) => ({
    label: STATUS_LABELS[key], color: STATUS[key],
  })));
  return cardEl;
}

/* ------------------------------------------------------------------ dashboard */
async function renderDashboard({ silent = false } = {}) {
  // A timed refresh must not blank a working dashboard while it fetches: the
  // new content replaces the old one only after every request has succeeded.
  if (!silent) mount(view, spinnerRows(3));
  const [dims, periodData] = await Promise.all([loadDimensions(), api(
    "compliance/snapshot-periods").catch(() => ({ periods: [] })), api("me").then((me) => {
    state.user = { ...state.user, ...me };
  })]);
  // A stored period is only honoured while a snapshot still backs it; an
  // unavailable key falls back to Live instead of rendering an empty screen.
  const periods = periodData.periods || [];
  let period = state.dashboard.period;
  if (period && !periods.some((p) => p.key === period)) {
    period = "";
    state.dashboard.period = "";
    remember("patchtrack.period", "");
  }
  // A recorded period dictates the trend granularity: a month view compares
  // months, a quarter view compares quarters. Live keeps whichever basis the
  // reader last chose for it.
  const recordedPeriod = periods.find((p) => p.key === period);
  const basis = recordedPeriod
    ? (BASIS_BY_KIND[recordedPeriod.kind] || state.dashboard.basis)
    : state.dashboard.basis;
  const points = BASIS_POINTS[basis];
  const noun = BASIS_NOUNS[basis];
  const periodMode = Boolean(period);
  if (periodMode) {
    // Dimension filters are not applicable to a snapshot; clearing them here
    // means switching back to Live cannot surprise the reader with a filter
    // set they could not see.
    for (const key of Object.keys(state.filters)) state.filters[key] = "";
  }
  const query = periodMode ? "" : filterQuery();
  const [summary, trend] = await Promise.all([
    api(periodMode
      ? `compliance/summary?period=${encodeURIComponent(period)}`
      : `compliance/summary${query ? `?${query}` : ""}`),
    api(`compliance/trends?period=${basis}&points=${points}`),
  ]);
  const refreshSeconds = Number(state.dashboard.refreshSeconds) || 0;
  lastRefresh.textContent = refreshSeconds
    ? `Updated ${fmtTime()} · every ${
      REFRESH_CHOICES.find((c) => c.value === String(refreshSeconds))?.label || `${refreshSeconds}s`}`
    : `Updated ${fmtTime()} · auto-refresh off`;
  // freshnessLine builds a node, so it is mounted rather than stringified.
  // In period mode the provenance of the frozen snapshot replaces it: the age
  // of today's collection is not evidence about a past month.
  pageContext.replaceChildren(
    periodMode && summary.period
      ? periodLine(summary.period)
      : freshnessLine(summary.freshness));

  // One point per month/quarter/year, each the average of the daily snapshots
  // recorded in that period. The API sends the sample count so the table can
  // show how many observations a point rests on.
  const series = (trend.series || []).map((row) => ({
    label: row.label,
    value: row.compliance_pct,
    target: row.target_pct,
    total: row.total_servers,
    compliant: row.compliant_servers,
    samples: row.samples,
    change: null,
  }));
  series.forEach((point, index) => {
    if (index === 0 || point.value === null) return;
    const before = series[index - 1].value;
    point.change = before === null ? null : Number(point.value) - Number(before);
  });

  const donutBody = h("div");
  donut(donutBody, {
    rows: [
      { label: "Compliant", value: summary.kpis.compliant, color: STATUS.compliant },
      { label: "Non-compliant", value: summary.kpis.non_compliant, color: STATUS.non_compliant },
      { label: "Unknown", value: summary.kpis.unknown, color: STATUS.unknown },
      { label: "Stale", value: summary.kpis.stale, color: STATUS.stale },
    ],
    centerValue: fmtPct(summary.kpis.compliance_pct),
    centerLabel: "compliance",
  });
  const donutLegend = h("div", { class: "legend" });
  legend(donutLegend, [
    { label: `Compliant ${fmtNumber(summary.kpis.compliant)}`, color: STATUS.compliant },
    { label: `Non-compliant ${fmtNumber(summary.kpis.non_compliant)}`, color: STATUS.non_compliant },
    { label: `Unknown ${fmtNumber(summary.kpis.unknown)}`, color: STATUS.unknown },
    { label: `Stale ${fmtNumber(summary.kpis.stale)}`, color: STATUS.stale },
  ]);

  const trendBody = h("div");
  if (basis === "monthly") {
    trendLine(trendBody, {
      points: series, target: summary.kpis.target_pct,
      empty: "No daily snapshots recorded in the last 12 months.",
    });
  } else {
    targetColumns(trendBody, {
      points: series, target: summary.kpis.target_pct,
      empty: `No daily snapshots recorded in the last ${points} ${noun}.`,
    });
  }

  // The table is the accessible counterpart of the chart: same numbers, the
  // observation count behind each point, and the change against the period
  // before it.
  const periodTable = table([
    { label: "Period", key: "label" },
    { label: "Compliance", numeric: true,
      render: (r) => fmtPct(r.value) },
    { label: "Target", numeric: true, render: (r) => fmtPct(r.target, 0) },
    { label: "Change", numeric: true, render: (r) => {
      if (r.change === null || Number.isNaN(r.change)) return "—";
      return h("span", {
        style: { color: r.change >= 0 ? STATUS.compliant : STATUS.non_compliant },
      }, `${r.change >= 0 ? "+" : ""}${r.change.toFixed(1)} pp`);
    } },
    { label: "Servers (avg)", numeric: true, render: (r) => fmtNumber(r.total) },
    { label: "Snapshots", numeric: true, render: (r) => fmtNumber(r.samples) },
  ], series.slice().reverse(), {
    compact: true, export: false,
    empty: `No daily snapshots in the last ${points} ${noun}.`,
  });

  // The server list is a live view. In period mode it would contradict the
  // snapshot above it, so it is not fetched or shown at all.
  const recent = periodMode ? { items: [] } : await api(
    `compliance/servers?state=non_compliant&page_size=6${query ? `&${query}` : ""}`
  ).catch(() => ({ items: [] }));
  // This panel now shares its row with the period table, so it shows the
  // identifying and actionable columns only; address, OS and last-checked
  // detail remain one click away through "View all".
  const recentTable = table([
    { label: "Hostname", render: (r) => h("a", {
      href: "#", onclick: (e) => { e.preventDefault(); openServer(r.id); },
    }, r.hostname) },
    { label: "Environment", key: "environment" },
    { label: "Business service", key: "business_group" },
    { label: "Missing patches", numeric: true, render: (r) => fmtNumber(r.total_patches) },
    { label: "Last checked", render: (r) => fmtDate(r.last_assessed_at, true) },
  ], recent.items, {
    compact: true, export: false,
    empty: "No non-compliant servers match these filters.",
  });

  const topBusiness = (summary.by_business_group || [])
    .slice()
    .sort((a, b) => (b.non_compliant || 0) - (a.non_compliant || 0))
    .slice(0, 6);
  const topTable = table([
    { label: "Business service", key: "group" },
    { label: "Total", numeric: true, render: (r) => fmtNumber(r.total_servers) },
    { label: "Non-compliant", numeric: true,
      render: (r) => h("span", { style: { color: "#ff9a9a" } }, fmtNumber(r.non_compliant)) },
    { label: "%", numeric: true,
      render: (r) => (r.in_scope ? fmtPct((r.non_compliant / r.in_scope) * 100) : "—") },
  ], topBusiness, { compact: true, export: false });

  const controls = h("div", { class: "dashboard-controls" },
    select({
      label: "Timeframe",
      value: period || `live:${basis}`,
      options: timeframeOptions(periods),
      allLabel: null,
      onChange: (value) => {
        if (value.startsWith("live:")) {
          state.dashboard.period = "";
          state.dashboard.basis = value.slice("live:".length);
          remember("patchtrack.period", "");
          remember("patchtrack.basis", state.dashboard.basis);
        } else {
          const chosen = periods.find((p) => p.key === value);
          state.dashboard.period = value;
          if (chosen) {
            state.dashboard.basis = BASIS_BY_KIND[chosen.kind] || state.dashboard.basis;
            remember("patchtrack.basis", state.dashboard.basis);
          }
          remember("patchtrack.period", value);
          // A snapshot has no server rows, so a dimension filter cannot be
          // carried into a historical view. Clear it here rather than leaving
          // an invisible filter behind for the next return to Live.
          for (const key of Object.keys(state.filters)) state.filters[key] = "";
        }
        renderDashboard();
      },
    }),
    select({
      label: "Auto-refresh",
      value: state.dashboard.refreshSeconds,
      options: REFRESH_CHOICES,
      allLabel: null,
      onChange: (value) => {
        state.dashboard.refreshSeconds = value;
        remember("patchtrack.refresh", value);
        renderDashboard();
      },
    }));

  const periodTitle = {
    monthly: "Monthly compliance trend",
    quarterly: "Quarterly compliance",
    yearly: "Yearly compliance",
  }[basis];

  // A snapshot has no server rows to drill into, so the live list is replaced
  // by a note that says what the reader is looking at and what is unavailable.
  const bottomRow = periodMode
    ? periodNote(summary.period, summary.dimensions)
    : card("Recent non-compliant servers", "Highest outstanding patch counts",
        recentTable,
        h("a", { class: "card-link", href: "#/servers" }, "View all →"));

  mount(view,
    filterBar(renderDashboard, {
      extra: controls,
      note: periodMode
        ? "Historical snapshot — dimension filters apply to Live only"
        : "",
      skip: periodMode
        ? ["environment", "business_group", "application_group", "os_family",
           "patch_source"]
        : [],
    }),
    h("div", { class: "stack", style: { marginTop: "14px" } },
      kpiRow(summary),
      h("div", { class: "grid grid-3" },
        statusChartCard("Compliance by operating system",
          periodMode && !(summary.by_os || []).length
            ? "Not recorded on this snapshot"
            : "Server counts by OS family",
          (summary.by_os || []).map((row) => ({
            ...row, group: OS_FAMILY_LABELS[row.group] || row.group,
          }))),
        statusChartCard("Compliance by environment", "Server counts by environment",
          summary.by_environment || []),
        statusChartCard("Compliance by business service", "Server counts by business service",
          summary.by_business_group || [], "rows")),
      h("div", { class: "grid grid-3" },
        card("Patch status distribution",
          `${fmtNumber(summary.kpis.in_scope)} servers judged${
            summary.kpis.excluded ? ` · ${fmtNumber(summary.kpis.excluded)} excluded` : ""}`,
          h("div", {
            class: "row",
            style: { justifyContent: "center", gap: "18px", flexWrap: "wrap" },
          }, donutBody, donutLegend)),
        card(periodTitle,
          `Last ${points} ${noun} · average of daily snapshots · target ${
            fmtPct(summary.kpis.target_pct, 0)}`,
          trendBody),
        card("Top non-compliance by business service",
          periodMode ? `As recorded in ${summary.period.label}`
            : "Largest gaps by count",
          topTable)),
      h("div", { class: "grid grid-2" },
        card("Period summary",
          `Same observations as the chart · change is against the previous ${basis.slice(0, -2)}`,
          periodTable),
        bottomRow),
    ));
  startDashboardRefresh(refreshSeconds);
}

/* ------------------------------------------------------------------ servers */
async function renderServers(params) {
  const token = renderToken;
  mount(view, spinnerRows(3));
  await loadDimensions();
  if (token !== renderToken) return;
  const page = Number(params?.get("page") || 1);
  const stateFilter = params?.get("state") ?? "non_compliant";
  const search = params?.get("q") || "";
  const sort = params?.get("sort") || "total_patches";
  const direction = params?.get("dir") === "asc" ? "asc" : "desc";
  const query = new URLSearchParams({ page: String(page), page_size: "25" });
  if (stateFilter) query.set("state", stateFilter);
  const merged = new URLSearchParams(filterQuery());
  for (const [key, value] of merged) query.set(key, value);
  if (search) query.set("search", search);
  query.set("sort", sort);
  query.set("direction", direction);
  const data = await api(`compliance/servers?${query.toString()}`);
  if (token !== renderToken) return;   // a newer keystroke or navigation won

  // The list is paged by the service, so the sort is part of the query, not a
  // client-side rearrangement of the 25 rows on screen.
  const link = (changes = {}) => {
    const next = new URLSearchParams();
    const keep = { state: stateFilter, q: search, sort, dir: direction, ...changes };
    for (const [key, value] of Object.entries(keep)) {
      if (value && !(key === "dir" && value === "desc")
          && !(key === "sort" && value === "total_patches")) next.set(key, value);
    }
    navigate("servers", next);
  };
  const searchInput = searchBox({
    route: "servers",
    value: search,
    placeholder: "Any column — host, IP, OS, environment…",
    onChange: debounce((term) => searchAction("servers", term,
      () => link({ q: term, page: "" })), 500),
  });
  // Each field carries the direction that reads naturally for it, so choosing
  // "Hostname" starts A→Z while "Missing patches" starts worst-first. The order
  // button is still there to reverse either.
  const SORTS = [
    { value: "total_patches", label: "Missing patches", dir: "desc" },
    { value: "critical_count", label: "Critical findings", dir: "desc" },
    { value: "hostname", label: "Hostname", dir: "asc" },
    { value: "last_assessed_at", label: "Last assessed", dir: "desc" },
    { value: "environment", label: "Environment", dir: "asc" },
    { value: "business_group", label: "Business service", dir: "asc" },
    { value: "os_name", label: "Operating system", dir: "asc" },
    { value: "state", label: "Compliance state", dir: "asc" },
  ];
  const sortSelect = select({
    label: "Sort by", allLabel: null,
    value: sort,
    options: SORTS.map(({ value, label }) => ({ value, label })),
    onChange: (value) => link({
      sort: value, dir: SORTS.find((entry) => entry.value === value)?.dir || "asc", page: "",
    }),
  });
  const directionButton = h("button", {
    type: "button", class: "btn btn-sm",
    title: "Reverse the sort direction",
    onclick: () => link({ dir: direction === "asc" ? "desc" : "asc", page: "" }),
  }, direction === "asc" ? "↑ Ascending" : "↓ Descending");

  const body = table([
    { label: "Hostname", render: (r) => h("a", {
      href: "#", onclick: (e) => { e.preventDefault(); openServer(r.id); },
    }, r.hostname) },
    { label: "IP address", key: "ip_address", mono: true },
    { label: "Operating system", render: (r) => `${r.os_name} ${r.os_version}`.trim() },
    { label: "Environment", key: "environment" },
    { label: "Business service", key: "business_group" },
    { label: "Status", render: (r) => statePill(r.state) },
    { label: "Critical", numeric: true, render: (r) => fmtNumber(r.critical_count) },
    { label: "Patches", numeric: true, render: (r) => fmtNumber(r.total_patches) },
    { label: "Last assessed", render: (r) => fmtDate(r.last_assessed_at, true) },
  ], data.items, {
    compact: true,
    exportRows: () => allPages("compliance/servers", query),
  });

  const stateBar = h("div", { class: "row", style: { marginBottom: "12px" } },
    ...[
      ["non_compliant", "Non-compliant"], ["compliant", "Compliant"],
      ["unknown", "Unknown"], ["stale", "Stale"],
    ].map(([key, label]) => h("button", {
      class: `btn btn-sm ${stateFilter === key ? "btn-primary" : ""}`,
      onclick: () => link({ state: key, page: "" }),
    }, label)),
    h("div", { class: "right muted small" },
      `${fmtNumber(data.total)} matching servers`));

  const pages = Math.max(1, Math.ceil(data.total / 25));
  const pager = h("div", { class: "pagination" },
    h("button", {
      class: "btn btn-sm", disabled: page <= 1,
      onclick: () => link({ page: page - 1 }),
    }, "Previous"),
    h("span", { class: "muted" }, `Page ${page} of ${pages}`),
    h("button", {
      class: "btn btn-sm", disabled: page >= pages,
      onclick: () => link({ page: page + 1 }),
    }, "Next"));

  mount(view,
    filterBar(() => renderServers(new URLSearchParams({ state: stateFilter })),
      { skip: ["patch_source"],
        extra: h("div", { class: "row", style: { alignItems: "flex-end" } },
          h("div", { class: "field" }, h("label", {}, "Search"), searchInput),
          sortSelect,
          h("div", { class: "field" },
            h("span", { class: "field-label" }, "Order"), directionButton)) }),
    h("div", { class: "stack", style: { marginTop: "14px" } },
      stateBar,
      card("Servers", "Drill into any server for its assessment and findings", body, pager)));
  restoreSearchFocus("servers", searchInput);
}

/* ------------------------------------------------------- justifications */
/* A failing state is verified by a person, not silenced by the UI. The
 * compliance engine still reports exactly what the collectors found; the
 * justification sits beside it, is versioned, and carries the evidence the
 * operator relied on. */
const JUSTIFICATION_REASONS = [
  ["accepted_risk", "Accepted risk"],
  ["compensating_control", "Compensating control in place"],
  ["scheduled_remediation", "Remediation scheduled"],
  ["false_positive", "False positive / assessment error"],
  ["decommissioning", "Asset being decommissioned"],
  ["collection_gap", "Collection or authentication gap"],
  ["investigation", "Under investigation"],
];
const EVIDENCE_TYPES = [
  "image/png", "image/jpeg", "application/pdf", "text/plain", "text/csv",
  "application/msword",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/vnd.ms-excel",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
];

function fileToEvidence(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({
      filename: file.name,
      content_type: file.type || "application/octet-stream",
      data: String(reader.result || ""),
    });
    reader.onerror = () => reject(new Error(`${file.name} could not be read`));
    reader.readAsDataURL(file);
  });
}

function justificationCard(asset, justifications, justifiable, refresh) {
  const active = justifications.active;
  const status = h("div");
  const evidenceRows = (active?.evidence || []);
  const activeBlock = active
    ? h("div", { class: "stack" },
      h("div", { class: "row between" },
        h("div", {},
          h("strong", {}, active.reason_label),
          h("div", { class: "muted small" },
            `Recorded by ${active.created_by} · `,
            fmtDate(active.created_at, true),
            active.compliance_state
              ? ` · state at the time: ${active.compliance_state.replaceAll("_", " ")}`
              : "")),
        can("compliance:justify")
          ? h("button", {
            class: "btn btn-sm",
            onclick: async () => {
              if (!window.confirm("Withdraw this justification? Its history and "
                + "evidence are kept.")) return;
              status.replaceChildren();
              try {
                await api(`inventory/assets/${asset.id}/justifications/${active.id}`, {
                  method: "DELETE",
                });
                await refresh();
              } catch (error) {
                status.appendChild(alert("error", error.message));
              }
            },
          }, "Withdraw")
          : null),
      h("p", { style: { margin: 0, whiteSpace: "pre-wrap" } }, active.statement),
      active.remediation_plan
        ? h("div", {}, h("span", { class: "muted small" }, "Remediation plan: "),
          active.remediation_plan)
        : null,
      active.remediation_target
        ? h("div", { class: "muted small" },
          `Target date: ${fmtDate(active.remediation_target)}`)
        : null,
      evidenceRows.length
        ? table([
          { label: "Evidence", key: "filename" },
          { label: "Type", key: "content_type" },
          { label: "Size", numeric: true,
            render: (r) => `${(r.byte_size / 1024).toFixed(0)} KB` },
          { label: "Uploaded", render: (r) => fmtDate(r.uploaded_at, true) },
          { label: "", export: false, render: (r) => h("button", {
            class: "btn btn-sm",
            onclick: () => download(
              `inventory/assets/${asset.id}/justifications/${active.id}`
              + `/evidence/${r.id}`, r.filename),
          }, "Download") },
        ], evidenceRows, { compact: true, export: false })
        : h("div", { class: "muted small" }, "No evidence attached."),
      status)
    : h("div", { class: "muted small" },
      justifiable
        ? "No justification recorded. The state above is what the collected "
          + "evidence shows."
        : "No justification recorded.");

  let form = null;
  if (can("compliance:justify") && justifiable) {
    const reason = h("select", { id: "justification-reason" },
      ...JUSTIFICATION_REASONS.map(([value, label]) =>
        h("option", { value }, label)));
    const statement = h("textarea", {
      id: "justification-statement", rows: "3", minLength: "20", maxLength: "4000",
      placeholder: "What did you verify, and why is the asset in this state?",
      style: { width: "100%" },
    });
    const plan = h("input", {
      id: "justification-plan", type: "text", maxLength: "2000",
      placeholder: "What will be done, and by whom?",
    });
    const target = h("input", { id: "justification-target", type: "date" });
    const files = h("input", {
      id: "justification-evidence", type: "file", multiple: true,
      accept: EVIDENCE_TYPES.join(","),
    });
    const submit = h("button", { class: "btn btn-primary", type: "submit" },
      "Record verification");
    const formEl = h("form", {
      class: "stack",
      onsubmit: async (event) => {
        event.preventDefault();
        status.replaceChildren();
        const chosen = [...(files.files || [])];
        if (chosen.length > 5) {
          status.appendChild(alert("error", "Attach at most 5 evidence files."));
          return;
        }
        const tooBig = chosen.find((file) => file.size > 5 * 1024 * 1024);
        if (tooBig) {
          status.appendChild(alert("error", `${tooBig.name} is larger than 5 MB.`));
          return;
        }
        submit.disabled = true;
        try {
          const evidence = await Promise.all(chosen.map(fileToEvidence));
          await api(`inventory/assets/${asset.id}/justifications`, {
            method: "POST",
            body: {
              reason_code: reason.value,
              statement: statement.value,
              remediation_plan: plan.value,
              remediation_target: target.value || null,
              evidence,
            },
          });
          await refresh();
        } catch (error) {
          status.appendChild(alert("error", error.message));
          submit.disabled = false;
        }
      },
    },
    h("div", { class: "row" },
      h("div", { class: "field", style: { minWidth: "240px" } },
        h("label", { for: "justification-reason" }, "Reason"), reason),
      h("div", { class: "field", style: { minWidth: "170px" } },
        h("label", { for: "justification-target" }, "Remediation target"), target)),
    h("div", { class: "field" },
      h("label", { for: "justification-statement" },
        "What was verified (at least 20 characters)"), statement),
    h("div", { class: "field" },
      h("label", { for: "justification-plan" }, "Remediation plan"), plan),
    h("div", { class: "field" },
      h("label", { for: "justification-evidence" },
        "Evidence (PDF, image, text, CSV or Office; up to 5 files, 5 MB each)"),
      files),
    submit);
    form = h("div", { class: "stack" },
      h("h3", { style: { margin: "8px 0 0", fontSize: "13.5px" } },
        active ? "Record a new verification (supersedes the current one)"
          : "Record verification"),
      formEl);
  }

  const history = (justifications.history || [])
    .filter((row) => row.status !== "active");
  const historyBlock = history.length
    ? card("Justification history",
      "Superseded and withdrawn records are retained for audit",
      table([
        { label: "Recorded", render: (r) => fmtDate(r.created_at, true) },
        { label: "By", key: "created_by" },
        { label: "Reason", key: "reason_label" },
        { label: "State", key: "compliance_state" },
        { label: "Status", render: (r) => pill(r.status, r.status) },
        { label: "Evidence", numeric: true,
          render: (r) => (r.evidence || []).length },
      ], history, { compact: true, export: false }))
    : null;

  return h("div", { class: "stack" },
    card("Verification and justification",
      justifiable
        ? "This asset is non-compliant, stale or unknown. Record what you "
          + "verified and attach evidence; the compliance state itself is not "
          + "changed by this record."
        : "Recorded verifications for this asset.",
      h("div", { class: "stack" }, activeBlock, status, form)),
    historyBlock);
}

/* ------------------------------------------------------------------ server detail */
async function openServer(assetId) {
  const close = modal("Server detail", spinnerRows(2));
  try {
    const data = await api(`inventory/assets/${assetId}`);
    const asset = data.asset;
    const assessment = data.assessment;
    const findings = data.findings || [];
    const justifications = data.justifications || { active: null, history: [] };
    const justifiable = ["non_compliant", "stale", "unknown"]
      .includes(asset.compliance_state || "unknown");
    const reopen = async () => { close(); await openServer(assetId); };
    const detail = h("div", { class: "stack" },
      h("div", { class: "row between" },
        h("div", {},
          h("h3", { style: { margin: "0 0 4px" } }, asset.hostname),
          h("div", { class: "muted small" },
            `${asset.os_name} ${asset.os_version} · ${asset.environment} · `,
            asset.business_group)),
        h("div", { class: "row", style: { alignItems: "center" } },
          can("inventory:manage")
            ? h("button", {
              class: "btn btn-sm",
              onclick: () => { close(); editPlacement(asset); },
            }, "Edit environment / business service")
            : null,
          statePill(asset.compliance_state))),
      h("dl", { class: "kv" },
        h("dt", {}, "AssetExplorer ID"), h("dd", { class: "mono" }, asset.assetexplorer_id),
        h("dt", {}, "Environment"), h("dd", {}, placementText(asset, "environment")),
        h("dt", {}, "Business service"),
        h("dd", {}, placementText(asset, "business_group")),
        asset.override_set_at
          ? h("dt", {}, "Placement override")
          : null,
        asset.override_set_at
          ? h("dd", {}, `${asset.override_reason} — ${asset.override_set_by}, ` +
            fmtDate(asset.override_set_at, true))
          : null,
        h("dt", {}, "FQDN"), h("dd", { class: "mono" }, asset.fqdn || "—"),
        h("dt", {}, "IP address"), h("dd", { class: "mono" }, asset.ip_address || "—"),
        h("dt", {}, "Owner"), h("dd", {}, asset.asset_owner || "—"),
        h("dt", {}, "Department"), h("dd", {}, asset.department || "—"),
        h("dt", {}, "Location"), h("dd", {}, asset.location || "—"),
        h("dt", {}, "Lifecycle"), h("dd", {}, asset.lifecycle_status),
        h("dt", {}, "Patch source"), h("dd", {}, asset.patch_source || "—"),
        h("dt", {}, "Inventory synced"), h("dd", {}, fmtDate(asset.last_synced_at, true)),
        h("dt", {}, "Last assessment"), h("dd", {}, fmtDate(asset.last_assessed_at, true)),
        h("dt", {}, "Compliance reason"), h("dd", {}, asset.compliance_reason || "—")),
      h("div", { class: "alert warn" }, h("span", { class: "ico" }, "!"),
        h("div", {}, "Remediation is performed in the authoritative patch-management " +
          "system. PatchTrack records status, references and the operator's " +
          "verification of a failing state.")),
      justificationCard(asset, justifications, justifiable, reopen),
      card("Outstanding findings",
        assessment ? `${fmtNumber(findings.length)} recorded · assessment ${fmtDate(assessment.assessed_at, true)}` : "No assessment recorded",
        table([
          { label: "Advisory", key: "advisory_id", mono: true },
          { label: "Title", render: (r) => r.title },
          { label: "Severity", render: (r) => pill(r.severity, r.severity) },
          { label: "Category", key: "category" },
          { label: "Released", render: (r) => fmtDate(r.released_at) },
          { label: "Source", key: "source_system" },
        ], findings, { compact: true, empty: "No outstanding findings recorded." })),
      data.assessment_history?.length
        ? card("Assessment history", "Most recent first",
          table([
            { label: "Assessed", render: (r) => fmtDate(r.assessed_at, true) },
            { label: "Status", key: "status" },
            { label: "Total", numeric: true, key: "total_patches" },
            { label: "Critical", numeric: true, key: "critical_count" },
            { label: "Important", numeric: true, key: "important_count" },
          ], data.assessment_history, { compact: true }))
        : null,
    );
    close();
    modal(`Server · ${asset.hostname}`, detail);
  } catch (error) {
    close();
    modal("Server detail", alert("error", error.message));
  }
}

/* ------------------------------------------------------- placement overrides */
/* Environment and business service come from AssetExplorer. An operator can
 * correct either locally: the correction is stored beside the source value, so
 * the next collection keeps updating its own columns and the override stands.
 * Everything that reads placement — lists, the compliance engine and reports
 * — reads the effective value, and the source answer is always shown next to
 * it. */
function placementText(asset, field) {
  // Effective value differs from the source either because an operator
  // overrode it or because an administrator renamed the Settings value. Both
  // are shown with the source answer so nothing is hidden.
  const source = asset[`${field}_source`];
  if (source === null || source === undefined || source === asset[field]) {
    return asset[field];
  }
  return `${asset[field]} (AssetExplorer: ${source})`;
}

function placementCell(asset, field) {
  const overridden = asset[`${field}_overridden`];
  const source = asset[`${field}_source`];
  // The tooltip covers both ways the effective value can differ from the
  // source: a local override, or a Settings rename the server now resolves to.
  const title = overridden
    ? `Local override — AssetExplorer reports ${source}`
      + (asset.override_reason ? `; reason: ${asset.override_reason}` : "")
    : (source && source !== asset[field]
      ? `Renamed in Settings — AssetExplorer reports ${source}` : null);
  return h("span", { class: "placement", title },
    asset[field],
    overridden ? h("span", { class: "override-mark" }, "local") : null);
}

/* The environment / business-service picker.
 *
 * Once an administrator has curated the list in Settings, the editor offers a
 * select rather than free text, so a correction cannot introduce a second
 * spelling of a value that already exists. With no list configured (a fresh
 * installation) it falls back to a text input with the values already in use,
 * so the dialog still works before anyone has visited Settings. */
function placementInput(kind, asset, options, listId) {
  const current = asset[kind] || "";
  const source = asset[`${kind}_source`] || "";
  const overridden = Boolean(asset[`${kind}_overridden`]);
  const values = (options || []).map((option) => option.value);
  if (values.length === 0) {
    return h("input", {
      type: "text", maxLength: 128, value: current, list: listId,
    });
  }
  const select = h("select", {});
  select.appendChild(h("option", { value: "" },
    source ? `Use AssetExplorer value — ${source}` : "Use AssetExplorer value"));
  for (const value of values) select.appendChild(h("option", { value }, value));
  // A value can leave the Settings list while a server still holds it. Keep it
  // selectable rather than silently changing the server on the next save.
  if (current && !values.includes(current)) {
    select.appendChild(h("option", { value: current }, `${current} (not in Settings)`));
  }
  select.value = overridden ? current : "";
  return select;
}

async function editPlacement(asset) {
  const error = h("div");
  const dimensions = state.dimensions || {};
  let options = { environments: [], business_groups: [] };
  try {
    options = await api("inventory/settings/placement");
  } catch {
    // Keep the dialog usable if the list cannot be read; free text applies.
  }
  const envInput = placementInput("environment", asset, options.environments,
    "placement-environments");
  const groupInput = placementInput("business_group", asset, options.business_groups,
    "placement-groups");
  const reason = h("textarea", {
    rows: 3, maxLength: 500,
    placeholder: "Why does this server differ from AssetExplorer? This is recorded in the audit trail.",
  });
  const buttons = [];
  let saving = false;
  let close = () => {};

  async function refresh() {
    // A placement can move a server between policy scope rules, so persisted
    // compliance states are refreshed. A failed refresh must not look like a
    // failed save — the override is already stored.
    try {
      await api("compliance/evaluate", { method: "POST" });
    } catch (error_) {
      console.warn("compliance refresh after a placement change failed", error_);
    }
    await renderRoute();
  }

  const save = h("button", { class: "btn btn-primary" }, "Save override");
  save.onclick = async () => {
    if (saving) return;
    const environment = envInput.value.trim();
    const businessGroup = groupInput.value.trim();
    const why = reason.value.trim();
    error.replaceChildren();
    if (why.length < 3) {
      error.appendChild(alert("error", "A reason of at least 3 characters is required."));
      return;
    }
    saving = true;
    save.disabled = true;
    save.textContent = "Saving…";
    try {
      await api(`inventory/assets/${asset.id}/overrides`, {
        method: "PUT",
        body: { environment, business_group: businessGroup, reason: why },
      });
      close();
      await refresh();
    } catch (error_) {
      saving = false;
      save.disabled = false;
      save.textContent = "Save override";
      error.appendChild(alert("error", `The override was not saved: ${error_.message}`));
    }
  };
  buttons.push(save, h("button", { class: "btn", onclick: () => close() }, "Cancel"));

  if (asset.environment_overridden || asset.business_group_overridden) {
    const restore = h("button", { class: "btn" }, "Use AssetExplorer values");
    restore.onclick = async () => {
      error.replaceChildren();
      try {
        await api(`inventory/assets/${asset.id}/overrides`, { method: "DELETE" });
        close();
        await refresh();
      } catch (error_) {
        error.appendChild(alert("error",
          `The override was not removed: ${error_.message}`));
      }
    };
    buttons.push(restore);
  }

  close = modal(`Environment and business service · ${asset.hostname}`,
    h("div", { class: "stack" },
      error,
      h("div", { class: "alert" }, h("span", { class: "ico" }, "i"),
        h("div", {},
          "This is a local override, not an edit in AssetExplorer. The sync keeps " +
          "running and keeps updating its own values; the override stays until you " +
          "remove it. Leave a field blank to use the AssetExplorer value.")),
      h("div", { class: "grid grid-2" },
        h("div", { class: "field" }, h("label", {}, "Environment"), envInput,
          h("span", { class: "muted small" },
            `AssetExplorer: ${asset.environment_source || "—"}`)),
        h("div", { class: "field" }, h("label", {}, "Business service"), groupInput,
          h("span", { class: "muted small" },
            `AssetExplorer: ${asset.business_group_source || "—"}`))),
      h("datalist", { id: "placement-environments" },
        (dimensions.environment || []).map((v) => h("option", { value: v }))),
      h("datalist", { id: "placement-groups" },
        (dimensions.business_group || []).map((v) => h("option", { value: v }))),
      h("div", { class: "field" }, h("label", {}, "Reason"), reason)),
    buttons);
}

/* ------------------------------------------------------------------ findings */
async function renderFindings() {
  mount(view, spinnerRows(3));
  await loadDimensions();
  const severity = "";
  const query = new URLSearchParams({ page_size: "100" });
  const data = await api(`compliance/findings?${query}`);
  const grid2 = h("div", { class: "grid grid-2" });
  const sevCounts = {};
  for (const item of data.items) sevCounts[item.severity] = (sevCounts[item.severity] || 0) + 1;
  const sevBody = h("div");
  severityBars(sevBody, Object.entries(sevCounts).map(([severityKey, count]) => ({
    severity: severityKey, count,
  })));
  grid2.appendChild(card("Findings by severity", "Current outstanding patch findings", sevBody));
  grid2.appendChild(card("Remediation guidance", "How deadlines are derived", h("div", { class: "stack small secondary" },
    h("p", {}, "Remediation windows come from the active compliance policy: critical 7 days, " +
      "important 30 days, moderate 90 days, low 180 days from publication."),
    h("p", {}, "Overdue items are visible where the source system reports a release date. " +
      "PatchTrack never initiates deployment."))));

  mount(view,
    filterBar(renderFindings, { skip: ["patch_source"] }),
    h("div", { class: "stack", style: { marginTop: "14px" } },
      grid2,
      listPanel("findings", {
        title: "Outstanding patch findings",
        subtitle: `${fmtNumber(data.total)} findings recorded`,
        columns: [
          { label: "Hostname", render: (r) => h("a", {
            href: "#", onclick: (e) => { e.preventDefault(); openServer(r.asset_id); },
          }, r.hostname) },
          { label: "Advisory", key: "advisory_id", mono: true },
          { label: "Title", render: (r) => r.title },
          { label: "Severity", render: (r) => pill(r.severity, r.severity) },
          { label: "Source", key: "source_system" },
          { label: "Business service", key: "business_group" },
          { label: "Released", render: (r) => fmtDate(r.released_at) },
        ],
        rows: data.items,
        searchPlaceholder: "Hostname, advisory, title…",
        facets: [
          { field: "severity", label: "Severity" },
          { field: "source_system", label: "Source" },
          { field: "business_group", label: "Business service" },
        ],
        sorts: [
          { field: "severity", label: "Severity",
            rank: ["critical", "important", "moderate", "low"] },
          { field: "released_at", label: "Released", dir: "desc" },
          { field: "hostname", label: "Hostname" },
          { field: "advisory_id", label: "Advisory" },
        ],
        empty: "No outstanding findings match this selection.",
      })));
}

/* ------------------------------------------------------------------ history */
async function renderTrends() {
  mount(view, spinnerRows(3));
  const [trends, snapshots] = await Promise.all([
    api("compliance/trends?months=12"),
    api("compliance/snapshots?limit=40"),
  ]);
  const monthly = (trends.monthly || []).map((row) => ({
    label: new Date(row.month).toLocaleString(undefined, { month: "short", year: "2-digit" }),
    value: row.compliance_pct,
  }));
  const quarterly = (trends.quarterly || []).slice().reverse().map((row) => {
    const start = new Date(row.period_start);
    return {
      label: `Q${Math.floor(start.getMonth() / 3) + 1} ${start.getFullYear()}`,
      value: row.compliance_pct,
    };
  });
  const policy = await api("compliance/policies/active").catch(() => null);
  const target = policy?.policy?.target_pct ?? 95;
  const trendBody = h("div");
  trendLine(trendBody, { points: monthly, target, height: 250 });
  const quarterBody = h("div");
  targetColumns(quarterBody, { points: quarterly, target, height: 250 });

  mount(view, h("div", { class: "stack" },
    h("div", { class: "grid grid-2" },
      card("Monthly compliance", "Rolled up from daily snapshots", trendBody),
      card("Quarterly compliance", "Immutable quarterly snapshots", quarterBody)),
    listPanel("snapshots", {
      title: "Snapshot ledger",
      subtitle: "Every capture is retained; reports reference a specific snapshot",
      columns: [
        { label: "Captured", render: (r) => fmtDate(r.captured_at, true) },
        { label: "Scope", key: "scope" },
        { label: "Period start", render: (r) => fmtDate(r.period_start) },
        { label: "Total", numeric: true, key: "total_servers" },
        { label: "Compliant", numeric: true, key: "compliant_servers" },
        { label: "Non-compliant", numeric: true, key: "non_compliant_servers" },
        { label: "Compliance", numeric: true, render: (r) => fmtPct(r.compliance_pct) },
        { label: "Target", numeric: true, render: (r) => fmtPct(r.target_pct, 0) },
        { label: "By", key: "generated_by" },
      ],
      rows: snapshots,
      searchPlaceholder: "Scope, generator, date…",
      facets: [
        { field: "scope", label: "Scope" },
        { field: "generated_by", label: "Generated by" },
      ],
      sorts: [
        { field: "captured_at", label: "Captured", dir: "desc" },
        { field: "period_start", label: "Period start", dir: "desc" },
        { field: "compliance_pct", label: "Compliance", numeric: true, dir: "desc" },
        { field: "total_servers", label: "Total servers", numeric: true, dir: "desc" },
        { field: "scope", label: "Scope" },
      ],
      empty: "No snapshots match this selection.",
    })));
}

/* ------------------------------------------------------------------ inventory */
/* Inventory-wide totals for the register on screen. They follow the same
 * filters as the list (so a narrowed register reports on what is selected),
 * but the Status filter is excluded upstream: selecting "Non-compliant" must
 * not make the Compliant tile read zero. */
function inventoryStatsPanel(stats) {
  const total = stats?.total || 0;
  const excluded = stats?.excluded || 0;
  const population = Math.max(0, total - excluded);
  const compliant = stats?.compliant || 0;
  const nonCompliant = stats?.non_compliant || 0;
  const unknown = stats?.unknown || 0;
  const stale = stats?.stale || 0;
  const notAssessed = stats?.not_assessed || 0;
  const noData = unknown + stale + notAssessed;
  const pct = (value) => (population ? fmtPct((value / population) * 100) : null);
  const unknownParts = [
    unknown ? `${fmtNumber(unknown)} unknown` : "",
    stale ? `${fmtNumber(stale)} stale` : "",
    notAssessed ? `${fmtNumber(notAssessed)} not assessed` : "",
  ].filter(Boolean);
  return h("div", { class: "grid grid-4" },
    statTile({
      label: "Total assets",
      value: fmtNumber(total),
      sub: excluded
        ? `${fmtNumber(excluded)} excluded of ${fmtNumber(total)}` : "",
      foot: "Every synced asset, in and out of compliance scope",
      accent: "#3987e5",
    }),
    statTile({
      label: "Compliant",
      value: fmtNumber(compliant),
      sub: pct(compliant),
      tone: "good",
      meter: population ? (compliant / population) * 100 : 0,
      accent: STATUS.compliant,
      foot: "No outstanding blocking findings",
    }),
    statTile({
      label: "Non-compliant",
      value: fmtNumber(nonCompliant),
      delta: pct(nonCompliant),
      tone: "bad",
      meter: population ? (nonCompliant / population) * 100 : 0,
      accent: STATUS.non_compliant,
      foot: "Blocking findings outstanding",
    }),
    statTile({
      label: "Unknown / no data",
      value: fmtNumber(noData),
      sub: unknownParts.join(" · "),
      delta: pct(noData),
      tone: "neutral",
      meter: population ? (noData / population) * 100 : 0,
      accent: STATUS.unknown,
      foot: "Never counted as compliant",
    }),
  );
}

async function renderInventory(params) {
  const token = renderToken;
  mount(view, spinnerRows(3));
  await loadDimensions();
  if (token !== renderToken) return;
  const page = Number(params?.get("page") || 1);
  const search = params?.get("q") || "";
  // The status filter is an exact state, separate from the free-text search:
  // "Compliant" in the search box must not also match "Non-compliant".
  const stateFilter = params?.get("state") || "";
  const sort = params?.get("sort") || "hostname";
  const direction = params?.get("dir") === "desc" ? "desc" : "asc";
  const query = new URLSearchParams({ page: String(page), page_size: "25" });
  const filters = new URLSearchParams(filterQuery());
  for (const [key, value] of filters) query.set(key, value);
  if (search) query.set("search", search);
  if (stateFilter) query.set("compliance_state", stateFilter);
  query.set("sort", sort);
  query.set("direction", direction);
  const data = await api(`inventory/assets?${query}`);
  if (token !== renderToken) return;   // a newer keystroke or navigation won
  const link = (changes = {}) => {
    const next = new URLSearchParams();
    const keep = { q: search, state: stateFilter, sort, dir: direction, ...changes };
    for (const [key, value] of Object.entries(keep)) {
      if (value && !(key === "dir" && value === "asc")
          && !(key === "sort" && value === "hostname")) next.set(key, value);
    }
    navigate("inventory", next);
  };
  const searchInput = searchBox({
    route: "inventory",
    value: search,
    placeholder: "Any column — host, OS, owner, service…",
    minWidth: "250px",
    onChange: debounce((term) => searchAction("inventory", term,
      () => link({ q: term, page: "" })), 500),
  });
  const statusSelect = select({
    label: "Status", value: stateFilter, allLabel: "All statuses",
    options: [
      { value: "compliant", label: "Compliant" },
      { value: "non_compliant", label: "Non-compliant" },
      { value: "unknown", label: "Unknown" },
      { value: "stale", label: "Stale" },
    ],
    onChange: (value) => link({ state: value, page: "" }),
  });
  const INVENTORY_SORTS = [
    { value: "hostname", label: "Hostname", dir: "asc" },
    { value: "os_family", label: "Operating system", dir: "asc" },
    { value: "environment", label: "Environment", dir: "asc" },
    { value: "business_group", label: "Business service", dir: "asc" },
    { value: "lifecycle_status", label: "Lifecycle", dir: "asc" },
    { value: "patch_source", label: "Patch source", dir: "asc" },
    { value: "state", label: "Compliance state", dir: "asc" },
  ];
  const sortSelect = select({
    label: "Sort by", allLabel: null, value: sort,
    options: INVENTORY_SORTS.map(({ value, label }) => ({ value, label })),
    onChange: (value) => link({
      sort: value,
      dir: INVENTORY_SORTS.find((entry) => entry.value === value)?.dir || "asc",
      page: "",
    }),
  });
  const directionButton = h("button", {
    type: "button", class: "btn btn-sm",
    title: "Reverse the sort direction",
    onclick: () => link({ dir: direction === "asc" ? "desc" : "asc", page: "" }),
  }, direction === "asc" ? "↑ Ascending" : "↓ Descending");
  const pages = Math.max(1, Math.ceil(data.total / 25));
  mount(view,
    filterBar(() => renderInventory(new URLSearchParams({ state: stateFilter })), {
      skip: ["patch_source"],
      extra: h("div", { class: "row", style: { alignItems: "flex-end" } },
        h("div", { class: "field" }, h("label", {}, "Search"), searchInput),
        statusSelect,
        sortSelect,
        h("div", { class: "field" },
          h("span", { class: "field-label" }, "Order"), directionButton)),
    }),
    h("div", { class: "stack", style: { marginTop: "14px" } },
      inventoryStatsPanel(data.stats),
      card("Authoritative inventory", "Synchronised from ManagedEngine AssetExplorer",
        table([
          { label: "Hostname", render: (r) => h("a", {
            href: "#", onclick: (e) => { e.preventDefault(); openServer(r.id); },
          }, r.hostname) },
          { label: "Asset ID", key: "assetexplorer_id", mono: true },
          { label: "IP address", key: "ip_address", mono: true },
          { label: "OS", render: (r) => `${r.os_name} ${r.os_version}`.trim() },
          { label: "Environment", render: (r) => placementCell(r, "environment") },
          { label: "Business service", render: (r) => placementCell(r, "business_group") },
          { label: "Lifecycle", key: "lifecycle_status" },
          { label: "Patch source", key: "patch_source" },
          { label: "Status", render: (r) => statePill(r.compliance_state) },
          { label: "Inventory sync", render: (r) => fmtRelative(r.last_synced_at) },
          can("inventory:manage") ? {
            label: "Placement", export: false,
            render: (r) => h("button", {
              class: "btn btn-sm",
              title: "Override the environment or business service locally",
              onclick: () => editPlacement(r),
            }, "Edit"),
          } : null,
        ].filter(Boolean), data.items, {
          compact: true,
          exportRows: () => allPages("inventory/assets", query),
        }),
        h("div", { class: "pagination" },
          h("button", {
            class: "btn btn-sm", disabled: page <= 1,
            onclick: () => link({ page: page - 1 }),
          }, "Previous"),
          h("span", { class: "muted" },
            `${fmtNumber(data.total)} assets · page ${page} of ${pages}`),
          h("button", {
            class: "btn btn-sm", disabled: page >= pages,
            onclick: () => link({ page: page + 1 }),
          }, "Next")))));
  restoreSearchFocus("inventory", searchInput);
}

/* ------------------------------------------------------------------ reconciliation */
async function renderReconciliation() {
  mount(view, spinnerRows(3));
  const data = await api("inventory/reconciliation");
  const summaryTable = table([
    { label: "Source", key: "source_system" },
    { label: "Reason", key: "reason" },
    { label: "Open", numeric: true, key: "n" },
  ], data.summary, { compact: true, empty: "No open conflicts." });
  const items = {
    title: "Conflict queue",
    subtitle: `${data.items.length} item(s) awaiting review`,
    columns: [
      { label: "Detected", render: (r) => fmtDate(r.detected_at, true) },
      { label: "Source", key: "source_system" },
      { label: "Source ref", key: "source_ref", mono: true },
      { label: "Match key", key: "match_key", mono: true },
      { label: "Reason", key: "reason" },
      { label: "Asset", render: (r) => r.asset_hostname || "—" },
      {
        label: "Action", render: (r) => (can("integrations:manage")
          ? h("button", {
            class: "btn btn-sm",
            onclick: () => resolveConflict(r),
          }, "Resolve") : "—"),
      },
    ],
    rows: data.items,
    searchPlaceholder: "Source, reference, match key…",
    facets: [
      { field: "source_system", label: "Source" },
      { field: "reason", label: "Reason" },
    ],
    sorts: [
      { field: "detected_at", label: "Detected", dir: "desc" },
      { field: "source_system", label: "Source" },
      { field: "reason", label: "Reason" },
      { field: "source_ref", label: "Source reference" },
    ],
    empty: "No open reconciliation conflicts match this selection.",
  };
  mount(view, h("div", { class: "stack" },
    alert("warn", "Records that cannot be matched unambiguously are never attached to an " +
      "asset automatically. Resolve them here once the correct asset is confirmed."),
    card("Open conflicts by source and reason", "Unresolved matches", summaryTable),
    listPanel("reconciliation", items)));
}

async function resolveConflict(conflict) {
  const form = h("div", { class: "stack" },
    h("div", { class: "small secondary" },
      `Source ${conflict.source_system} · reference ${conflict.source_ref} · ${conflict.reason}`),
    h("div", { class: "field" }, h("label", {}, "Resolve by asset ID (optional)"),
      h("input", { type: "text", id: "resolve-asset", placeholder: "UUID" })),
    h("div", { class: "field" }, h("label", {}, "Resolution note"),
      h("input", { type: "text", id: "resolve-note", value: "Reviewed and confirmed" })));
  const close = modal("Resolve reconciliation conflict", form, [
    h("button", {
      class: "btn btn-primary",
      onclick: async () => {
        const assetId = document.getElementById("resolve-asset").value.trim() || null;
        try {
          await api(`inventory/reconciliation/${conflict.id}/resolve`, {
            method: "POST",
            body: {
              asset_id: assetId,
              resolution: document.getElementById("resolve-note").value || "Resolved",
            },
          });
          close();
          renderRoute();
        } catch (error) {
          alert("error", escapeMessage(error));
        }
      },
    }, "Resolve"),
  ]);
  function escapeMessage(error) {
    const box = document.getElementById("modal-root");
    box.querySelector(".alert")?.remove();
    form.prepend(alert("error", error.message));
  }
}

/* ------------------------------------------------------------------ reports */
async function renderReports() {
  mount(view, spinnerRows(3));
  const [reports, snapshots] = await Promise.all([
    api("reporting/reports?limit=40"),
    api("compliance/snapshots?limit=24"),
  ]);
  const monthInput = h("input", { type: "month", id: "report-month", value: toMonthInput() });
  const typeSelect = h("select", { id: "report-type" },
    h("option", { value: "monthly_management" }, "Monthly management report"),
    h("option", { value: "quarterly_compliance" }, "Quarterly compliance report"));
  const snapshotSelect = h("select", { id: "report-snapshot" },
    h("option", { value: "" }, "Latest snapshot covering the period"),
    ...snapshots.map((snap) => h("option", { value: snap.id },
      `${snap.scope} · ${snap.period_start} → ${snap.period_end} · ${fmtPct(snap.compliance_pct)}`)));
  const requestCard = can("reports:generate") ? card("Generate a report",
    "PDF is produced server-side and archived; download does not depend on email",
    h("div", { class: "row", style: { alignItems: "flex-end" } },
      h("div", { class: "field" }, h("label", {}, "Report type"), typeSelect),
      h("div", { class: "field" }, h("label", {}, "Reporting month"), monthInput),
      h("div", { class: "field", style: { minWidth: "320px" } },
        h("label", {}, "Snapshot"), snapshotSelect),
      h("button", {
        class: "btn btn-primary", id: "generate-report",
        onclick: async (event) => {
          event.target.disabled = true;
          event.target.textContent = "Queuing…";
          try {
            await api("reporting/reports", {
              method: "POST",
              body: {
                report_type: typeSelect.value,
                reporting_month: monthInput.value || null,
                snapshot_id: snapshotSelect.value || null,
              },
            });
            await renderReports();
          } catch (error) {
            event.target.disabled = false;
            event.target.textContent = "Generate report";
            alert("error", error.message);
          }
        },
      }, "Generate report"))) : null;

  const history = listPanel("reports", {
    title: "Report history",
    subtitle: `${fmtNumber(reports.total)} reports retained`,
    exportable: false,
    columns: [
      { label: "Requested", render: (r) => fmtDate(r.requested_at, true) },
      { label: "Type", render: (r) => r.report_type.replaceAll("_", " ") },
      { label: "Period", render: (r) => `${r.period_start} → ${r.period_end}` },
      { label: "Status", render: (r) => pill(r.status, r.status) },
      { label: "Size", numeric: true,
        render: (r) => (r.file_size ? `${(r.file_size / 1024).toFixed(0)} KB` : "—") },
      { label: "SHA-256", render: (r) => (r.sha256
        ? h("span", { class: "mono small", title: r.sha256 }, `${r.sha256.slice(0, 12)}…`)
        : "—") },
      { label: "Delivery", render: (r) => (r.deliveries?.length
        ? pill(r.deliveries[0].status, r.deliveries[0].status)
        : h("span", { class: "muted" }, "—")) },
      { label: "Requested by", key: "requested_by" },
      {
        label: "", export: false,
        render: (r) => (r.status === "completed" && r.download_available
          ? h("button", {
            class: "btn btn-sm",
            onclick: () => download(`reporting/reports/${r.id}/download`, r.filename),
          }, "Download")
          : r.status === "failed"
            ? h("button", {
              class: "btn btn-sm",
              onclick: async () => {
                await api(`reporting/reports/${r.id}/retry`, { method: "POST" });
                renderReports();
              },
            }, "Retry")
            : "—"),
      },
    ],
    rows: reports.items,
    searchPlaceholder: "Type, status, requester…",
    facets: [
      { field: "report_type", label: "Type",
        format: (value) => value.replaceAll("_", " ") },
      { field: "status", label: "Status" },
      { field: "requested_by", label: "Requested by" },
    ],
    sorts: [
      { field: "requested_at", label: "Requested", dir: "desc" },
      { field: "period_start", label: "Period start", dir: "desc" },
      { field: "status", label: "Status" },
      { field: "file_size", label: "Size", numeric: true, dir: "desc" },
      { field: "requested_by", label: "Requested by" },
    ],
    empty: "No reports match this selection.",
    // Generation runs in the background. While anything is queued or
    // generating, refresh just this table so the status changes on its own.
    live: reports.items.some((r) => r.status === "queued" || r.status === "generating")
      ? {
        interval: 2500,
        fetch: async () => (await api("reporting/reports?limit=40")).items,
        done: (rows) => !rows.some(
          (r) => r.status === "queued" || r.status === "generating"),
      } : null,
    selection: can("reports:delete") ? {
      label: (r) => `${r.report_type.replaceAll("_", " ")} ${r.period_start}`,
      selectable: (r) => r.status !== "queued" && r.status !== "generating",
      onDelete: async (rows) => {
        const noun = rows.length === 1 ? "report" : "reports";
        if (!window.confirm(
          `Delete ${rows.length} ${noun} permanently, including the archived `
          + "PDF? This cannot be undone.")) return false;
        const result = await api("reporting/reports/bulk-delete", {
          method: "POST", body: { ids: rows.map((row) => row.id) },
        });
        if (result.refused?.length) {
          window.alert(`${result.refused.length} report(s) could not be deleted: `
            + result.refused.map((item) => item.reason).join("; "));
        }
        await renderReports();
        return true;
      },
    } : null,
  });

  mount(view, h("div", { class: "stack" }, requestCard, history));
}

/* ------------------------------------------------------------------ schedules */
async function renderSchedules() {
  mount(view, spinnerRows(3));
  const data = await api("reporting/schedules");
  const jobs = await api("reporting/scheduler/jobs");
  const manage = can("schedules:manage");
  const configureJobs = can("settings:manage");
  const rows = listPanel("schedules", {
    title: "Scheduled reports",
    subtitle: "Persisted in PostgreSQL; survive restarts",
    columns: [
      { label: "Name", key: "name" },
      { label: "Report", render: (r) => r.report_type.replaceAll("_", " ") },
      { label: "Cadence", key: "cadence" },
      { label: "Runs", render: (r) => `${r.day_of_month} at ${String(r.run_hour).padStart(2, "0")}:00 ${r.timezone}` },
      { label: "Recipients", render: (r) => (r.recipients?.length
        ? r.recipients.join(", ") : h("span", { class: "muted" }, "none")) },
      { label: "Enabled", render: (r) => pill(r.enabled ? "success" : "unknown", r.enabled ? "enabled" : "disabled") },
      { label: "Next run", render: (r) => fmtDate(r.next_run_at, true) },
      { label: "Last result", render: (r) => (r.last_run_status ? pill(r.last_run_status, r.last_run_status) : "—") },
      { label: "", export: false, render: (r) => (manage ? h("div", { class: "row" },
        h("button", {
          class: "btn btn-sm",
          onclick: async () => {
            await api(`reporting/schedules/${r.id}`, {
              method: "PATCH", body: { enabled: !r.enabled },
            });
            renderSchedules();
          },
        }, r.enabled ? "Disable" : "Enable"),
        h("button", {
          class: "btn btn-sm btn-danger",
          onclick: async () => {
            if (!window.confirm(`Delete schedule “${r.name}”?`)) return;
            await api(`reporting/schedules/${r.id}`, { method: "DELETE" });
            renderSchedules();
          },
        }, "Delete")) : "—") },
    ],
    rows: data,
    searchPlaceholder: "Name, report, recipient…",
    facets: [
      { field: "cadence", label: "Cadence" },
      { field: "report_type", label: "Report",
        format: (value) => value.replaceAll("_", " ") },
      { field: "enabled", label: "Enabled", value: (r) => String(Boolean(r.enabled)),
        format: (value) => (value === "true" ? "Enabled" : "Disabled") },
    ],
    sorts: [
      { field: "name", label: "Name" },
      { field: "next_run_at", label: "Next run", dir: "asc" },
      { field: "cadence", label: "Cadence" },
      { field: "enabled", label: "Enabled", rank: ["true", "false"] },
      { field: "last_run_status", label: "Last result" },
    ],
    empty: "No schedules match this selection.",
  });

  const form = h("div", { class: "row", style: { alignItems: "flex-end" } },
    h("div", { class: "field" }, h("label", {}, "Name"),
      h("input", { id: "schedule-name", type: "text", placeholder: "Monthly compliance" })),
    h("div", { class: "field" }, h("label", {}, "Report"),
      h("select", { id: "schedule-report" },
        h("option", { value: "monthly_management" }, "Monthly management"),
        h("option", { value: "quarterly_compliance" }, "Quarterly compliance"))),
    h("div", { class: "field" }, h("label", {}, "Cadence"),
      h("select", { id: "schedule-cadence" },
        h("option", { value: "monthly" }, "Monthly"),
        h("option", { value: "quarterly" }, "Quarterly"))),
    h("div", { class: "field" }, h("label", {}, "Day"),
      h("input", { id: "schedule-day", type: "number", min: "1", max: "28", value: "1" })),
    h("div", { class: "field" }, h("label", {}, "Hour (UTC)"),
      h("input", { id: "schedule-hour", type: "number", min: "0", max: "23", value: "6" })),
    h("div", { class: "field" }, h("label", {}, "Recipients"),
      h("input", { id: "schedule-recipients", type: "text", placeholder: "a@example.com, b@example.com" })),
    h("button", {
      class: "btn btn-primary",
      onclick: async () => {
        try {
          await api("reporting/schedules", {
            method: "POST",
            body: {
              name: document.getElementById("schedule-name").value,
              report_type: document.getElementById("schedule-report").value,
              cadence: document.getElementById("schedule-cadence").value,
              day_of_month: Number(document.getElementById("schedule-day").value),
              run_hour: Number(document.getElementById("schedule-hour").value),
              recipients: document.getElementById("schedule-recipients").value
                .split(",").map((s) => s.trim()).filter(Boolean),
              timezone: "UTC",
            },
          });
          renderSchedules();
        } catch (error) {
          alert("error", error.message);
        }
      },
    }, "Add schedule"));

  const jobRows = jobs.map((job) => {
    const interval = h("input", {
      type: "number", min: "10", value: String(job.interval_seconds),
      style: { width: "110px" }, disabled: !configureJobs,
      "aria-label": `${job.label} interval in seconds`,
    });
    const enabled = h("input", {
      type: "checkbox", checked: Boolean(job.enabled), disabled: !configureJobs,
      "aria-label": `${job.label} enabled`,
    });
    const save = h("button", {
      class: "btn btn-sm", disabled: !configureJobs,
      onclick: async () => {
        const seconds = Number(interval.value);
        if (!Number.isInteger(seconds) || seconds < 10) {
          window.alert("The interval must be at least 10 seconds.");
          return;
        }
        save.disabled = true;
        save.textContent = "Saving…";
        try {
          await api(`reporting/scheduler/jobs/${encodeURIComponent(job.name)}`, {
            method: "PATCH",
            body: { interval_seconds: seconds, enabled: enabled.checked },
          });
          await renderSchedules();
        } catch (error) {
          save.disabled = false;
          save.textContent = "Save";
          window.alert(error.message);
        }
      },
    }, "Save");
    return {
      ...job,
      intervalControl: h("div", { class: "row" },
        interval,
        h("span", { class: "muted small" }, fmtInterval(job.interval_seconds))),
      enabledControl: enabled,
      saveControl: save,
    };
  });

  const jobsTable = table([
    { label: "Job", render: (r) => h("div", {},
      h("div", {}, r.label),
      h("div", { class: "muted small" }, r.description)) },
    { label: "Interval", export: false, render: (r) => r.intervalControl },
    { label: "Enabled", export: false, render: (r) => r.enabledControl },
    { label: "Next run", render: (r) => fmtDate(r.next_run_at, true) },
    { label: "Last result", render: (r) => (r.last_status
      ? h("span", { title: r.last_message || "" }, pill(r.last_status, r.last_status))
      : h("span", { class: "muted" }, "never run")) },
    { label: "", export: false, render: (r) => r.saveControl },
  ], jobRows, { compact: true, export: false, empty: "No scheduled jobs." });

  mount(view, h("div", { class: "stack" },
    manage ? card("New schedule", "Runs are idempotent per period", form) : null,
    rows,
    card("System jobs",
      configureJobs
        ? "Collection, snapshots, retention and report ticks — saved changes apply on the next poll"
        : "Collection, snapshots, retention and report ticks — administrators can change these",
      jobsTable)));
}

/* ------------------------------------------------------------------ backups */
async function renderBackups() {
  mount(view, spinnerRows(3));
  const [data, settings, jobs] = await Promise.all([
    api("reporting/backups"),
    api("reporting/settings/backup"),
    api("reporting/scheduler/jobs"),
  ]);
  const job = jobs.find((entry) => entry.name === "reporting.backup") || null;
  const worker = settings.worker || data.worker || {};
  const active = data.active;
  const completed = data.items.filter((row) => row.status === "completed");
  const latest = completed.slice().sort((a, b) =>
    String(b.finished_at || "").localeCompare(String(a.finished_at || "")))[0] || null;
  const totalSize = completed.reduce(
    (sum, row) => sum + (Number(row.size_bytes) || 0), 0);

  const flash = h("div", { class: "stack" });
  if (!worker.available) {
    flash.appendChild(alert("warn",
      "The backup worker is not reporting a heartbeat. A requested backup "
      + "stays queued until it runs. Check that the backup container is up "
      + "(`docker compose ps backup` and its logs)."));
  }
  if (job && !job.enabled) {
    flash.appendChild(alert("warn",
      "Scheduled backups are disabled. Existing archives remain downloadable; "
      + "enable the job at Reporting → Schedules → System jobs."));
  }
  if (settings.pending_purge > 0) {
    flash.appendChild(alert("warn",
      `${settings.pending_purge} archive(s) are beyond the retention count and `
      + "will be removed after the next successful backup."));
  }

  const create = h("button", {
    class: "btn btn-primary",
    disabled: Boolean(active),
  }, active ? `Backup ${active.status}…` : "Create backup now");
  create.addEventListener("click", async () => {
    create.disabled = true;
    flash.replaceChildren();
    try {
      await api("reporting/backups", { method: "POST" });
      await renderBackups();
    } catch (error) {
      create.disabled = false;
      flash.replaceChildren(alert("error", error.message));
    }
  });

  const createCard = card("Create a backup",
    "One archive of the database and the report volume; no service is stopped",
    h("div", { class: "stack" },
      h("div", { class: "row", style: { alignItems: "center" } },
        create,
        h("span", { class: "muted small" },
          active
            ? `${active.trigger_kind} backup requested by `
              + `${active.requested_by || "unknown"} at `
              + `${fmtDate(active.requested_at, true)}`
            : "On-demand requests are queued and processed in the background.")),
      h("div", { class: "small secondary" },
        "A backup normally takes seconds to minutes depending on database and "
        + "report size. It runs alongside the services and does not interrupt "
        + "collection or report delivery.")));

  const tiles = h("div", { class: "grid grid-4" },
    statTile({
      label: "Archives kept", value: String(settings.completed),
      sub: `Retention: ${settings.retention} completed archive(s)`,
      accent: "#5794f2",
    }),
    statTile({
      label: "Archive storage", value: fmtBytes(totalSize),
      sub: "Compressed archives on the backups volume",
      accent: "#73bf69",
    }),
    statTile({
      label: "Last backup",
      value: latest ? fmtRelative(latest.finished_at) : "Never",
      sub: latest ? fmtDate(latest.finished_at, true) : "No completed archive yet",
      accent: "#b877d9",
    }),
    statTile({
      label: "Backup worker",
      value: worker.available ? "Online" : "Not running",
      sub: worker.last_seen_at
        ? `Last seen ${fmtRelative(worker.last_seen_at)}`
        : "No heartbeat recorded",
      accent: worker.available ? "#73bf69" : "#d03b3b",
    }));

  const retentionInput = h("input", {
    id: "backup-retention",
    type: "number",
    min: String(settings.min_retention),
    max: String(settings.max_retention),
    value: String(settings.retention),
    style: { width: "110px" },
    "aria-label": "Completed archives to keep",
  });
  const retentionNote = h("div");
  const retentionSave = h("button", { class: "btn btn-primary" }, "Save");
  retentionSave.addEventListener("click", async () => {
    const count = Number(retentionInput.value);
    if (!Number.isInteger(count)
        || count < settings.min_retention || count > settings.max_retention) {
      retentionNote.replaceChildren(alert("error",
        `Choose a whole number between ${settings.min_retention} and `
        + `${settings.max_retention}.`));
      return;
    }
    retentionSave.disabled = true;
    retentionSave.textContent = "Saving…";
    try {
      const saved = await api("reporting/settings/backup", {
        method: "PUT", body: { count },
      });
      retentionInput.value = String(saved.retention);
      retentionNote.replaceChildren(alert("ok",
        saved.pending_purge > 0
          ? `Saved. The worker will remove ${saved.pending_purge} older `
            + "archive(s) after the next successful backup."
          : "Saved. The worker applies this count after every successful backup."));
    } catch (error) {
      retentionNote.replaceChildren(alert("error", error.message));
    } finally {
      retentionSave.disabled = false;
      retentionSave.textContent = "Save";
    }
  });
  const retentionCard = card("Archive retention",
    "How many completed archives stay downloadable",
    h("div", { class: "stack" },
      h("div", { class: "row", style: { alignItems: "flex-end" } },
        h("div", { class: "field" },
          h("label", { for: "backup-retention" }, "Completed archives to keep"),
          retentionInput),
        retentionSave),
      retentionNote,
      h("div", { class: "small secondary" },
        `Each archive contains the whole database and report volume, so size `
        + `the backups volume for about ${settings.retention} × their combined `
        + "size. Lowering the count does not delete anything until the next "
        + "successful backup.")));

  const scheduleBody = job
    ? h("div", { class: "stack" },
      h("dl", { class: "kv" },
        h("dt", {}, "Cadence"),
        h("dd", {}, `${fmtInterval(job.interval_seconds)}`
          + (job.enabled ? "" : " (disabled)")),
        h("dt", {}, "Next run"), h("dd", {}, fmtDate(job.next_run_at, true)),
        h("dt", {}, "Last result"),
        h("dd", {}, job.last_status
          ? h("span", { title: job.last_message || "" },
            pill(job.last_status, job.last_status))
          : h("span", { class: "muted" }, "never run"))),
      h("button", {
        class: "btn btn-sm",
        onclick: () => navigate("schedules"),
      }, "Configure schedule"))
    : h("p", { class: "muted" },
      "The backup job is not registered in this release.");
  const scheduleCard = card("Scheduled backups",
    "The same queue as the on-demand button", scheduleBody);

  const archiveTable = table([
    { label: "Requested", render: (r) => h("div", {},
      h("div", {}, fmtDate(r.requested_at, true)),
      h("div", { class: "muted small" },
        `${r.trigger_kind}${r.requested_by ? ` · ${r.requested_by}` : ""}`)) },
    { label: "Status", render: (r) => h("div", {},
      r.status === "queued" ? pill("pending", "queued")
        : r.status === "running" ? pill("partial", "running")
          : pill(r.status, r.status),
      r.message
        ? h("div", { class: "muted small", title: r.message },
          r.message.length > 90 ? `${r.message.slice(0, 90)}…` : r.message)
        : null) },
    { label: "Size", numeric: true,
      render: (r) => (r.size_bytes ? fmtBytes(r.size_bytes) : "—") },
    { label: "Reports", numeric: true,
      render: (r) => (r.report_files == null ? "—" : String(r.report_files)) },
    { label: "Version", render: (r) => (r.app_version
      ? h("div", { class: "small" }, r.app_version,
        h("div", { class: "muted" }, `schema ${r.schema_version || "?"}`))
      : "—") },
    { label: "SHA-256", render: (r) => (r.sha256
      ? h("span", { class: "mono small", title: r.sha256 },
        `${r.sha256.slice(0, 12)}…`)
      : "—") },
    { label: "", export: false, render: (r) => (r.download_available
      ? h("button", {
        class: "btn btn-sm",
        onclick: () => download(
          `reporting/backups/${r.id}/download`, r.filename,
        ).catch((error) => window.alert(error.message)),
      }, "Download")
      : h("span", { class: "muted" },
        r.status === "completed" ? "file unavailable" : "—")) },
  ], data.items, {
    compact: true,
    export: false,
    empty: "No backups have been created yet.",
  });
  const archiveCard = card("Archives",
    "Newest first; downloads are authenticated and audited", archiveTable);

  const restoreCard = card("Restore and migration",
    "Archives are portable only to the same PatchTrack release",
    h("div", { class: "stack" },
      h("p", { class: "small secondary" },
        "Each archive is one .tar.gz containing database.dump (pg_dump custom "
        + "format), reports.tar.gz, manifest.json, SHA256SUMS and RESTORE.txt. "
        + "Verify it with sha256sum -c SHA256SUMS before restoring."),
      h("p", { class: "small secondary" },
        "Secrets are excluded: no .env, JWT signing secret, service token, TLS "
        + "private key or database password. The database carries password "
        + "hashes (users keep their passwords) and the SMTP password encrypted "
        + "with the previous installation's key; re-enter the SMTP password "
        + "after restoring."),
      h("p", { class: "small secondary" },
        "For a new host, install the same PatchTrack release first, then follow "
        + "the restore steps in docs/backup-restore.md. Compare the application "
        + "and schema versions in the manifest before restoring.")));

  mount(view, h("div", { class: "stack" },
    flash,
    tiles,
    createCard,
    h("div", { class: "grid grid-2-1" }, retentionCard, scheduleCard),
    archiveCard,
    restoreCard));

  const busy = active
    || data.items.some((row) => row.status === "queued" || row.status === "running");
  if (busy) scheduleBackupRefresh();
}

/* ------------------------------------------------------------------ integrations */
async function renderIntegrations() {
  mount(view, spinnerRows(3));
  const data = await api("inventory/integrations");
  const runs = await api("inventory/sync-runs?limit=20");
  const manage = can("integrations:manage");
  const cards = data.map((integration) => {
    const last = integration.last_run || {};
    const testResult = h("div");
    const body = h("div", { class: "stack" },
      h("dl", { class: "kv" },
        h("dt", {}, "Status"), h("dd", {}, pill(integration.health, integration.health.replaceAll("_", " "))),
        h("dt", {}, "Endpoint"), h("dd", { class: "mono small" }, integration.endpoint || "not configured"),
        h("dt", {}, "Authentication"), h("dd", {}, `${integration.auth_method} · ${integration.secret_configured ? "secret configured" : "no secret"}`),
        h("dt", {}, "Last attempt"), h("dd", {}, last.started_at ? fmtDate(last.started_at, true) : "never"),
        h("dt", {}, "Last success"), h("dd", {}, fmtDate(integration.last_success_at, true)),
        h("dt", {}, "Records"), h("dd", {}, `${fmtNumber(last.records_seen)} seen · ${fmtNumber(last.records_updated)} matched · ${fmtNumber(last.records_rejected)} rejected`),
        h("dt", {}, "Duration"), h("dd", {}, last.duration_s ? `${Number(last.duration_s).toFixed(1)} s` : "—"),
        h("dt", {}, "Error"), h("dd", { class: "small" }, last.error_summary || "—")),
      h("div", { class: "row" },
        can("sync:run") ? h("button", {
          class: "btn btn-sm",
          onclick: async (event) => {
            event.target.disabled = true;
            event.target.textContent = "Running…";
            try {
              await api("inventory/sync", {
                method: "POST", body: { integration: integration.key },
              });
              renderIntegrations();
            } catch (error) {
              event.target.disabled = false;
              event.target.textContent = "Run now";
              alert("error", error.message);
            }
          },
        }, "Run now") : null,
        manage ? h("button", {
          class: "btn btn-sm",
          onclick: () => editIntegration(integration),
        }, "Configure") : null,
        manage ? h("button", {
          class: "btn btn-sm",
          onclick: async (event) => {
            const button = event.currentTarget;
            button.disabled = true;
            button.textContent = "Testing…";
            testResult.replaceChildren();
            try {
              const result = await api(
                `inventory/integrations/${integration.key}/test`,
                { method: "POST", body: {} },
              );
              testResult.appendChild(alert(
                result.status === "success" ? "ok" : "error",
                `${result.detail} (${result.duration_ms} ms)`));
            } catch (error) {
              testResult.appendChild(alert("error", error.message));
            } finally {
              button.disabled = false;
              button.textContent = "Test configuration";
            }
          },
        }, "Test configuration") : null),
      testResult);
    return card(integration.display_name || integration.key,
      `${integration.kind} · every ${integration.sync_interval_minutes} min`, body);
  });

  mount(view, h("div", { class: "stack" },
    alert("warn", "Integrations are only marked healthy after a successful collection. " +
      "A failure never deletes inventory and never classifies servers as compliant."),
    h("div", { class: "grid grid-2" }, ...cards),
    listPanel("sync-runs", {
      title: "Recent collection runs",
      subtitle: "Newest first",
      columns: [
        { label: "Started", render: (r) => fmtDate(r.started_at, true) },
        { label: "Integration", key: "integration_key" },
        { label: "Trigger", key: "trigger" },
        { label: "Status", render: (r) => pill(r.status, r.status) },
        { label: "Seen", numeric: true, key: "records_seen" },
        { label: "Created", numeric: true, key: "records_created" },
        { label: "Updated", numeric: true, key: "records_updated" },
        { label: "Rejected", numeric: true, key: "records_rejected" },
        { label: "Unmatched", numeric: true, key: "assets_unmatched" },
        { label: "Error", render: (r) => h("span", { class: "small muted" }, r.error_summary || "—") },
      ],
      rows: runs,
      searchPlaceholder: "Integration, trigger, error…",
      facets: [
        { field: "integration_key", label: "Integration" },
        { field: "status", label: "Status" },
        { field: "trigger", label: "Trigger" },
      ],
      sorts: [
        { field: "started_at", label: "Started", dir: "desc" },
        { field: "integration_key", label: "Integration" },
        { field: "status", label: "Status" },
        { field: "records_seen", label: "Records seen", numeric: true, dir: "desc" },
        { field: "records_rejected", label: "Rejected", numeric: true, dir: "desc" },
      ],
      empty: "No collection runs match this selection.",
    })));
}

function editIntegration(integration) {
  const form = h("div", { class: "stack" },
    h("div", { class: "field" }, h("label", {}, "Display name"),
      h("input", { id: "int-name", type: "text", value: integration.display_name || "" })),
    h("div", { class: "field" }, h("label", {}, "Endpoint"),
      h("input", { id: "int-endpoint", type: "text", value: integration.endpoint || "",
        placeholder: "https://assetexplorer.example.com" })),
    h("div", { class: "field" }, h("label", {}, "Authentication method"),
      h("select", { id: "int-auth" },
        ...["bearer", "apikey", "basic", "none"].map((method) =>
          h("option", { value: method, selected: integration.auth_method === method }, method)))),
    h("div", { class: "field" }, h("label", {}, "Credential reference (write-only)"),
      h("input", { id: "int-secret", type: "password", autocomplete: "new-password",
        placeholder: integration.secret_configured ? "•••••• configured — leave blank to keep" : "Enter secret" })),
    h("div", { class: "field" }, h("label", {}, "Sync interval (minutes)"),
      h("input", { id: "int-interval", type: "number", min: "5", max: "10080",
        value: integration.sync_interval_minutes })),
    h("div", { class: "row" },
      h("label", { class: "row", style: { gap: "6px" } },
        h("input", { id: "int-enabled", type: "checkbox", checked: integration.enabled }),
        "Enabled"),
      h("label", { class: "row", style: { gap: "6px" } },
        h("input", { id: "int-tls", type: "checkbox", checked: integration.verify_tls }),
        "Verify TLS")));
  const draftPayload = () => {
    const payload = {
      display_name: document.getElementById("int-name").value,
      endpoint: document.getElementById("int-endpoint").value,
      auth_method: document.getElementById("int-auth").value,
      enabled: document.getElementById("int-enabled").checked,
      verify_tls: document.getElementById("int-tls").checked,
      sync_interval_minutes: Number(document.getElementById("int-interval").value),
    };
    const secret = document.getElementById("int-secret").value;
    if (secret) payload.secret_ref = secret;
    return payload;
  };
  modal(`Configure ${integration.key}`, form, [
    h("button", {
      class: "btn",
      onclick: async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        button.textContent = "Testing…";
        try {
          // The draft is sent to the test route only; nothing is saved.
          const result = await api(
            `inventory/integrations/${integration.key}/test`,
            { method: "POST", body: draftPayload() },
          );
          form.prepend(alert(
            result.status === "success" ? "ok" : "error",
            `${result.detail} (${result.duration_ms} ms)`));
        } catch (error) {
          form.prepend(alert("error", error.message));
        } finally {
          button.disabled = false;
          button.textContent = "Test configuration";
        }
      },
    }, "Test configuration"),
    h("button", {
      class: "btn btn-primary",
      onclick: async () => {
        try {
          await api(`inventory/integrations/${integration.key}`, {
            method: "PATCH", body: draftPayload(),
          });
          document.getElementById("modal-root").innerHTML = "";
          renderIntegrations();
        } catch (error) {
          form.prepend(alert("error", error.message));
        }
      },
    }, "Save configuration"),
  ]);
}

/* ------------------------------------------------------------------ policies */
async function renderPolicies() {
  mount(view, spinnerRows(3));
  const [policies, active] = await Promise.all([
    api("compliance/policies"),
    api("compliance/policies/active"),
  ]);
  const current = active.policy;
  const summaryCard = card("Active policy",
    active.version_id ? `Version ${active.version_id.slice(0, 8)}` : "Built-in baseline (no policy activated)",
    h("dl", { class: "kv" },
      h("dt", {}, "Name"), h("dd", {}, current.name || "Built-in baseline"),
      h("dt", {}, "Overall target"), h("dd", {}, fmtPct(current.target_pct)),
      h("dt", {}, "Quarterly target"), h("dd", {}, fmtPct(current.quarterly_target_pct)),
      h("dt", {}, "Freshness threshold"), h("dd", {}, `${current.freshness_hours} hours`),
      h("dt", {}, "Blocking severities"),
      h("dd", {}, (current.default_rule?.blocking_severities || []).join(", ")),
      h("dt", {}, "Remediation windows"), h("dd", {},
        Object.entries(current.default_rule?.remediation_days || {})
          .map(([severity, days]) => `${severity}: ${days}d`).join(" · ")),
      h("dt", {}, "Excluded environments"), h("dd", {},
        (current.excluded_environments || []).join(", ") || "none")));

  const rows = listPanel("policies", {
    title: "Policy register",
    subtitle: "All versions retained",
    columns: [
      { label: "Policy", key: "name" },
      { label: "Version", render: (r) => (r.active_version ?? "—") },
      { label: "Target", render: (r) => fmtPct(r.definition?.target_pct) },
      { label: "Active", render: (r) => (r.is_active ? pill("success", "active") : "—") },
      { label: "Effective", render: (r) => fmtDate(r.effective_from, true) },
      { label: "", export: false, render: (r) => (can("policies:manage") && !r.is_active
        ? h("button", {
          class: "btn btn-sm",
          onclick: async () => {
            await api(`compliance/policies/${r.id}/activate`, { method: "POST" });
            renderPolicies();
          },
        }, "Activate") : "—") },
    ],
    rows: policies,
    searchPlaceholder: "Policy name, note…",
    facets: [
      { field: "is_active", label: "Active", value: (r) => String(Boolean(r.is_active)),
        format: (value) => (value === "true" ? "Active" : "Not active") },
    ],
    sorts: [
      { field: "effective_from", label: "Effective", dir: "desc" },
      { field: "name", label: "Policy" },
      { field: "active_version", label: "Version", numeric: true, dir: "desc" },
      { field: "is_active", label: "Active", rank: ["true", "false"] },
    ],
    empty: policies.length
      ? "No policies match this selection."
      : "No policies created. The built-in baseline is in use.",
  });

  const form = can("policies:manage") ? card("New policy version",
    "Policy versions are immutable; activating one re-evaluates every asset",
    h("div", { class: "row", style: { alignItems: "flex-end", flexWrap: "wrap" } },
      h("div", { class: "field" }, h("label", {}, "Name"),
        h("input", { id: "pol-name", type: "text", placeholder: "Q4 2026 security baseline" })),
      h("div", { class: "field" }, h("label", {}, "Target %"),
        h("input", { id: "pol-target", type: "number", min: "0", max: "100", value: "95" })),
      h("div", { class: "field" }, h("label", {}, "Quarterly target %"),
        h("input", { id: "pol-qtarget", type: "number", min: "0", max: "100", value: "95" })),
      h("div", { class: "field" }, h("label", {}, "Freshness (hours)"),
        h("input", { id: "pol-fresh", type: "number", min: "1", value: "48" })),
      h("button", {
        class: "btn btn-primary",
        onclick: async () => {
          try {
            await api("compliance/policies", {
              method: "POST",
              body: {
                name: document.getElementById("pol-name").value,
                target_pct: Number(document.getElementById("pol-target").value),
                quarterly_target_pct: Number(document.getElementById("pol-qtarget").value),
                freshness_hours: Number(document.getElementById("pol-fresh").value),
                change_note: "Created from console",
              },
            });
            renderPolicies();
          } catch (error) {
            alert("error", error.message);
          }
        },
      }, "Create policy"))) : null;

  const reasoning = alert("ok",
    "Unknown and stale assets remain in the denominator. A server is only compliant " +
    "when a fresh assessment shows no blocking findings on its scope rule.");
  mount(view, h("div", { class: "stack" },
    reasoning, summaryCard, form, rows));
}

/* ------------------------------------------------------------------ users */
async function renderUsers() {
  mount(view, spinnerRows(3));
  const [users, roles] = await Promise.all([api("identity/users"), api("identity/roles")]);
  const roleNames = roles.map((r) => r.role);
  const rows = listPanel("users", {
    title: "Users",
    subtitle: `${users.length} accounts`,
    columns: [
    { label: "Username", render: (r) => h("span", { class: "mono" }, r.username) },
    { label: "Display name", key: "display_name" },
    { label: "Role", render: (r) => h("span", { class: "badge-role" }, r.role.replaceAll("_", " ")) },
    { label: "Status", render: (r) => pill(r.is_active ? "success" : "unknown", r.is_active ? "active" : "disabled") },
    { label: "Last sign-in", render: (r) => fmtDate(r.last_login_at, true) },
    { label: "", export: false, render: (r) => h("div", { class: "row" },
      h("button", { class: "btn btn-sm", onclick: () => editUser(r, roleNames) }, "Edit"),
      h("button", {
        class: "btn btn-sm",
        onclick: () => modal("Reset password",
          h("div", { class: "stack" },
            h("div", { class: "small secondary" }, `Set a new password for ${r.username}.`),
            h("div", { class: "field" }, h("label", {}, "New password (min 12 chars)"),
              h("input", { id: "reset-pw", type: "password", autocomplete: "new-password" }))),
          [h("button", {
            class: "btn btn-primary",
            onclick: async () => {
              try {
                await api(`identity/users/${r.id}/password`, {
                  method: "POST",
                  body: { password: document.getElementById("reset-pw").value },
                });
                document.getElementById("modal-root").innerHTML = "";
              } catch (error) {
                alert("error", error.message);
              }
            },
          }, "Set password")]),
      }, "Reset")) },
    ],
    rows: users,
    searchPlaceholder: "Username, display name, role…",
    facets: [
      { field: "role", label: "Role", format: (value) => value.replaceAll("_", " ") },
      { field: "is_active", label: "Status", value: (r) => String(Boolean(r.is_active)),
        format: (value) => (value === "true" ? "Active" : "Disabled") },
    ],
    sorts: [
      { field: "username", label: "Username" },
      { field: "display_name", label: "Display name" },
      { field: "role", label: "Role" },
      { field: "last_login_at", label: "Last sign-in", dir: "desc" },
      { field: "is_active", label: "Status", rank: ["true", "false"] },
    ],
    empty: "No accounts match this selection.",
  });

  const createForm = h("div", { class: "row", style: { alignItems: "flex-end" } },
    h("div", { class: "field" }, h("label", {}, "Username"),
      h("input", { id: "user-name", type: "text" })),
    h("div", { class: "field" }, h("label", {}, "Display name"),
      h("input", { id: "user-display", type: "text" })),
    h("div", { class: "field" }, h("label", {}, "Role"),
      h("select", { id: "user-role" },
        ...roleNames.map((role) => h("option", { value: role }, role.replaceAll("_", " "))))),
    h("div", { class: "field" }, h("label", {}, "Temporary password"),
      h("input", { id: "user-pw", type: "password", autocomplete: "new-password" })),
    h("button", {
      class: "btn btn-primary",
      onclick: async () => {
        try {
          await api("identity/users", {
            method: "POST",
            body: {
              username: document.getElementById("user-name").value,
              display_name: document.getElementById("user-display").value,
              role: document.getElementById("user-role").value,
              password: document.getElementById("user-pw").value,
            },
          });
          renderUsers();
        } catch (error) {
          alert("error", error.message);
        }
      },
    }, "Create user"));

  mount(view, h("div", { class: "stack" },
    card("Create user", "New accounts must change their password at first sign-in", createForm),
    rows));
}

function editUser(user, roles) {
  const form = h("div", { class: "stack" },
    h("div", { class: "field" }, h("label", {}, "Display name"),
      h("input", { id: "edit-display", type: "text", value: user.display_name || "" })),
    h("div", { class: "field" }, h("label", {}, "Role"),
      h("select", { id: "edit-role" },
        ...roles.map((role) => h("option", {
          value: role, selected: role === user.role,
        }, role.replaceAll("_", " "))))),
    h("label", { class: "row", style: { gap: "6px" } },
      h("input", { id: "edit-active", type: "checkbox", checked: user.is_active }),
      "Account active"));
  modal(`Edit ${user.username}`, form, [
    h("button", {
      class: "btn btn-primary",
      onclick: async () => {
        try {
          await api(`identity/users/${user.id}`, {
            method: "PATCH",
            body: {
              display_name: document.getElementById("edit-display").value,
              role: document.getElementById("edit-role").value,
              is_active: document.getElementById("edit-active").checked,
            },
          });
          document.getElementById("modal-root").innerHTML = "";
          renderUsers();
        } catch (error) {
          form.prepend(alert("error", error.message));
        }
      },
    }, "Save"),
  ]);
}

/* ------------------------------------------------------------------ audit */
async function renderAudit() {
  mount(view, spinnerRows(3));
  const data = await api("identity/audit?limit=200");
  mount(view, h("div", { class: "stack" },
    alert("ok", "Audit events are append-only. Passwords, tokens and secrets are never recorded."),
    listPanel("audit", {
      title: "Audit trail",
      subtitle: `${fmtNumber(data.total)} events retained`,
      columns: [
        { label: "Time", render: (r) => fmtDate(r.occurred_at, true) },
        { label: "Actor", key: "actor" },
        { label: "Role", key: "actor_role" },
        { label: "Event", key: "event_type", mono: true },
        { label: "Target", render: (r) => (r.target_type ? `${r.target_type} ${r.target_id}` : "—") },
        { label: "Outcome", render: (r) => pill(r.outcome, r.outcome) },
        { label: "Source", key: "source_ip", mono: true },
      ],
      rows: data.items,
      searchPlaceholder: "Actor, event, target, address…",
      facets: [
        { field: "event_type", label: "Event" },
        { field: "outcome", label: "Outcome" },
        { field: "actor_role", label: "Role", format: (value) => value.replaceAll("_", " ") },
      ],
      sorts: [
        { field: "occurred_at", label: "Time", dir: "desc" },
        { field: "actor", label: "Actor" },
        { field: "event_type", label: "Event" },
        { field: "outcome", label: "Outcome" },
      ],
      empty: "No audit events match this selection.",
    })));
}

/* ------------------------------------------------------------------ health */
// Remember the selected Operational health tab for the session.
const HEALTH_STATE = { key: "services" };

async function renderHealth() {
  mount(view, spinnerRows(3));
  const gateway = await fetch("/readyz").then((r) => r.json()).catch(() => ({}));
  const [summary, runs, integrations] = await Promise.all([
    api("compliance/summary").catch(() => null),
    api("inventory/sync-runs?limit=30").catch(() => []),
    api("inventory/integrations").catch(() => []),
  ]);
  const serviceRows = Object.entries(gateway.services || {}).map(([name, status]) => ({
    name, status,
  }));
  const panels = [
    ["services", "Services", card("Service availability",
      "Backend health as reported through the gateway",
      table([
        { label: "Component", key: "name" },
        { label: "Status", render: (r) => pill(r.status, r.status) },
      ], [
        { name: "gateway", status: gateway.status || "unknown" },
        ...serviceRows,
      ], { compact: true, export: false }))],
    ["freshness", "Data freshness", card("Data freshness",
      "Inventory and patch collection are tracked separately",
      h("dl", { class: "kv" },
        h("dt", {}, "Inventory synchronised"),
        h("dd", {}, fmtDate(summary?.freshness?.inventory_synced_at, true)),
        h("dt", {}, "Patch data synchronised"),
        h("dd", {}, fmtDate(summary?.freshness?.patch_synced_at, true)),
        ...Object.entries(summary?.freshness?.per_source || {}).flatMap(([key, value]) => [
          h("dt", {}, `${key} last success`), h("dd", {}, fmtDate(value, true)),
        ])))],
    ["integrations", "Integrations", card("Integration health", "Per-source state", table([
      { label: "Integration", key: "key" },
      { label: "Health", render: (r) => pill(r.health, r.health.replaceAll("_", " ")) },
      { label: "Last success", render: (r) => fmtDate(r.last_success_at, true) },
      { label: "Last status", render: (r) => (r.last_run ? pill(r.last_run.status, r.last_run.status) : "—") },
    ], integrations, { compact: true, export: false }))],
    ["runs", "Collection runs", card("Recent collection runs", "Operational history", table([
      { label: "Started", render: (r) => fmtDate(r.started_at, true) },
      { label: "Integration", key: "integration_key" },
      { label: "Status", render: (r) => pill(r.status, r.status) },
      { label: "Seen", numeric: true, key: "records_seen" },
      { label: "Rejected", numeric: true, key: "records_rejected" },
      { label: "Error", render: (r) => r.error_summary || "—" },
    ], runs, { compact: true, export: false }))],
  ];
  mount(view, h("div", { class: "stack" },
    tabbedPanels(panels, {
      ariaLabel: "Operational health sections", state: HEALTH_STATE,
    })));
}

/* ------------------------------------------------------------------ settings */
/* Settings → Inventory placement values.
 *
 * The list is the vocabulary every screen displays. Renaming an entry
 * propagates to every server holding the old spelling (the source value in
 * AssetExplorer is preserved and shown beside it); a value still in use cannot
 * be deleted, only renamed. */
let SETTINGS_FLASH = null;
// Remember the selected Settings tab while the console stays open, so saving
// a value and re-rendering does not throw the operator back to the first tab.
const SETTINGS_STATE = { key: "branding" };

function renamePlacementOption(item, label) {
  const error = h("div");
  const input = h("input", { type: "text", maxLength: 128, value: item.value });
  let saving = false;
  let close = () => {};
  const save = h("button", { class: "btn btn-primary" }, "Save");
  save.onclick = async () => {
    if (saving) return;
    saving = true;
    save.disabled = true;
    error.replaceChildren();
    try {
      await api(`inventory/settings/placement/${item.id}`, {
        method: "PATCH", body: { value: input.value },
      });
      close();
      if (item.in_use) {
        // A rename can move servers between policy scope rules, exactly as an
        // override does. The rename is already stored; a failed refresh must
        // not be presented as a failed rename.
        try {
          await api("compliance/evaluate", { method: "POST" });
        } catch (error_) {
          console.warn("compliance refresh after a rename failed", error_);
        }
      }
      await renderSettings();
    } catch (error_) {
      saving = false;
      save.disabled = false;
      error.appendChild(alert("error", error_.message));
    }
  };
  close = modal(`Edit ${label} · ${item.value}`,
    h("div", { class: "stack" },
      error,
      item.in_use
        ? alert("warn", `The ${item.in_use} server`
          + `${item.in_use === 1 ? "" : "s"} using “${item.value}” will show the `
          + "new name on the next refresh. The value AssetExplorer reports is "
          + "kept and shown beside it.")
        : null,
      h("div", { class: "field" }, h("label", {}, label), input)),
    [save, h("button", { class: "btn", onclick: () => close() }, "Cancel")]);
}

async function removePlacementOption(item, label) {
  // The server refuses this too; guarding here avoids a pointless round trip
  // and explains the way out (rename instead).
  if (item.in_use) {
    SETTINGS_FLASH = alert("warn",
      `${item.in_use} server${item.in_use === 1 ? "" : "s"} still use `
      + `“${item.value}”. Rename it instead — the new name reaches those `
      + "servers without touching the value AssetExplorer reports.");
    await renderSettings();
    return;
  }
  if (!window.confirm(`Remove “${item.value}” from the ${label} list?`)) return;
  try {
    await api(`inventory/settings/placement/${item.id}`, { method: "DELETE" });
    SETTINGS_FLASH = alert("ok", "Removed from the list.");
    await renderSettings();
  } catch (error) {
    SETTINGS_FLASH = alert("error", error.message);
    await renderSettings();
  }
}

function placementOptionSection(kind, label, items) {
  const addInput = h("input", {
    type: "text", maxLength: 128, placeholder: `New ${label} value`,
  });
  const status = h("div");
  const add = async () => {
    status.replaceChildren();
    try {
      await api("inventory/settings/placement", {
        method: "POST", body: { kind, value: addInput.value },
      });
      await renderSettings();
    } catch (error) {
      status.appendChild(alert("error", error.message));
    }
  };
  addInput.onkeydown = (event) => {
    if (event.key === "Enter") { event.preventDefault(); add(); }
  };
  return h("div", { class: "stack" },
    h("h3", { style: { margin: 0, fontSize: "13.5px" } }, `Allowed ${label} values`),
    items.length
      ? table([
        { label: "Value", key: "value" },
        { label: "In use", numeric: true, render: (r) => (r.in_use
          ? `${r.in_use} server${r.in_use === 1 ? "" : "s"}` : "—") },
        { label: "", export: false, render: (r) => h("div", { class: "row" },
          h("button", {
            class: "btn btn-sm",
            onclick: () => renamePlacementOption(r, label),
          }, "Edit"),
          h("button", {
            class: "btn btn-sm",
            disabled: r.in_use ? true : null,
            title: r.in_use
              ? "In use — rename it instead, or change those servers first"
              : null,
            onclick: () => removePlacementOption(r, label),
          }, "Delete")) },
      ], items, { compact: true, export: false })
      : h("p", { class: "muted small" },
        `No ${label} values configured yet. Until one is added, the placement `
        + "editor offers free text."),
    h("div", { class: "row", style: { alignItems: "flex-end" } },
      h("div", { class: "field", style: { minWidth: "260px" } }, addInput),
      h("button", { class: "btn btn-primary", onclick: add }, "Add")),
    status);
}

async function renderSettings() {
  mount(view, spinnerRows(3));
  const orgInput = h("input", { id: "org-name", type: "text", placeholder: "Organisation name" });
  const preparerName = h("input", { id: "preparer-name", type: "text", placeholder: "Name" });
  const preparerTitle = h("input", { id: "preparer-title", type: "text", placeholder: "Designation" });
  const concurName = h("input", { id: "concur-name", type: "text", placeholder: "Name" });
  const concurTitle = h("input", { id: "concur-title", type: "text", placeholder: "Designation" });
  const logoInput = h("input", {
    id: "org-logo", type: "file", accept: "image/png,image/jpeg",
    style: { maxWidth: "300px" },
  });
  const logoPreview = h("img", {
    alt: "Organisation logo preview",
    style: {
      maxHeight: "64px", maxWidth: "220px", display: "none",
      background: "#fff", padding: "6px", borderRadius: "4px",
    },
  });
  const logoName = h("span", { class: "muted small" });
  const logoRemove = h("button", {
    class: "btn btn-sm", type: "button", style: { display: "none" },
  }, "Remove logo");
  const orgStatus = h("div");
  const retentionInput = h("input", {
    id: "audit-retention-days",
    type: "number", min: "30", max: "36500", step: "1",
  });
  const retentionStatus = h("div", { class: "muted small" });
  const reportRetentionInput = h("input", {
    id: "report-retention-days",
    type: "number", min: "30", max: "36500", step: "1",
  });
  const reportRetentionStatus = h("div", { class: "muted small" });
  const smtpHost = h("input", {
    id: "smtp-host", type: "text", placeholder: "relay.example.com",
  });
  const smtpPort = h("input", {
    id: "smtp-port", type: "number", min: "1", max: "65535", step: "1",
  });
  const smtpStarttls = h("input", { id: "smtp-starttls", type: "checkbox" });
  const smtpUsername = h("input", {
    id: "smtp-username", type: "text", autocomplete: "off",
  });
  const smtpPassword = h("input", {
    id: "smtp-password", type: "password", autocomplete: "new-password",
  });
  const smtpClear = h("input", { id: "smtp-clear-password", type: "checkbox" });
  const smtpSender = h("input", {
    id: "smtp-sender", type: "text", placeholder: "patchtrack@example.com",
  });
  const smtpTestTo = h("input", {
    id: "smtp-test-to", type: "text", placeholder: "your.address@example.com",
  });
  const smtpStatus = h("div", { class: "muted small" });
  const tlsCertFile = h("input", {
    id: "tls-cert", type: "file", accept: ".pem,.crt,.cer,application/x-pem-file",
  });
  const tlsKeyFile = h("input", {
    id: "tls-key", type: "file", accept: ".pem,.key,application/x-pem-file",
  });
  const tlsDetails = h("dl", { class: "kv" });
  const tlsStatus = h("div", { class: "muted small" });
  const timezoneInput = h("select", { id: "system-timezone" });
  const timezoneStatus = h("div", { class: "muted small" });
  let placement = { environments: [], business_groups: [] };
  // The logo is sent to the server only when it changed: re-uploading a
  // 700 KB data URI on every name edit would be wasteful, and an omitted
  // field must not be mistaken for a request to remove it.
  const logo = { data: "", filename: "", remove: false, existing: "" };
  const showLogo = (src, name) => {
    if (src) {
      logoPreview.src = src;
      logoPreview.style.display = "block";
      logoName.textContent = name || "Uploaded logo";
      logoRemove.style.display = "inline-flex";
    } else {
      logoPreview.removeAttribute("src");
      logoPreview.style.display = "none";
      logoName.textContent = "No logo configured — reports show the organisation name only";
      logoRemove.style.display = "none";
    }
  };
  showLogo("", "");
  logoRemove.addEventListener("click", () => {
    logo.data = "";
    logo.filename = "";
    logo.remove = true;
    logoInput.value = "";
    showLogo("", "");
  });
  logoInput.addEventListener("change", () => {
    const file = logoInput.files && logoInput.files[0];
    if (!file) return;
    if (!["image/png", "image/jpeg"].includes(file.type)) {
      orgStatus.replaceChildren(alert("error", "Choose a PNG or JPEG image."));
      logoInput.value = "";
      return;
    }
    if (file.size > 1000000) {
      orgStatus.replaceChildren(alert("error", "The logo must be no larger than 1 MB."));
      logoInput.value = "";
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      logo.data = String(reader.result || "");
      logo.filename = file.name;
      logo.remove = false;
      orgStatus.replaceChildren();
      showLogo(logo.data, file.name);
    };
    reader.onerror = () => orgStatus.replaceChildren(
      alert("error", "The logo could not be read."));
    reader.readAsDataURL(file);
  });
  try {
    const value = await api("inventory/settings/organization");
    orgInput.value = value?.name || "";
    preparerName.value = value?.preparer_name || "";
    preparerTitle.value = value?.preparer_title || "";
    concurName.value = value?.concur_name || "";
    concurTitle.value = value?.concur_title || "";
    if (value?.logo_data) {
      logo.existing = value.logo_data;
      showLogo(value.logo_data, value.logo_filename);
    }
  } catch { /* not configured yet */ }
  try {
    const value = await api("identity/settings/audit-retention");
    retentionInput.min = String(value.min_days);
    retentionInput.max = String(value.max_days);
    retentionInput.value = String(value.days);
    const seen = value.oldest
      ? `${value.events.toLocaleString()} events, oldest ${new Date(value.oldest).toLocaleString()}`
      : "No events recorded yet";
    retentionStatus.textContent = `${seen}. Applied by the daily maintenance job.`;
  } catch { /* the server default still applies */ }
  try {
    const value = await api("reporting/settings/report-retention");
    reportRetentionInput.min = String(value.min_days);
    reportRetentionInput.max = String(value.max_days);
    reportRetentionInput.value = String(value.days);
    reportRetentionStatus.textContent =
      `${value.completed.toLocaleString()} archived reports`
      + (value.expired ? `, ${value.expired} awaiting purge` : "");
  } catch { /* the server default still applies */ }
  const describeSmtpPassword = (value) => {
    smtpPassword.placeholder = value.password_unreadable
      ? "Saved password cannot be decrypted — enter it again"
      : value.password_set
        ? "•••••• saved — leave blank to keep"
        : "No password saved";
  };
  try {
    const value = await api("reporting/settings/smtp");
    smtpHost.value = value.host || "";
    smtpPort.value = String(value.port);
    smtpStarttls.checked = Boolean(value.starttls);
    smtpUsername.value = value.username || "";
    smtpSender.value = value.sender || "";
    describeSmtpPassword(value);
    smtpStatus.textContent = value.source === "database"
      ? "Saved on this screen"
        + (value.updated_by ? ` by ${value.updated_by}` : "")
        + (value.updated_at
          ? ` on ${new Date(value.updated_at).toLocaleString()}` : "")
        + ". Scheduled reports use this relay immediately."
      : "Using the deployment's environment settings until a relay is saved "
        + "here. Saving makes the values on this screen authoritative.";
  } catch (error) {
    smtpStatus.textContent = `Relay settings unavailable: ${error.message}`;
  }
  const readFileText = (file) => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("The file could not be read."));
    reader.readAsText(file);
  });
  let tlsState = null;
  const renderTlsState = (value) => {
    tlsState = value;
    const rows = [];
    if (!value?.enabled) {
      tlsStatus.textContent =
        "The gateway is not terminating TLS in this deployment. An external "
        + "reverse proxy manages HTTPS and its certificate; nothing can be "
        + "uploaded here.";
    } else if (value.error) {
      tlsStatus.textContent = `Stored certificate problem: ${value.error}`;
    } else if (value.certificate) {
      const cert = value.certificate;
      rows.push(
        h("dt", {}, "Common name"), h("dd", {}, cert.common_name || "—"),
        h("dt", {}, "Issuer"), h("dd", {}, cert.issuer),
        h("dt", {}, "Valid until"),
        h("dd", {}, new Date(cert.not_after).toLocaleString()),
        h("dt", {}, "SHA-256 fingerprint"), h("dd", {}, cert.sha256),
        h("dt", {}, "Alternative names"), h("dd", {}, cert.san.join(", ") || "—"),
      );
      tlsStatus.textContent = cert.self_signed
        ? "The gateway is using a generated self-signed certificate. Browsers "
          + "warn until you upload a certificate your clients trust."
        : "A CA-issued certificate is installed.";
      if (!value.serving) {
        tlsStatus.textContent +=
          " TLS is not active in this process; restart the gateway to serve "
          + "HTTPS with the stored files.";
      }
    } else {
      tlsStatus.textContent =
        "TLS is enabled but no certificate has been generated yet. Restart the "
        + "gateway, or upload a certificate and key below.";
    }
    tlsDetails.replaceChildren(...rows);
  };
  try {
    renderTlsState(await api("tls"));
  } catch (error) {
    tlsStatus.textContent = `Certificate status unavailable: ${error.message}`;
  }
  try {
    const value = await api("identity/settings/timezone");
    const zones = value?.zones?.length ? value.zones : [value.timezone];
    timezoneInput.replaceChildren(...zones.map((zone) => h("option", {
      value: zone, selected: zone === value.timezone ? true : null,
    }, zone)));
    timezoneStatus.textContent =
      `Currently ${value.timezone}. Schedules, daily snapshots, period `
      + "boundaries and report timestamps all use this zone.";
  } catch { /* the selector still saves a typed zone only if the list loads */ }
  try {
    placement = await api("inventory/settings/placement");
  } catch { /* the free-text fallback in the editor still works */ }
  const flash = SETTINGS_FLASH;
  SETTINGS_FLASH = null;
  const signatureGroup = (heading, nameInput, titleInput) =>
    h("div", { class: "field" },
      h("label", {}, heading),
      h("div", { class: "stack", style: { gap: "6px" } },
        nameInput, titleInput));
  // One column of the side-by-side policy panel: the branding controls are
  // stacked so they fit the column without forcing the grid wider.
  const form = h("div", { class: "stack", style: { gap: "10px" } },
    h("div", { class: "field" },
      h("label", { for: "org-name" }, "Organisation name"), orgInput),
    h("div", { class: "field" },
      h("label", { for: "org-logo" }, "Logo (PNG or JPEG, up to 1 MB)"),
      logoInput),
    h("div", { class: "row", style: { alignItems: "center", gap: "10px" } },
      logoPreview, logoName, logoRemove),
    signatureGroup("Prepared by", preparerName, preparerTitle),
    signatureGroup("Concurred by", concurName, concurTitle),
    h("div", { class: "row", style: { alignItems: "flex-end" } },
      h("button", {
        class: "btn btn-primary",
        onclick: async () => {
          orgStatus.replaceChildren();
          try {
            const body = {
              name: orgInput.value,
              preparer_name: preparerName.value,
              preparer_title: preparerTitle.value,
              concur_name: concurName.value,
              concur_title: concurTitle.value,
              remove_logo: logo.remove,
            };
            if (logo.data) {
              body.logo_data = logo.data;
              body.logo_filename = logo.filename;
            }
            const saved = await api("inventory/settings/organization", {
              method: "PUT", body,
            });
            logo.data = "";
            logo.remove = false;
            logo.existing = saved?.logo_data || "";
            logoInput.value = "";
            if (saved?.logo_data) showLogo(saved.logo_data, saved.logo_filename);
            orgStatus.appendChild(alert("ok", "Branding saved. New reports use it."));
          } catch (error) {
            orgStatus.appendChild(alert("error", error.message));
          }
        },
      }, "Save branding")),
    orgStatus);
  // The release comes from the running image, not a string baked into this
  // file, so a rebuilt deployment cannot show a stale version.
  const applicationVersion = h("dd", {}, "PatchTrack");
  api("version").then((value) => {
    if (value && value.version) {
      applicationVersion.textContent = `PatchTrack ${value.version}`;
    }
  }).catch(() => { /* leave the neutral label if the release is unavailable */ });
  // Each Settings concern gets its own tab, so a long form or two retention
  // windows never compete for one screen. The panels are built once and shown
  // or hidden; the last selected tab survives a re-render.
  const panels = [
    ["branding", "Report branding", card(
      "Report branding",
      "Organisation name, logo and signatories used on generated PDFs. The "
      + "cover carries the logo; the preparer and concurrence blocks appear on "
      + "the sign-off page.", form)],
    ["retention", "Retention", h("div", { class: "stack" },
      card("Report file retention",
        "How long generated PDFs are kept before the maintenance job deletes "
        + "them. Saving applies the window immediately, including to reports "
        + "already on disk; the report record, SHA-256 and size are retained as "
        + "an archive tombstone either way.",
        h("div", { class: "stack" },
          h("div", { class: "row", style: { alignItems: "flex-end" } },
            h("div", { class: "field", style: { minWidth: "220px" } },
              h("label", { for: "report-retention-days" }, "Retention (days)"),
              reportRetentionInput),
            h("button", {
              class: "btn btn-primary",
              onclick: async () => {
                reportRetentionStatus.replaceChildren();
                try {
                  const value = await api("reporting/settings/report-retention", {
                    method: "PUT",
                    body: { days: Number(reportRetentionInput.value) },
                  });
                  reportRetentionStatus.appendChild(alert("ok",
                    `Saved — files older than ${value.days} days are removed. `
                    + `${value.purged_reports} expired file(s) purged now.`));
                } catch (error) {
                  reportRetentionStatus.appendChild(alert("error", error.message));
                }
              },
            }, "Save")),
          reportRetentionStatus)),
      card("Audit trail retention",
        "How long audit events are kept before the maintenance job deletes "
        + "them. The trail is append-only otherwise: no screen or API can edit "
        + "or remove an individual event, and a change to this setting is "
        + "itself audited.",
        h("div", { class: "stack" },
          h("div", { class: "row", style: { alignItems: "flex-end" } },
            h("div", { class: "field", style: { minWidth: "220px" } },
              h("label", { for: "audit-retention-days" }, "Retention (days)"),
              retentionInput),
            h("button", {
              class: "btn btn-primary",
              onclick: async () => {
                retentionStatus.replaceChildren();
                try {
                  const value = await api("identity/settings/audit-retention", {
                    method: "PUT", body: { days: Number(retentionInput.value) },
                  });
                  retentionStatus.appendChild(alert("ok",
                    `Saved — events older than ${value.days} days will be removed.`));
                } catch (error) {
                  retentionStatus.appendChild(alert("error", error.message));
                }
              },
            }, "Save")),
          retentionStatus)))],
    ["email", "Email delivery", card(
      "Email delivery (SMTP relay)",
      "Scheduled reports are emailed through this relay. The password is "
      + "encrypted at rest and is never returned by the API; leave the field "
      + "blank to keep the saved one. Saving applies to the next scheduled "
      + "run without a restart.",
      h("div", { class: "stack" },
        h("div", { class: "row", style: { alignItems: "flex-end" } },
          h("div", { class: "field", style: { minWidth: "280px", flex: "2" } },
            h("label", { for: "smtp-host" }, "Relay host"), smtpHost),
          h("div", { class: "field", style: { width: "120px" } },
            h("label", { for: "smtp-port" }, "Port"), smtpPort)),
        h("label", { class: "row", style: { gap: "6px" } },
          smtpStarttls, "Use STARTTLS"),
        h("div", { class: "field" },
          h("label", { for: "smtp-username" }, "Username (optional)"),
          smtpUsername),
        h("div", { class: "field" },
          h("label", { for: "smtp-password" }, "Password (write-only)"),
          smtpPassword,
          h("label", { class: "row", style: { gap: "6px" } },
            smtpClear, "Remove the saved password")),
        h("div", { class: "field" },
          h("label", { for: "smtp-sender" }, "Sender address"), smtpSender),
        h("div", { class: "field" },
          h("label", { for: "smtp-test-to" }, "Test recipient"), smtpTestTo),
        h("div", { class: "row" },
          h("button", {
            class: "btn btn-primary",
            onclick: async () => {
              smtpStatus.replaceChildren();
              const body = {
                host: smtpHost.value.trim(),
                port: Number(smtpPort.value) || 587,
                starttls: smtpStarttls.checked,
                username: smtpUsername.value.trim(),
                sender: smtpSender.value.trim(),
              };
              if (smtpClear.checked) body.clear_password = true;
              else if (smtpPassword.value) body.password = smtpPassword.value;
              try {
                const value = await api("reporting/settings/smtp", {
                  method: "PUT", body,
                });
                smtpPassword.value = "";
                smtpClear.checked = false;
                describeSmtpPassword(value);
                smtpStatus.appendChild(alert("ok",
                  "Relay saved. Scheduled reports use it from the next run."));
              } catch (error) {
                smtpStatus.appendChild(alert("error", error.message));
              }
            },
          }, "Save relay"),
          h("button", {
            class: "btn",
            onclick: async (event) => {
              smtpStatus.replaceChildren();
              const recipient = smtpTestTo.value.trim();
              if (!recipient) {
                smtpStatus.appendChild(alert("error",
                  "Enter an address to send the test to."));
                return;
              }
              const button = event.currentTarget;
              button.disabled = true;
              button.textContent = "Sending…";
              try {
                await api("reporting/settings/smtp/test", {
                  method: "POST", body: { recipient },
                });
                smtpStatus.appendChild(alert("ok",
                  `Test message accepted by the relay for ${recipient}.`));
              } catch (error) {
                smtpStatus.appendChild(alert("error", error.message));
              } finally {
                button.disabled = false;
                button.textContent = "Send test email";
              }
            },
          }, "Send test email")),
        smtpStatus))],
    ["https", "HTTPS", card(
      "HTTPS certificate",
      "The gateway terminates TLS itself, so the console is served over HTTPS "
      + "from the first installation. A self-signed certificate is generated "
      + "on first start; upload the organisation's certificate here to replace "
      + "it. The key is written beside the certificate with owner-only "
      + "permissions and is never returned by the API. The new certificate is "
      + "used for new connections immediately.",
      h("div", { class: "stack" },
        tlsDetails,
        h("div", { class: "field" },
          h("label", { for: "tls-cert" },
            "Certificate (PEM; include intermediate certificates)"),
          tlsCertFile),
        h("div", { class: "field" },
          h("label", { for: "tls-key" }, "Private key (PEM, unencrypted)"),
          tlsKeyFile),
        h("button", {
          class: "btn btn-primary",
          onclick: async (event) => {
            tlsStatus.replaceChildren();
            if (!tlsState?.enabled) {
              tlsStatus.appendChild(alert("error",
                "The gateway is not terminating TLS in this deployment."));
              return;
            }
            const certificate = tlsCertFile.files && tlsCertFile.files[0];
            const privateKey = tlsKeyFile.files && tlsKeyFile.files[0];
            if (!certificate || !privateKey) {
              tlsStatus.appendChild(alert("error",
                "Choose both the certificate and its private key."));
              return;
            }
            const button = event.currentTarget;
            button.disabled = true;
            button.textContent = "Installing…";
            try {
              const value = await api("tls", {
                method: "PUT",
                body: {
                  certificate: await readFileText(certificate),
                  private_key: await readFileText(privateKey),
                },
              });
              tlsCertFile.value = "";
              tlsKeyFile.value = "";
              renderTlsState(value);
              tlsStatus.appendChild(alert("ok", value.applied
                ? "Certificate installed. New connections use it now."
                : "Certificate saved. Restart the gateway to serve it."));
            } catch (error) {
              tlsStatus.appendChild(alert("error", error.message));
            } finally {
              button.disabled = false;
              button.textContent = "Install certificate";
            }
          },
        }, "Install certificate"),
        tlsStatus))],
    ["placement", "Inventory placement", card(
      "Inventory placement values",
      "The picker offered when an operator corrects environment or business "
      + "service, and the names shown across inventory, compliance, reports and "
      + "the dashboard. A rename updates every server holding the old value; "
      + "the AssetExplorer value is preserved beside it.",
      h("div", { class: "stack" },
        placementOptionSection("environment", "environment",
          placement.environments || []),
        placementOptionSection("business_group", "business service",
          placement.business_groups || [])))],
    ["platform", "Platform", h("div", { class: "stack" },
      card("System timezone",
        "One timezone for the whole deployment. It decides the local calendar "
        + "day a daily snapshot covers, the first and last instant of every "
        + "reporting period and schedule run, and how timestamps are displayed "
        + "here and in generated PDFs. Stored instants remain UTC.",
        h("div", { class: "stack" },
          h("div", { class: "row", style: { alignItems: "flex-end" } },
            h("div", { class: "field", style: { minWidth: "280px" } },
              h("label", { for: "system-timezone" }, "Timezone"), timezoneInput),
            h("button", {
              class: "btn btn-primary",
              onclick: async () => {
                timezoneStatus.replaceChildren();
                try {
                  const value = await api("identity/settings/timezone", {
                    method: "PUT", body: { timezone: timezoneInput.value },
                  });
                  setDisplayTimeZone(value.timezone);
                  timezoneStatus.appendChild(alert("ok",
                    `Saved — PatchTrack now uses ${value.timezone} `
                    + `(local time ${value.local_time}).`));
                } catch (error) {
                  timezoneStatus.appendChild(alert("error", error.message));
                }
              },
            }, "Save timezone")),
          timezoneStatus)),
      card("Platform", "Deployment facts",
        h("dl", { class: "kv" },
          h("dt", {}, "Application"), applicationVersion,
          h("dt", {}, "Report retention"), h("dd", {}, "Editable in Settings"),
          h("dt", {}, "Database"), h("dd", {}, "PostgreSQL"))))],
  ];
  mount(view, h("div", { class: "stack" }, flash,
    tabbedPanels(panels, {
      ariaLabel: "Settings sections", state: SETTINGS_STATE,
    })));
}

const RENDERERS = {
  dashboard: renderDashboard,
  servers: renderServers,
  findings: renderFindings,
  trends: renderTrends,
  inventory: renderInventory,
  reconciliation: renderReconciliation,
  reports: renderReports,
  schedules: renderSchedules,
  integrations: renderIntegrations,
  policies: renderPolicies,
  users: renderUsers,
  audit: renderAudit,
  health: renderHealth,
  backups: renderBackups,
  settings: renderSettings,
};

/* ------------------------------------------------- forced password rotation */
/* A user whose account was just created, or whose password an administrator
 * reset, may not use the console until they set a new password. The gateway
 * enforces this on every other route; this dialog is only the way to satisfy
 * it without a support call. */
function promptPasswordChange(me) {
  const current = h("input", { type: "password", autocomplete: "current-password" });
  const next = h("input", { type: "password", autocomplete: "new-password", minLength: 12 });
  const confirm = h("input", { type: "password", autocomplete: "new-password", minLength: 12 });
  const error = h("div", { class: "small", style: { color: "var(--critical)" }, hidden: true });
  const submit = h("button", { class: "btn btn-primary", type: "submit" }, "Set new password");
  const form = h("form", {
    class: "stack",
    onsubmit: async (event) => {
      event.preventDefault();
      error.hidden = true;
      if (next.value !== confirm.value) {
        error.textContent = "The new passwords do not match.";
        error.hidden = false;
        return;
      }
      submit.disabled = true;
      try {
        const result = await api("session/change-password", {
          method: "POST",
          body: { current_password: current.value, new_password: next.value },
          // A 401 here is a mistyped current password, not an expired
          // session; show it inline instead of bouncing to /login.
          redirectOnUnauthorized: false,
        });
        // The password changed but the gateway could not mint a fresh session
        // (for example the account locked during the change). Send the user to
        // sign in rather than looping back into this dialog.
        window.location.replace(result?.reauthenticate ? "/login" : "/");
      } catch (failure) {
        error.textContent = failure.message;
        error.hidden = false;
        submit.disabled = false;
      }
    },
  },
    h("p", { class: "small secondary" },
      `${me.display_name || me.username}, you must choose a new password before continuing.`),
    h("div", { class: "field" }, h("label", {}, "Current password"), current),
    h("div", { class: "field" }, h("label", {}, "New password (at least 12 characters)"), next),
    h("div", { class: "field" }, h("label", {}, "Confirm new password"), confirm),
    error,
    submit);
  // Not dismissible: every other route answers 403 until this is done, so a
  // close button would leave the user staring at a console that cannot load.
  modal("Set a new password", form, [], { dismissible: false });
}

/* ------------------------------------------------------------------ boot */
async function boot() {
  try {
    const me = await api("me");
    state.user = me;
    // Bound before the rotation check: a user who no longer knows their
    // current password must still be able to sign out.
    document.getElementById("logout").addEventListener("click", async () => {
      // Through api() so the CSRF header travels with the request: the
      // gateway rejects a bare POST here, and signing out would silently
      // leave the session cookie in place.
      try { await api("session/logout", { method: "POST" }); } catch { /* already gone */ }
      window.location.replace("/login");
    });
    if (me.must_change_password) {
      promptPasswordChange(me);
      return;
    }
    document.getElementById("user-name").textContent = me.display_name || me.username;
    document.getElementById("user-role").textContent = me.role.replaceAll("_", " ");
    document.getElementById("user-initials").textContent =
      (me.display_name || me.username).slice(0, 2).toUpperCase();
  } catch {
    window.location.replace("/login");
    return;
  }
  try {
    const zone = await api("identity/settings/timezone");
    setDisplayTimeZone(zone?.timezone);
  } catch { /* display falls back to the browser timezone */ }
  await loadDimensions();
  bindKiosk();
  bindNavToggle();
  document.getElementById("refresh").addEventListener("click", () => renderRoute());
  window.addEventListener("hashchange", renderRoute);
  const route = parseHash().path;
  if (!window.location.hash) navigate(route);
  renderRoute();
}

boot();
