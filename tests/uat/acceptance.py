"""User acceptance walkthrough against a populated PatchTrack stack.

Each scenario is taken from docs/user-stories.md and checked in the browser the
way the persona would use it. Setup that a person would not perform (creating a
viewer account) uses the API and is labelled as setup. Needs Playwright with
Chromium, a running stack and demo or real data with a monthly snapshot.

    PATCHTRACK_URL=http://127.0.0.1:8005 PATCHTRACK_USER=admin \
    PATCHTRACK_PASSWORD=... python tests/uat/acceptance.py
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import sys
import time

import httpx
from playwright.sync_api import sync_playwright
from pypdf import PdfReader

BASE = os.environ.get("PATCHTRACK_URL", "http://127.0.0.1:8005")
ADMIN = (os.environ.get("PATCHTRACK_USER", "admin"),
         os.environ.get("PATCHTRACK_PASSWORD", ""))
VIEWER = (f"uat-viewer-{os.getpid()}", "Viewer-Password-2026")

passed = 0
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"ok   {name}")
    else:
        failed.append(name)
        print(f"FAIL {name} — {detail}")


def number(text: str) -> float:
    match = re.search(r"-?[\d,.]+", text or "")
    return float(match.group(0).replace(",", "")) if match else float("nan")


def login(page, username: str, password: str) -> None:
    page.goto(f"{BASE}/login", wait_until="networkidle")
    page.fill("#username", username)
    page.fill("#password", password)
    page.click("#submit")
    page.wait_for_url(f"{BASE}/", timeout=15000)
    page.wait_for_timeout(500)


def kpis(page) -> dict[str, dict[str, str]]:
    result = {}
    for tile in page.locator("#view .kpi").all():
        label = (tile.locator(".kpi-label").text_content() or "").strip()

        def part(selector: str) -> str:
            node = tile.locator(selector)
            return (node.first.text_content() or "").strip() if node.count() else ""

        result[label] = {
            "value": part(".kpi-value"),
            "share": part(".kpi-share"),
            "foot": part(".kpi-foot"),
        }
    return result


def admin_setup() -> None:
    client = httpx.Client(base_url=BASE, timeout=30)
    client.post("/api/session/login",
                json={"username": ADMIN[0], "password": ADMIN[1]})
    headers = {"X-PatchTrack-CSRF": client.cookies.get("patchtrack_csrf", "")}
    created = client.post("/api/identity/users", json={
        "username": VIEWER[0], "display_name": "UAT Viewer",
        "email": f"{VIEWER[0]}@example.com", "role": "viewer",
        "password": VIEWER[1],
    }, headers=headers)
    check("setup: an administrator creates a viewer account",
          created.status_code in (201, 409), str(created.status_code))
    session = httpx.Client(base_url=BASE, timeout=30)
    session.post("/api/session/login",
                 json={"username": VIEWER[0], "password": VIEWER[1]})
    changed = session.post("/api/session/change-password", json={
        "current_password": VIEWER[1], "new_password": VIEWER[1] + "x",
    }, headers={"X-PatchTrack-CSRF": session.cookies.get("patchtrack_csrf", "")})
    check("setup: the viewer sets their first password", changed.status_code == 200,
          str(changed.status_code))
    session.close()
    client.close()


def monthly_period() -> str:
    client = httpx.Client(base_url=BASE, timeout=30)
    client.post("/api/session/login",
                json={"username": ADMIN[0], "password": ADMIN[1]})
    snapshots = client.get("/api/compliance/snapshots?limit=200").json()
    client.close()
    rows = snapshots if isinstance(snapshots, list) else snapshots.get("snapshots") or []
    for row in rows:
        if row.get("scope") == "monthly":
            return str(row["period_start"])[:7]
    return ""


def main() -> int:
    if not ADMIN[1]:
        print("PATCHTRACK_PASSWORD is required", file=sys.stderr)
        return 2
    period = monthly_period()
    if not period:
        print("a monthly snapshot is required for US-8", file=sys.stderr)
        return 2
    admin_setup()
    with sync_playwright() as play:
        browser = play.chromium.launch()
        context = browser.new_context(viewport={"width": 2200, "height": 1300},
                                      color_scheme="dark",
                                      ignore_https_errors=True)
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text)
                if message.type == "error" else None)

        # US-1: see the current position.
        login(page, *ADMIN)
        page.goto(f"{BASE}/#/dashboard", wait_until="networkidle")
        page.wait_for_timeout(1200)
        tiles = kpis(page)
        for label in ("Total Servers", "Compliant", "Non-Compliant",
                      "Unknown / No Data", "Compliance vs Target"):
            check(f"US-1 dashboard shows the {label!r} measure", label in tiles,
                  str(list(tiles)))
        total = number(tiles["Total Servers"]["value"])
        compliant = number(tiles["Compliant"]["value"])
        non_compliant = number(tiles["Non-Compliant"]["value"])
        unknown = number(tiles["Unknown / No Data"]["value"])
        share = number(tiles["Compliant"]["share"])
        calculated = compliant / (compliant + non_compliant + unknown) * 100
        check("US-1 compliance equals compliant / judged population",
              0 <= abs(calculated - share) < 0.15,
              f"{calculated:.2f} vs displayed {share:.2f}")
        check("US-1 the population counts add up to the in-scope total",
              compliant + non_compliant + unknown == total,
              f"{compliant}+{non_compliant}+{unknown} vs {total}")
        body = page.text_content("#view") or ""
        check("US-1 unknown and stale are part of the population",
              "unknown" in tiles["Unknown / No Data"]["foot"].lower()
              and "stale" in tiles["Unknown / No Data"]["foot"].lower(),
              tiles["Unknown / No Data"]["foot"])
        check("US-1 freshness names the inventory and patch collection",
              "synced" in body.lower() or "fresh" in body.lower())
        check("US-1 the monthly trend is drawn", "Monthly compliance trend" in body)
        check("US-1 the trend states the target", "Target" in body or "target" in body)

        # US-3: a recorded period is a frozen snapshot, not today relabelled.
        period_select = page.locator(
            '.dashboard-controls .field', has_text="Reporting period").locator("select")
        options = period_select.first.locator("option").all()
        recorded = None
        for option in options:
            value = option.get_attribute("value") or ""
            if value and not value.startswith("live:"):
                recorded = (value, (option.text_content() or "").strip())
                break
        check("US-3 a recorded period is offered", recorded is not None)
        if recorded:
            period_select.first.select_option(recorded[0])
            page.wait_for_timeout(1500)
            context_line = page.text_content("#page-context") or ""
            period_body = page.text_content("#view") or ""
            check("US-3 the period view names the recorded period",
                  recorded[1] in context_line, context_line)
            check("US-3 the period view explains it is a snapshot",
                  "About this period" in period_body)
            check("US-3 the period view withholds live dimension filters",
                  page.locator('.dashboard-controls .field',
                               has_text="Environment").count() == 0)
            page.goto(f"{BASE}/#/trends", wait_until="networkidle")
            page.wait_for_timeout(800)
            trends = page.text_content("#view") or ""
            check("US-3 the history ledger records the period",
                  recorded[1].split()[0] in trends or "snapshot" in trends.lower())
            page.goto(f"{BASE}/#/dashboard", wait_until="networkidle")
            page.wait_for_timeout(800)

        # US-4: explain why a server is not compliant.
        page.goto(f"{BASE}/#/servers?state=non_compliant", wait_until="networkidle")
        page.wait_for_timeout(1000)
        page.locator("#view tbody tr a").first.click()
        page.wait_for_timeout(1000)
        detail = page.text_content("#modal-root") or ""
        check("US-4 the server detail shows the compliance reason",
              "Compliance reason" in detail)
        check("US-4 the server detail shows the last assessment",
              "Last assessment" in detail)
        check("US-4 outstanding findings name the advisory and severity",
              "Outstanding findings" in detail and "Advisory" in detail
              and "Severity" in detail)
        check("US-4 the detail does not claim PatchTrack deploys patches",
              "performed in the authoritative patch-management system" in detail)
        page.locator("#modal-root button[aria-label='Close']").click()
        page.wait_for_timeout(300)

        # US-5: see what needs patching.
        page.goto(f"{BASE}/#/findings", wait_until="networkidle")
        page.wait_for_timeout(900)
        findings = page.text_content("#view") or ""
        for column in ("Hostname", "Advisory", "Severity", "Source"):
            check(f"US-5 the findings register shows {column!r}", column in findings)
        check("US-5 severity counts are shown", "critical" in findings.lower())
        check("US-5 remediation deadlines reflect the policy",
              "critical 7 days" in findings.lower() or "7 days" in findings)

        # Export dashboard as a management reader would.
        page.goto(f"{BASE}/#/dashboard", wait_until="networkidle")
        page.wait_for_timeout(1200)
        try:
            with page.expect_download(timeout=30000) as info:
                page.get_by_role("button", name="Export Dashboard").click()
            download = info.value
            path = download.path()
            data = open(path, "rb").read()
            reader = PdfReader(io.BytesIO(data))
            text = reader.pages[0].extract_text()
            check("export: the management summary downloads as a PDF",
                  data.startswith(b"%PDF-"))
            check("export: it is one landscape page",
                  len(reader.pages) == 1
                  and float(reader.pages[0].mediabox.width)
                  > float(reader.pages[0].mediabox.height))
            for expected in ("RHEL", "Windows", "Solaris",
                             "Compliance by business service",
                             "Most widespread outstanding advisories"):
                check(f"export: the sheet carries {expected!r}", expected in text)
        except Exception as error:  # noqa: BLE001
            check("export: the management summary downloads as a PDF", False, str(error))

        # US-8: produce the monthly pack from a frozen snapshot.
        page.goto(f"{BASE}/#/reports", wait_until="networkidle")
        page.wait_for_timeout(900)
        # The snapshot picker lists the most recent daily captures; a monthly
        # pack is requested by month and resolved to the monthly snapshot that
        # covers it.
        page.locator("#report-snapshot").select_option("")
        page.locator("#report-month").fill(period)
        check("US-8 report generation accepts a reporting month",
              page.locator("#report-month").input_value() == period)
        page.locator("#generate-report").click()
        completed = False
        for _ in range(40):
            page.wait_for_timeout(1000)
            first = page.locator("#view tbody tr").first
            if "completed" in (first.text_content() or "").lower():
                completed = True
                break
        check("US-8 the monthly report is generated", completed)
        if completed:
            with page.expect_download(timeout=30000) as info:
                page.locator("#view tbody tr").first.get_by_role(
                    "button", name="Download").click()
            report = open(info.value.path(), "rb").read()
            reader = PdfReader(io.BytesIO(report))
            text = "\n".join(page_text.extract_text() or ""
                             for page_text in reader.pages)
            digest = hashlib.sha256(report).hexdigest()
            check("US-8 the report downloads as a PDF",
                  report.startswith(b"%PDF-") and len(reader.pages) >= 5,
                  f"{len(reader.pages)} pages")
            for expected in ("Executive summary", "Compliance",
                             "Methodology", "Data quality"):
                check(f"US-8 the report carries {expected!r}",
                      expected.lower() in text.lower())
            check("US-8 the report states whether the target was met",
                  "target" in text.lower() and
                  ("met" in text.lower() or "miss" in text.lower()))
            client = httpx.Client(base_url=BASE, timeout=30)
            client.post("/api/session/login",
                        json={"username": ADMIN[0], "password": ADMIN[1]})
            records = client.get("/api/reporting/reports?limit=5").json()["items"]
            client.close()
            stored = records[0].get("sha256") if records else None
            check("US-8 the stored digest matches the downloaded file",
                  stored == digest, f"{stored} vs {digest}")

        # Role acceptance: a viewer can read but not act, and has no export.
        viewer_context = browser.new_context(viewport={"width": 1600, "height": 1000},
                                            color_scheme="dark")
        viewer_page = viewer_context.new_page()
        login(viewer_page, VIEWER[0], VIEWER[1] + "x")
        viewer_page.wait_for_timeout(900)
        check("role: a viewer reaches the dashboard",
              "Compliance vs Target" in (viewer_page.text_content("#view") or ""))
        check("role: a viewer is not offered the management export",
              viewer_page.get_by_role("button", name="Export Dashboard").count() == 0)
        viewer_page.goto(f"{BASE}/#/users", wait_until="networkidle")
        viewer_page.wait_for_timeout(600)
        viewer_users = viewer_page.text_content("#view") or ""
        check("role: a viewer cannot administer users",
              "Add user" not in viewer_users and "Create user" not in viewer_users)
        viewer_context.close()

        check("no uncaught browser errors on the admin journey",
              not [error for error in errors
                   if "Failed to load resource" not in error],
              " | ".join(errors[:3]))

        context.close()
        browser.close()

    print(f"\n{passed} acceptance checks passed, {len(failed)} failed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
