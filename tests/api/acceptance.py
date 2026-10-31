"""Component / API acceptance checks against a running PatchTrack gateway.

Exercises the session, CSRF and role boundaries, every read endpoint the
console uses, a recorded-period view, the synchronous management PDF export
and the asynchronous report pipeline. The account it signs in with must be an
administrator because it creates and exercises a viewer account.

    PATCHTRACK_URL=http://127.0.0.1:8005 PATCHTRACK_USER=admin \
    PATCHTRACK_PASSWORD=... python tests/api/acceptance.py

Point it at a development or demo stack whose data may be changed; it adds a
uniquely named viewer, and never deletes anything.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time

import httpx
from pypdf import PdfReader

BASE = os.environ.get("PATCHTRACK_URL", "http://127.0.0.1:8005")
ADMIN = (os.environ.get("PATCHTRACK_USER", "admin"),
         os.environ.get("PATCHTRACK_PASSWORD", ""))
VIEWER = (f"api-viewer-{os.getpid()}", "Viewer-Password-2026")

passed = 0
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"ok   {name}")
    else:
        failed.append(name)
        print(f"FAIL {name} {detail}")


def session_client(username: str, password: str) -> tuple[httpx.Client, dict]:
    client = httpx.Client(base_url=BASE, timeout=60)
    response = client.post("/api/session/login",
                           json={"username": username, "password": password})
    response.raise_for_status()
    return client, response.json()


def csrf(client: httpx.Client) -> dict[str, str]:
    return {"X-PatchTrack-CSRF": client.cookies.get("patchtrack_csrf", "")}


def main() -> int:
    if not ADMIN[1]:
        print("PATCHTRACK_PASSWORD is required", file=sys.stderr)
        return 2
    anonymous = httpx.Client(base_url=BASE, timeout=30)
    check("anonymous API read is refused",
          anonymous.get("/api/compliance/summary").status_code in (401, 403))
    check("anonymous report export is refused",
          anonymous.post("/api/reporting/reports/dashboard-export",
                         json={}).status_code in (401, 403))

    bad = anonymous.post("/api/session/login",
                         json={"username": ADMIN[0], "password": "wrong-password"})
    check("wrong password is rejected", bad.status_code == 401, str(bad.status_code))

    admin, _ = session_client(*ADMIN)
    check("administrator signs in", admin.cookies.get("patchtrack_session") is not None)

    # CSRF: a mutating call without the header is refused.
    without = admin.post("/api/compliance/evaluate")
    check("mutation without CSRF is refused",
          without.status_code == 403, str(without.status_code))
    check("mutation with CSRF is accepted",
          admin.post("/api/compliance/evaluate", headers=csrf(admin)).status_code == 200)

    for path in (
        "version", "me", "compliance/summary", "compliance/trends?months=12",
        "compliance/snapshots?limit=40", "compliance/snapshot-periods",
        "compliance/policies/active", "inventory/dimensions",
        "inventory/integrations", "inventory/sync-runs?limit=20",
        "inventory/settings/placement", "reporting/reports?limit=40",
        "reporting/schedules", "reporting/scheduler/jobs",
        "reporting/settings/smtp", "reporting/settings/backup",
        "identity/users", "identity/roles", "identity/audit?limit=200",
        "identity/settings/timezone",
    ):
        response = admin.get(f"/api/{path}")
        check(f"GET /api/{path} returns 200",
              response.status_code == 200, str(response.status_code))

    summary = admin.get("/api/compliance/summary").json()
    kpis = summary.get("kpis") or {}
    check("summary carries the headline measures",
          all(k in kpis for k in ("total_servers", "compliant", "non_compliant",
                                  "unknown", "stale", "in_scope")))
    check("summary compliance is a percentage",
          isinstance(kpis.get("compliance_pct"), (int, float))
          and 0 <= kpis["compliance_pct"] <= 100, str(kpis.get("compliance_pct")))
    os_groups = {row["group"] for row in summary.get("by_os") or []}
    check("summary facets every operating system",
          os_groups == {"rhel", "windows", "solaris"}, str(os_groups))
    business = summary.get("by_business_group") or []
    check("summary carries every business service", len(business) >= 3, str(len(business)))
    advisories = (summary.get("management") or {}).get("advisories") or []
    advisory_os = {a.get("os_family") for a in advisories}
    check("advisories are faceted by platform",
          advisory_os == {"rhel", "windows", "solaris"}, str(advisory_os))
    check("each platform keeps at most two advisories",
          all(sum(1 for a in advisories if a.get("os_family") == family) <= 2
              for family in advisory_os), str(advisories))
    check("management units expose group_key",
          all("group_key" in unit for unit in (summary.get("management") or {}).get("units") or []))

    periods = admin.get("/api/compliance/snapshot-periods").json().get("periods") or []
    check("recorded periods are offered", len(periods) > 0, str(len(periods)))
    if periods:
        period = periods[len(periods) // 2]
        recorded = admin.get("/api/compliance/summary",
                             params={"period": period["key"]})
        check("a recorded period returns 200", recorded.status_code == 200,
              str(recorded.status_code))
        body = recorded.json()
        check("a recorded period is labelled as a snapshot",
              body.get("mode") == "period" and body.get("period") is not None,
              str(body.get("mode")))
        mixed = admin.get("/api/compliance/summary",
                          params={"period": period["key"], "environment": "Production"})
        check("a recorded period refuses dimension filters",
              mixed.status_code in (400, 422), str(mixed.status_code))

    dimensions = admin.get("/api/inventory/dimensions").json()
    check("inventory dimensions list environments and business services",
          bool(dimensions.get("environment")) and bool(dimensions.get("business_group")),
          json.dumps(dimensions)[:120])

    servers = admin.get("/api/compliance/servers",
                        params={"state": "non_compliant", "page": 1, "page_size": 5}).json()
    check("the server register pages non-compliant rows",
          servers.get("total", 0) > 0 and len(servers.get("items") or []) <= 5,
          f"total={servers.get('total')}")
    findings = admin.get("/api/compliance/findings",
                         params={"severity": "critical", "page": 1, "page_size": 5}).json()
    check("the findings register filters by severity",
          all(row.get("severity") == "critical" for row in findings.get("items") or []),
          json.dumps(findings.get("items") or [])[:150])

    # --- synchronous management export -----------------------------------
    blocked = admin.post("/api/reporting/reports/dashboard-export",
                         json={"mode": "live", "scope_label": "API check"})
    check("dashboard export requires CSRF", blocked.status_code == 403,
          str(blocked.status_code))

    payload = {
        "mode": "live",
        "scope_label": "Live · all environments · monthly trend",
        "environment": "",
        "kpis": summary["kpis"],
        "remediation": summary.get("dashboard", {}).get("remediation"),
        "management": summary.get("management"),
        "freshness": {
            "total_sources": len((summary.get("freshness") or {}).get("sources") or []),
            "stale_sources": sum(1 for source in
                                 (summary.get("freshness") or {}).get("sources") or []
                                 if source.get("stale") or source.get("age_hours") is None),
            "oldest_age_hours": max(
                [float(source.get("age_hours") or 0)
                 for source in (summary.get("freshness") or {}).get("sources") or []] or [0]),
        },
        "by_environment": summary.get("by_environment") or [],
        "by_business_group": summary.get("by_business_group") or [],
        "by_os": summary.get("by_os") or [],
        "severity": summary.get("severity") or [],
        "trend": [],
        "servers": [],
    }
    export = admin.post("/api/reporting/reports/dashboard-export",
                        json=payload, headers=csrf(admin))
    check("dashboard export returns a PDF", export.status_code == 200
          and export.content.startswith(b"%PDF-"), str(export.status_code))
    if export.content.startswith(b"%PDF-"):
        reader = PdfReader(io.BytesIO(export.content))
        check("dashboard export is one landscape page",
              len(reader.pages) == 1
              and float(reader.pages[0].mediabox.width)
              > float(reader.pages[0].mediabox.height),
              f"{len(reader.pages)} pages")
        text = reader.pages[0].extract_text()
        for expected in ("Patch Management — Transparency Dashboard",
                         "RHEL", "Windows", "Solaris",
                         "Compliance by business service",
                         "Most widespread outstanding advisories"):
            check(f"management PDF carries {expected!r}", expected in text)

    # --- asynchronous report pipeline ------------------------------------
    snapshots = admin.get("/api/compliance/snapshots?limit=40").json()
    rows = snapshots if isinstance(snapshots, list) else snapshots.get("snapshots") or []
    monthly = [row for row in rows if row.get("scope") == "monthly"]
    if monthly:
        created = admin.post("/api/reporting/reports",
                             json={"report_type": "monthly_management",
                                   "snapshot_id": monthly[0]["id"]},
                             headers=csrf(admin))
        check("report request is accepted", created.status_code == 202,
              str(created.status_code))
        report_id = (created.json() or {}).get("id")
        state = None
        for _ in range(60):
            reports = admin.get("/api/reporting/reports?limit=10").json()
            records = reports if isinstance(reports, list) else reports.get("items") or []
            current = next((r for r in records if r.get("id") == report_id), None)
            state = (current or {}).get("status")
            if state in ("completed", "failed"):
                break
            time.sleep(1)
        check("report generation completes", state == "completed", str(state))
        if state == "completed":
            download = admin.get(f"/api/reporting/reports/{report_id}/download")
            check("generated report downloads as a PDF",
                  download.status_code == 200
                  and download.content.startswith(b"%PDF-"),
                  str(download.status_code))
            if download.content.startswith(b"%PDF-"):
                check("generated report has pages",
                      len(PdfReader(io.BytesIO(download.content)).pages) >= 1)
    else:
        check("a monthly snapshot exists for report generation", False)

    # --- role boundary ----------------------------------------------------
    created_user = admin.post("/api/identity/users", json={
        "username": VIEWER[0], "display_name": "API Viewer",
        "email": "api-viewer@example.com", "role": "viewer",
        "password": VIEWER[1],
    }, headers=csrf(admin))
    check("administrator creates a viewer",
          created_user.status_code in (201, 409), str(created_user.status_code))

    viewer, login_body = session_client(*VIEWER)
    changed = viewer.post("/api/session/change-password", json={
        "current_password": VIEWER[1], "new_password": VIEWER[1] + "x",
    }, headers=csrf(viewer))
    check("viewer can rotate the forced password", changed.status_code == 200,
          str(changed.status_code))
    viewer.close()
    viewer, _ = session_client(VIEWER[0], VIEWER[1] + "x")
    check("viewer reads the dashboard",
          viewer.get("/api/compliance/summary").status_code == 200)
    check("viewer cannot run an evaluation",
          viewer.post("/api/compliance/evaluate", headers=csrf(viewer)).status_code == 403)
    check("viewer cannot list users",
          viewer.get("/api/identity/users").status_code == 403)
    check("viewer cannot generate reports",
          viewer.post("/api/reporting/reports",
                      json={"report_type": "monthly_management"},
                      headers=csrf(viewer)).status_code == 403)
    check("viewer can read reports",
          viewer.get("/api/reporting/reports?limit=5").status_code == 200)

    audit = admin.get("/api/identity/audit?limit=200").json()
    events = audit if isinstance(audit, list) else audit.get("events") or audit.get("items") or []
    event_types = {event.get("event_type") for event in events}
    check("the audit trail records sign-ins and user creation",
          "auth.login" in event_types and "user.created" in event_types,
          str(sorted(e for e in event_types if e)[:12]))
    check("the audit trail records a failed sign-in",
          any(event.get("event_type") == "auth.login"
              and event.get("outcome") == "failure" for event in events))

    admin.post("/api/session/logout", headers=csrf(admin))
    check("logout ends the session",
          admin.get("/api/compliance/summary").status_code in (401, 403))
    viewer.close()
    anonymous.close()

    print(f"\n{passed} checks passed, {len(failed)} failed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
