"""Browser smoke test for the PatchTrack SPA.

Requires Playwright with Chromium (an optional development dependency — the
application itself needs no browser). It signs in, visits every route and fails
on: console errors, unhandled rejections, a rendered error alert, a blank view,
or too few dashboard tiles/charts.

    PATCHTRACK_URL=https://127.0.0.1:8005 \
    PATCHTRACK_USER=admin PATCHTRACK_PASSWORD=... \
    python tests/ui/smoke.py --screenshot .runtime/dashboard.png

Not part of the pytest suite: it needs a browser and a running stack.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys

from playwright.sync_api import sync_playwright

ROUTES = [
    "dashboard", "servers", "findings", "trends", "inventory", "reconciliation",
    "reports", "schedules", "integrations", "policies", "users", "audit",
    "health", "settings",
]

BASE = os.environ.get("PATCHTRACK_URL", "https://127.0.0.1:8005")
USER = os.environ.get("PATCHTRACK_USER", "admin")
PASSWORD = os.environ.get("PATCHTRACK_PASSWORD", "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshot")
    args = parser.parse_args()

    if not PASSWORD:
        print("PATCHTRACK_PASSWORD is required", file=sys.stderr)
        return 2

    failures: list[str] = []

    def fail(message: str) -> None:
        failures.append(message)
        print(f"FAIL: {message}")

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - report, do not crash the suite
            print(f"chromium is not available ({exc}); skipping browser smoke test")
            return 0

        # The development stack serves HTTPS with a self-signed certificate;
        # accepting it here is the browser equivalent of curl -k and is limited
        # to this throwaway test context.
        browser_context = browser.new_context(
            viewport={"width": 1600, "height": 1000},
            ignore_https_errors=True,
        )
        page = browser_context.new_page()
        console_errors: list[str] = []

        page.on("console",
                lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))

        page.goto(f"{BASE}/login", wait_until="networkidle")
        page.fill("#username", USER)
        page.fill("#password", PASSWORD)
        page.click("#submit")
        page.wait_for_url(f"{BASE}/", timeout=15000)

        for route in ROUTES:
            console_errors.clear()
            page.goto(f"{BASE}/#/{route}", wait_until="networkidle")
            page.wait_for_timeout(600)
            title = page.text_content("#page-title") or ""
            body = page.text_content("#view") or ""
            if len(body.strip()) < 20:
                fail(f"{route}: view rendered empty")
            if "This view could not be loaded" in body:
                fail(f"{route}: view reported an error — {body.strip()[:200]}")
            if console_errors:
                fail(f"{route}: console errors — {' | '.join(console_errors)}")
            print(f"ok  {route:<15} {title}")

        page.goto(f"{BASE}/#/dashboard", wait_until="networkidle")
        page.wait_for_timeout(800)
        charts = page.locator("#view svg").count()
        tiles = page.locator("#view .stat").count()
        # On a brand-new installation the estate is empty, and every chart
        # renders its documented empty state ("No servers match the current
        # filters.") instead of axes. That is correct behaviour, not a
        # missing panel — so an empty-state placeholder counts toward the
        # panel total. Against a populated database the placeholders cannot
        # appear and real charts are required.
        empty = page.locator("#view .chart-empty").count()
        if tiles < 5:
            fail(f"dashboard: expected at least 5 KPI tiles, found {tiles}")
        if charts + empty < 4:
            fail(f"dashboard: expected at least 4 chart panels, found "
                 f"{charts} charts and {empty} empty states")
        print(f"ok  dashboard charts={charts} empty_states={empty} tiles={tiles}")

        # The composition panels must draw every group the API returns — the
        # business-service list used to stop at seven rows — and the OS panel
        # must label platform families, not versions.
        api_summary = page.evaluate(
            "async () => (await fetch('/api/compliance/summary',"
            " {credentials: 'same-origin'})).json()")
        business_card = page.locator(
            "#view .card", has_text="Compliance by business service")
        business_labels = [t.strip() for t in
                           business_card.locator("svg .axis-label").all_text_contents()]
        expected_business = len(api_summary.get("by_business_group") or [])
        if expected_business and len(business_labels) != expected_business:
            fail(f"dashboard: the business-service panel draws {len(business_labels)} "
                 f"of {expected_business} groups")
        else:
            print(f"ok  dashboard business-service panel draws every group "
                  f"({len(business_labels)})")
        os_card = page.locator("#view .card", has_text="Compliance by operating system")
        os_labels = [t.strip() for t in
                     os_card.locator("svg .axis-label").all_text_contents()]
        family_labels = {"rhel": "RHEL", "windows": "Microsoft Windows",
                         "solaris": "Solaris", "other": "Other"}
        expected_os = {family_labels.get(r["group"], r["group"])
                       for r in (api_summary.get("by_os") or [])}
        if expected_os and set(os_labels) != expected_os:
            fail(f"dashboard: the OS panel labels are {os_labels}, expected "
                 f"{sorted(expected_os)}")
        else:
            print(f"ok  dashboard OS panel labels platform families "
                  f"({', '.join(os_labels) or 'empty'})")

        # --- timeframe: one control for liveness and trend granularity --------
        timeframe = page.locator(
            '.dashboard-controls .field', has_text="Timeframe").locator("select")
        if timeframe.count() != 1:
            fail(f"dashboard: expected one Timeframe control, found {timeframe.count()}")
            period_options: list[str] = []
        else:
            period_options = [o.strip() for o in
                              timeframe.first.locator("option").all_text_contents()]
            expected_live = ["Live · monthly trend", "Live · quarterly trend",
                             "Live · yearly trend"]
            if period_options[:3] != expected_live:
                fail(f"dashboard: Timeframe must start with {expected_live}, "
                     f"got {period_options[:3]}")
            if page.locator(
                    '.dashboard-controls .segmented[aria-label="Basis"]').count():
                fail("dashboard: the separate Basis control is still present")

            for value, title in (("live:quarterly", "Quarterly compliance"),
                                 ("live:yearly", "Yearly compliance"),
                                 ("live:monthly", "Monthly compliance trend")):
                console_errors.clear()
                timeframe.select_option(value)
                page.wait_for_timeout(900)
                if timeframe.first.input_value() != value:
                    fail(f"dashboard: after choosing {value} the active timeframe is "
                         f"{timeframe.first.input_value()!r}")
                if title not in (page.text_content("#view") or ""):
                    fail(f"dashboard: {value} did not render its '{title}' panel")
                if console_errors:
                    fail(f"dashboard: {value} console errors — "
                         + " | ".join(console_errors))

            # The choice is per browser so a wall display keeps it across a reload.
            timeframe.select_option("live:quarterly")
            page.wait_for_timeout(400)
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(800)
            timeframe = page.locator(
                '.dashboard-controls .field', has_text="Timeframe").locator("select")
            kept = timeframe.first.input_value()
            if kept != "live:quarterly":
                fail(f"dashboard: the timeframe choice did not survive a reload "
                     f"(got {kept!r})")
            print("ok  dashboard timeframe control switches live monthly/quarterly/"
                  "yearly trends and the choice persists across a reload")

            # --- recorded period inside the same control ----------------------
            periods = page.evaluate(
                """async () => {
                    const response = await fetch('/api/compliance/snapshot-periods',
                                                 {credentials: 'same-origin'});
                    return response.ok ? (await response.json()).periods : [];
                }""")
            if periods:
                chosen = periods[0]
                console_errors.clear()
                timeframe.select_option(chosen["key"])
                page.wait_for_timeout(900)
                context = page.text_content("#page-context") or ""
                body = page.text_content("#view") or ""
                if chosen["label"] not in context:
                    fail(f"dashboard: period view did not name {chosen['label']} "
                         f"(context was {context!r})")
                if "Recent non-compliant servers" in body:
                    fail("dashboard: the live server list is still shown in a period view")
                if page.locator(
                        '.filterbar .field', has_text="Environment").count() != 0:
                    fail("dashboard: dimension filters are still offered in a period view")
                if console_errors:
                    fail("dashboard: period view console errors — "
                         + " | ".join(console_errors))
                timeframe.select_option("live:monthly")
                page.wait_for_timeout(900)
                if "Recent non-compliant servers" not in (page.text_content("#view") or ""):
                    fail("dashboard: returning to Live did not restore the live panels")
                print(f"ok  dashboard timeframe offers {len(period_options) - 3} "
                      f"recorded periods and switches to {chosen['label']} and back")
            else:
                print("ok  dashboard timeframe present; no snapshots recorded yet")
        # --- auto-refresh ----------------------------------------------------
        refresh = page.locator(
            '.dashboard-controls .field', has_text="Auto-refresh").locator("select")
        options: list[str] = []
        if refresh.count() != 1:
            fail(f"dashboard: expected one auto-refresh control, found {refresh.count()}")
        else:
            options = [o.strip() for o in refresh.first.locator("option").all_text_contents()]
            if options != ["Off", "30 seconds", "1 minute", "5 minutes"]:
                fail(f"dashboard: auto-refresh options are {options}")
        print(f"ok  dashboard auto-refresh options={options}")

        # --- TV / kiosk mode -------------------------------------------------
        sidebar = page.locator(".sidebar")
        if not sidebar.is_visible():
            fail("dashboard: the sidebar should be visible before TV mode is enabled")
        page.click("#kiosk-toggle")
        # Move off the button: a transform does not re-evaluate :hover until
        # the pointer moves, so the header would otherwise stay revealed.
        page.mouse.move(800, 500)
        page.wait_for_timeout(300)
        if sidebar.is_visible():
            fail("dashboard: the sidebar is still visible in TV mode")
        if "kiosk" not in (page.get_attribute("body", "class") or ""):
            fail("dashboard: body.kiosk was not set in TV mode")
        if page.get_attribute("#kiosk-toggle", "aria-pressed") != "true":
            fail("dashboard: the TV mode button did not report aria-pressed=true")
        header = page.locator(".topbar")
        if header.bounding_box()["y"] > -1:
            fail("dashboard: the header is still on screen in TV mode")
        page.mouse.move(700, 3)            # top edge of the upper screen
        page.wait_for_timeout(350)
        if header.bounding_box()["y"] < 0:
            fail("dashboard: hovering the top edge did not reveal the header")
        page.mouse.move(700, 500)
        page.wait_for_timeout(350)
        if header.bounding_box()["y"] > -1:
            fail("dashboard: the revealed header did not hide again")
        page.keyboard.press("k")          # K returns to the navigation
        page.wait_for_timeout(300)
        if not sidebar.is_visible():
            fail("dashboard: pressing K did not leave TV mode")
        page.goto(f"{BASE}/#/dashboard?tv=1", wait_until="networkidle")
        page.wait_for_timeout(600)
        if sidebar.is_visible():
            fail("dashboard: the #/dashboard?tv=1 deep link did not hide the navigation")
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        if not sidebar.is_visible():
            fail("dashboard: Escape did not leave TV mode")
        print("ok  dashboard TV mode hides the navigation and header, with a "
              "top-edge reveal (button, K, Esc, ?tv=1)")

        # --- every register can be filtered and sorted ------------------------
        # Server list and inventory sort server-side (the page holds one page of
        # rows); the other registers filter and sort in the browser. Both use the
        # same Search / Sort by controls, so the check is the same.
        list_routes = ["servers", "findings", "trends", "inventory",
                       "reconciliation", "reports", "schedules", "integrations",
                       "policies", "users", "audit"]
        for route in list_routes:
            console_errors.clear()
            page.goto(f"{BASE}/#/{route}", wait_until="networkidle")
            page.wait_for_timeout(500)
            # Server list and inventory put the controls in the page-level
            # filter bar; the other registers carry their own list toolbar.
            if page.locator("#view .list-tools, #view .filterbar").count() == 0:
                fail(f"{route}: no filter/sort controls on the list")
                continue
            if page.locator("#view input[type=search]").count() == 0:
                fail(f"{route}: no search box on the list")
            if page.get_by_label("Sort by").count() == 0:
                fail(f"{route}: no Sort by control on the list")
            if (route != "reports"
                    and page.locator("table.data tbody tr").count()
                    and page.locator("table.data td[colspan]").count() == 0
                    and page.locator(".table-export").count() == 0):
                fail(f"{route}: a populated list has no Export CSV button")
            if console_errors:
                fail(f"{route}: console errors — {' | '.join(console_errors)}")
        print(f"ok  {len(list_routes)} registers expose filter and sort controls")

        # --- an integration can be tested without collecting -----------------
        # The seeded demo integrations have no endpoint, so the expected
        # outcome is a *reported* failure — not a button that does nothing,
        # and not a probe that writes a collection run.
        console_errors.clear()
        page.goto(f"{BASE}/#/integrations", wait_until="networkidle")
        page.wait_for_timeout(500)
        probe_card = page.locator(".card", has_text="AssetExplorer").first
        if probe_card.get_by_role("button", name="Test configuration").count() != 1:
            fail("integrations: no Test configuration button")
        else:
            probe_card.get_by_role("button", name="Test configuration").click()
            try:
                probe_card.locator(".alert").first.wait_for(timeout=40_000)
            except Exception:
                fail("integrations: Test configuration produced no result")
            else:
                text = probe_card.locator(".alert").first.inner_text()
                if "ms)" not in text:
                    fail(f"integrations: unhelpful test result — {text}")
                else:
                    print(f"ok  integrations report a configuration test — {text.splitlines()[-1]}")
        if console_errors:
            fail(f"integrations: console errors — {' | '.join(console_errors)}")

        # --- CSV export covers the filtered list, not just the visible page ---
        page.goto(f"{BASE}/#/findings", wait_until="networkidle")
        page.wait_for_timeout(400)
        page.locator("#view input[type=search]").fill("critical")
        page.wait_for_timeout(500)
        count_text = page.locator(".list-count").text_content() or ""
        with page.expect_download() as download_info:
            page.locator(".table-export").first.click()
        exported = list(csv.reader(io.StringIO(
            open(download_info.value.path(), encoding="utf-8-sig").read())))
        match = re.search(r"Showing ([\d,]+)", count_text)
        if match:
            expected = int(match.group(1).replace(",", ""))
            if len(exported) - 1 != expected:
                fail(f"findings: CSV has {len(exported) - 1} rows, "
                     f"the filtered list shows {expected}")
        # The register remembers its search; clear it so later checks start
        # from the unfiltered list.
        page.locator("#view input[type=search]").fill("")
        page.wait_for_timeout(400)
        # A server-paged register must export every matching row, not one page.
        page.goto(f"{BASE}/#/inventory", wait_until="networkidle")
        page.wait_for_timeout(400)
        pager = page.locator(".pagination .muted").first.text_content() or ""
        total = re.search(r"([\d,]+) assets", pager)
        with page.expect_download() as download_info:
            page.locator(".table-export").first.click()
        exported = list(csv.reader(io.StringIO(
            open(download_info.value.path(), encoding="utf-8-sig").read())))
        if total:
            expected = int(total.group(1).replace(",", ""))
            if len(exported) - 1 != expected:
                fail(f"inventory: CSV has {len(exported) - 1} rows, "
                     f"the register holds {expected}")
        print("ok  CSV export follows the active filters and spans every page")

        # Presentation and operations views are deliberately not exportable.
        for route in ("dashboard", "health", "settings", "reports"):
            page.goto(f"{BASE}/#/{route}", wait_until="networkidle")
            page.wait_for_timeout(400)
            if page.locator(".table-export").count():
                fail(f"{route}: an Export CSV button is still present")
        print("ok  dashboard, operational health, settings and reports have no CSV export")

        # Report deletion is selectable in the UI for an administrator, but the
        # checkbox column must not appear as data in the CSV export.
        page.goto(f"{BASE}/#/reports", wait_until="networkidle")
        page.wait_for_timeout(400)
        if not page.locator("table.data .select-cell input[type=checkbox]").count():
            fail("reports: no selection checkboxes for an administrator")
        if page.get_by_role("button", name="Delete selected").count() == 0:
            fail("reports: Delete selected button is missing for an administrator")
        print("ok  reports offer administrator selection and deletion")

        # Filtering narrows the rendered rows and says so, rather than silently
        # showing a subset.
        page.goto(f"{BASE}/#/audit", wait_until="networkidle")
        page.wait_for_timeout(500)
        before = page.locator("#view tbody tr").count()
        page.locator("#view input[type=search]").first.fill("login")
        page.wait_for_timeout(500)
        after = page.locator("#view tbody tr").count()
        count_text = (page.text_content("#view .list-count") or "").strip()
        if not (0 < after < before):
            fail(f"audit: searching 'login' showed {after} rows of {before}")
        if not count_text.startswith("Showing"):
            fail(f"audit: the count line did not report the filtered subset ({count_text!r})")
        page.locator("#view input[type=search]").first.fill("")
        page.wait_for_timeout(500)
        if page.locator("#view tbody tr").count() != before:
            fail("audit: clearing the search did not restore every row")
        order = page.locator(
            "#view .list-tools button[title='Reverse the sort direction']").first
        label_before = order.text_content().strip()
        order.click()
        page.wait_for_timeout(400)
        if order.text_content().strip() == label_before:
            fail("audit: the order control did not reverse the sort")
        print(f"ok  audit search and sort narrow and re-order the register "
              f"({before} → {after} rows)")

        # The paged server list sorts through the API, so the whole result set
        # is ordered rather than the visible page.
        page.goto(f"{BASE}/#/servers", wait_until="networkidle")
        page.wait_for_timeout(800)
        page.get_by_label("Sort by").first.select_option("hostname")
        page.wait_for_timeout(900)
        hostnames = [h.strip() for h in
                     page.locator("#view tbody tr td:first-child").all_text_contents()]
        if hostnames != sorted(hostnames, key=str.lower):
            fail(f"servers: sort by hostname is not ordered — {hostnames[:3]}")
        total_before = (page.text_content("#view .right.muted") or "").strip()
        page.locator("#view input[type=search]").first.fill("win-")
        page.wait_for_timeout(1000)
        total_after = (page.text_content("#view .right.muted") or "").strip()
        if total_after == total_before or "matching" not in total_after:
            fail(f"servers: searching did not narrow the result ({total_before!r} → "
                 f"{total_after!r})")
        print(f"ok  servers sort server-side and search narrows "
              f"({total_before} → {total_after})")

        # The search box is not a hostname box: a term that occurs only in
        # another column (here the business service) must still match.
        page.locator("#view input[type=search]").first.fill("Core Banking")
        page.wait_for_timeout(1000)
        column_total = (page.text_content("#view .right.muted") or "").strip()
        matched_rows = page.locator("#view tbody tr")
        matched_business = (
            (matched_rows.first.locator("td").nth(4).text_content() or "").strip()
            if matched_rows.count() else ""
        )
        if column_total.startswith("0 ") or "Core Banking" not in matched_business:
            fail(f"servers: a term in the business-service column did not match "
                 f"({column_total!r}, business={matched_business!r})")
        print(f"ok  servers search matches a non-hostname column ({column_total})")

        # A failing server can be verified by a person: the detail drawer
        # offers a justification form with evidence, without changing the
        # compliance state itself.
        page.goto(f"{BASE}/#/servers?state=non_compliant", wait_until="networkidle")
        page.wait_for_timeout(900)
        rows = page.locator("#view tbody tr")
        if not rows.count():
            fail("justifications: no non-compliant server to inspect")
        rows.first.locator("a").first.click()
        page.wait_for_timeout(900)
        if not page.get_by_text("Verification and justification").count():
            fail("justifications: the server detail has no verification card")
        elif not page.locator("#justification-reason").is_visible():
            fail("justifications: the verification form or reason picker is missing")
        elif not page.locator("#justification-evidence").count():
            fail("justifications: the form has no evidence file input")
        else:
            print("ok  a failing server offers verification with evidence")
        page.locator("#modal-root button[aria-label='Close']").click()
        page.wait_for_timeout(300)

        # Server inventory: the Status filter is an exact state, and a state
        # name in the search box is matched as a whole term. Searching
        # "Compliant" must not drag in every "Non-compliant" server — it did,
        # and the screen also lost the caret between keystrokes.
        page.goto(f"{BASE}/#/inventory", wait_until="networkidle")
        page.wait_for_timeout(800)
        page.get_by_label("Status").select_option("compliant")
        page.wait_for_timeout(1000)
        statuses = [s.strip() for s in
                    page.locator("#view tbody tr .pill").all_text_contents()]
        if not statuses:
            fail("inventory: the Status filter returned no rows")
        elif any("non" in s.lower() for s in statuses):
            fail(f"inventory: the Status filter mixed in other states ({statuses[:3]})")
        else:
            print(f"ok  inventory Status filter selects one state ({len(statuses)} rows)")

        page.get_by_label("Status").select_option("")
        page.locator("#view input[type=search]").first.fill("Compliant")
        page.wait_for_timeout(1200)
        searched = [s.strip() for s in
                    page.locator("#view tbody tr .pill").all_text_contents()]
        if not searched or any(s.lower() != "compliant" for s in searched):
            fail(f"inventory: searching 'Compliant' returned other states "
                 f"({searched[:5]})")
        else:
            print(f"ok  inventory search 'Compliant' returns only compliant rows "
                  f"({len(searched)} rows)")

        # The re-render after the debounced search must not interrupt typing:
        # focus and caret stay in the box across the server round trip.
        box = page.locator("#view input[type=search]").first
        box.fill("")
        box.type("win-", delay=60)
        page.wait_for_timeout(1400)
        focused = page.evaluate(
            "() => { const el = document.activeElement;"
            " return { tag: el && el.tagName, type: el && el.type,"
            " value: el && el.value, caret: el && el.selectionStart }; }")
        if (focused.get("type") != "search" or focused.get("value") != "win-"
                or focused.get("caret") != 4):
            fail(f"inventory: the search box lost focus, text or caret ({focused})")
        else:
            print("ok  inventory search keeps focus and caret across the re-render")

        # Operational health is split by concern so the integration table and
        # the run history are not buried under the service list.
        page.goto(f"{BASE}/#/health", wait_until="networkidle")
        page.wait_for_timeout(500)
        health_tabs = [tab.inner_text() for tab in page.get_by_role("tab").all()]
        if health_tabs != ["Services", "Data freshness",
                           "Integrations", "Collection runs"]:
            fail(f"operational health: unexpected tabs {health_tabs}")
        else:
            print("ok  operational health is split into service, freshness, "
                  "integration and run tabs")
        page.get_by_role("tab", name="Collection runs").click()
        page.wait_for_timeout(200)
        if not page.locator('[role="tabpanel"]:not([hidden])').get_by_text(
                "Recent collection runs").is_visible():
            fail("operational health: the Collection runs tab is not visible")
        else:
            print("ok  the Collection runs tab shows the run history")

        # Settings manages the environment / business-service picker. Add,
        # rename and delete must all work there, and the placement editor must
        # then offer the curated list instead of free text — a typo in a
        # placement value silently splits every report and filter.
        page.goto(f"{BASE}/#/settings", wait_until="networkidle")
        page.wait_for_timeout(700)
        retention = page.locator("#audit-retention-days")
        if retention.count() != 1 or not retention.input_value().isdigit():
            fail("settings: the audit retention control is missing or has no value")
        elif not (30 <= int(retention.input_value()) <= 36500):
            fail("settings: the audit retention value is outside 30–36500 days")
        else:
            print("ok  settings exposes the audit retention window")
        report_retention = page.locator("#report-retention-days")
        if (report_retention.count() != 1
                or not report_retention.input_value().isdigit()):
            fail("settings: the report retention control is missing or has no value")
        elif not (30 <= int(report_retention.input_value()) <= 36500):
            fail("settings: the report retention value is outside 30–36500 days")
        else:
            print("ok  settings exposes the report file retention window")
        branding = ["#org-name", "#org-logo", "#preparer-name",
                    "#preparer-title", "#concur-name", "#concur-title"]
        missing = [selector for selector in branding
                   if page.locator(f"#view {selector}").count() != 1]
        if missing:
            fail(f"settings: report branding controls missing: {missing}")
        else:
            print("ok  settings exposes logo upload and both signatories")
        tabs = page.get_by_role("tab")
        tab_labels = [tab.inner_text() for tab in tabs.all()]
        if tab_labels != ["Report branding", "Retention", "Email delivery",
                          "HTTPS", "Inventory placement", "Platform"]:
            fail(f"settings: unexpected tabs {tab_labels}")
        else:
            print("ok  settings is split into branding, retention, email delivery, "
                  "HTTPS, placement and platform tabs")
        page.get_by_role("tab", name="Retention").click()
        page.wait_for_timeout(200)
        if not page.locator("#report-retention-days").is_visible():
            fail("settings: the Retention tab does not show the report retention control")
        elif not page.locator("#audit-retention-days").is_visible():
            fail("settings: the Retention tab does not show the audit retention control")
        else:
            print("ok  the Retention tab shows both retention windows")
        page.get_by_role("tab", name="Platform").click()
        page.wait_for_timeout(200)
        zone_select = page.locator("#system-timezone")
        if not zone_select.is_visible():
            fail("settings: the Platform tab does not show the timezone selector")
        elif zone_select.locator("option").count() < 2:
            fail("settings: the timezone selector has no zones")
        else:
            print("ok  the Platform tab offers the system timezone selector")
        page.get_by_role("tab", name="Email delivery").click()
        page.wait_for_timeout(200)
        smtp_fields = ["#smtp-host", "#smtp-port", "#smtp-username",
                       "#smtp-password", "#smtp-sender", "#smtp-test-to"]
        missing = [selector for selector in smtp_fields
                   if page.locator(f"#view {selector}").count() != 1]
        if missing:
            fail(f"settings: SMTP relay controls missing: {missing}")
        elif not page.locator("#smtp-password").get_attribute("type") == "password":
            fail("settings: the SMTP password field is not masked")
        else:
            print("ok  the Email delivery tab shows the masked relay settings")
        page.get_by_role("tab", name="HTTPS").click()
        page.wait_for_timeout(200)
        tls_controls = ["#tls-cert", "#tls-key"]
        missing = [selector for selector in tls_controls
                   if page.locator(f"#view {selector}").count() != 1]
        if missing:
            fail(f"settings: HTTPS certificate controls missing: {missing}")
        elif not page.locator("#view").get_by_role(
                "button", name="Install certificate").count():
            fail("settings: the certificate install button is missing")
        else:
            print("ok  the HTTPS tab offers certificate and key upload")
        page.get_by_role("tab", name="Inventory placement").click()
        page.wait_for_timeout(300)
        env_input = page.locator("#view input[placeholder='New environment value']")
        if env_input.count() != 1:
            fail("settings: the environment list has no add field")
        # Unique per run, so a crashed previous run cannot make this pass on a
        # stale row or leave a duplicate behind.
        smoke_env = f"Smoke Env {os.getpid()}"
        smoke_env_renamed = f"{smoke_env} renamed"
        env_input.fill(smoke_env)
        page.locator("#view").get_by_role("button", name="Add").first.click()
        page.wait_for_timeout(1200)
        added = page.locator("#view tbody tr", has_text=smoke_env)
        if added.count() != 1:
            fail("settings: the new environment value was not listed")
        else:
            print("ok  settings adds an environment value")

        added.get_by_role("button", name="Edit").click()
        page.wait_for_selector(".modal", timeout=4000)
        page.locator(".modal input").fill(smoke_env_renamed)
        page.locator(".modal").get_by_role("button", name="Save", exact=True).click()
        page.wait_for_timeout(1200)
        if page.locator("#view tbody tr", has_text=smoke_env_renamed).count() != 1:
            fail("settings: the renamed environment value was not listed")
        else:
            print("ok  settings renames an environment value")

        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("#view tbody tr", has_text=smoke_env_renamed).get_by_role(
            "button", name="Delete").click()
        page.wait_for_timeout(1200)
        if page.locator("#view tbody tr", has_text=smoke_env_renamed).count():
            fail("settings: the deleted environment value is still listed")
        else:
            print("ok  settings deletes an environment value")

        # A value that servers still hold cannot be deleted: the inventory must
        # never show a placement the list no longer recognises. The button is
        # disabled, so the refusal is visible before the click.
        in_use_delete = page.locator("#view tbody tr button:disabled").filter(
            has_text="Delete")
        if in_use_delete.count() == 0:
            fail("settings: Delete is enabled for a value that is still in use")
        else:
            print("ok  settings disables Delete for an in-use value")

        # A local placement override is marked as such and clears back to the
        # AssetExplorer value. The source value is never edited by the app, so
        # the override is the only thing the save changes.
        page.goto(f"{BASE}/#/inventory", wait_until="networkidle")
        page.wait_for_timeout(900)
        box = page.locator("#view input[type=search]").first
        box.fill("")
        page.wait_for_timeout(900)
        page.locator("#view tbody tr").first.get_by_role("button", name="Edit").click()
        page.wait_for_selector(".modal", timeout=4000)
        env_select = page.locator(".modal select").first
        if env_select.count() == 0:
            fail("inventory: the placement editor does not offer the Settings list")
        else:
            print("ok  placement editor offers the configured environment list")
            env_select.select_option(index=1)
        page.locator(".modal textarea").fill("Browser smoke test override")
        page.get_by_role("button", name="Save override").click()
        page.wait_for_timeout(2500)
        marked = page.locator("#view tbody tr").first.locator(".override-mark").count()
        if page.locator(".modal").count():
            page.get_by_role("button", name="Cancel").click()
        if marked == 0:
            fail("inventory: a saved placement override is not marked on the row")
        else:
            print("ok  inventory placement override is saved and marked")
            page.locator("#view tbody tr").first.get_by_role("button", name="Edit").click()
            page.wait_for_selector(".modal", timeout=4000)
            page.get_by_role("button", name="Use AssetExplorer values").click()
            page.wait_for_timeout(2500)
            if page.locator("#view tbody tr").first.locator(".override-mark").count():
                fail("inventory: clearing the override did not restore the source value")
            else:
                print("ok  inventory placement override clears back to the source value")

        page.goto(f"{BASE}/#/findings", wait_until="networkidle")
        page.wait_for_timeout(600)
        finding_rows = page.locator("#view tbody tr").count()
        page.locator("#view input[type=search]").first.fill("sccm")
        page.wait_for_timeout(600)
        sccm_rows = page.locator("#view tbody tr").count()
        if not (0 < sccm_rows < finding_rows):
            fail(f"findings: searching a source column showed {sccm_rows} of "
                 f"{finding_rows} rows")
        print(f"ok  findings search matches the source column "
              f"({finding_rows} → {sccm_rows} rows)")

        # --- chart labels are data, never markup -----------------------------
        # Group and environment names come from inventory and can contain HTML
        # metacharacters. Legends and tooltips must render them as text.
        escaping = page.evaluate(
            """async () => {
                const charts = await import('/static/charts.js');
                const host = document.createElement('div');
                document.body.appendChild(host);
                const payload = '<img src=x onerror="window.__ptXss=1">';
                charts.legend(host, [{label: payload, color: '#3987e5'}]);
                const plot = document.createElement('div');
                document.body.appendChild(plot);
                charts.stackedColumns(plot, {
                    rows: [{group: payload, overdue: 3}],
                    keys: ['overdue'],
                    labels: {overdue: 'Overdue'},
                    colors: {overdue: '#d9534f'},
                });
                const rect = plot.querySelector('rect');
                rect.dispatchEvent(new MouseEvent(
                    'mouseenter', {clientX: 20, clientY: 20, bubbles: true}));
                const tip = document.querySelector('.tooltip');
                const result = {
                    legendInjected: host.querySelector('img') !== null,
                    tooltipInjected: !!tip && tip.querySelector('img') !== null,
                    tooltipText: tip ? tip.textContent : '',
                };
                host.remove();
                plot.remove();
                if (tip) tip.style.display = 'none';
                return result;
            }""")
        if escaping["legendInjected"] or escaping["tooltipInjected"]:
            fail("charts: an inventory value was parsed as HTML in a chart label")
        elif "onerror" not in escaping["tooltipText"]:
            fail("charts: the HTML-special group name is missing from the tooltip")
        else:
            print("ok  chart legends and tooltips render group names as text")

        if args.screenshot:
            page.screenshot(path=args.screenshot, full_page=True)
            print(f"screenshot: {args.screenshot}")

        browser_context.close()
        browser.close()

    if failures:
        print("SMOKE TEST FAILED")
        return 1
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
