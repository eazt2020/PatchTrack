"""Application security acceptance checks against a running PatchTrack stack.

Covers response headers and CSP, cookie attributes, CSRF, injection and
traversal handling, request-size limits, method handling, account
enumeration, lockout and login throttling, and session termination.

    PATCHTRACK_URL=http://127.0.0.1:8005 PATCHTRACK_USER=admin \
    PATCHTRACK_PASSWORD=... python tests/security/acceptance.py

It creates a throwaway account and signs in repeatedly, so run it against a
development or demo stack, last: the per-address login throttle applies to
every later sign-in from the same address for fifteen minutes.
"""

from __future__ import annotations

import os
import socket
import sys
from urllib.parse import urlparse

import httpx

BASE = os.environ.get("PATCHTRACK_URL", "https://127.0.0.1:8005")
ADMIN = (os.environ.get("PATCHTRACK_USER", "admin"),
         os.environ.get("PATCHTRACK_PASSWORD", ""))
LOCK_USER = (f"security-lock-{os.getpid()}", "Lockout-Password-2026")
PROBE_USER = f"no-such-user-{os.getpid()}"

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


def csrf(client: httpx.Client) -> dict[str, str]:
    return {"X-PatchTrack-CSRF": client.cookies.get("patchtrack_csrf", "")}


def main() -> int:
    if not ADMIN[1]:
        print("PATCHTRACK_PASSWORD is required")
        return 2
    anonymous = httpx.Client(base_url=BASE, timeout=30)
    page = anonymous.get("/login")
    headers = {key.lower(): value for key, value in page.headers.items()}
    expected_headers = {
        "content-security-policy": "default-src 'self'",
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "referrer-policy": "no-referrer",
        "permissions-policy": "camera=()",
    }
    for name, expected in expected_headers.items():
        check(f"security header {name} is present",
              expected in headers.get(name, ""), headers.get(name, "<missing>"))
    csp = headers.get("content-security-policy", "")
    for directive in ("script-src 'self'", "frame-ancestors 'none'",
                      "base-uri 'none'", "form-action 'self'"):
        check(f"CSP contains {directive!r}", directive in csp, csp)
    if BASE.startswith("https://"):
        check("HSTS is present over TLS",
              "max-age=" in headers.get("strict-transport-security", ""))
    else:
        check("HSTS is correctly omitted on a plain-HTTP development stack",
              "strict-transport-security" not in headers)
    check("no wildcard CORS header is returned",
          "*" not in headers.get("access-control-allow-origin", ""))
    check("the response does not advertise a server banner",
          "server" not in headers)

    # --- session cookies -------------------------------------------------
    admin = httpx.Client(base_url=BASE, timeout=30)
    login = admin.post("/api/session/login",
                       json={"username": ADMIN[0], "password": ADMIN[1]})
    check("administrator signs in", login.status_code == 200)
    cookies = admin.cookies
    set_cookie = login.headers.get_list("set-cookie")
    session_attributes = " ".join(line for line in set_cookie
                                  if line.startswith("patchtrack_session="))
    csrf_attributes = " ".join(line for line in set_cookie
                               if line.startswith("patchtrack_csrf="))
    check("the session cookie is HttpOnly", "httponly" in session_attributes.lower(),
          session_attributes)
    check("the session cookie is SameSite=Strict",
          "samesite=strict" in session_attributes.lower(), session_attributes)
    check("the session cookie is scoped to /",
          "path=/" in session_attributes.lower(), session_attributes)
    check("the CSRF cookie is intentionally readable by the page",
          "httponly" not in csrf_attributes.lower(), csrf_attributes)
    first_csrf = cookies.get("patchtrack_csrf")
    relogin = httpx.Client(base_url=BASE, timeout=30)
    relogin.post("/api/session/login",
                 json={"username": ADMIN[0], "password": ADMIN[1]})
    check("a fresh sign-in issues a fresh CSRF token",
          relogin.cookies.get("patchtrack_csrf") != first_csrf)
    relogin.close()

    # --- CSRF -------------------------------------------------------------
    check("a mutation without a CSRF token is refused",
          admin.post("/api/compliance/evaluate").status_code == 403)
    wrong = {"X-PatchTrack-CSRF": "not-the-cookie-value"}
    check("a mutation with the wrong CSRF token is refused",
          admin.post("/api/compliance/evaluate", headers=wrong).status_code == 403)
    check("a mutation with the matching CSRF token is accepted",
          admin.post("/api/compliance/evaluate", headers=csrf(admin)).status_code == 200)

    # --- injection and traversal -----------------------------------------
    baseline = admin.get("/api/compliance/servers",
                         params={"search": "rhel", "page_size": 50}).json()
    for payload in ("' OR 1=1 --", "'); DROP TABLE inventory.assets; --",
                    "%' UNION SELECT password_hash FROM identity.users --"):
        response = admin.get("/api/compliance/servers",
                             params={"search": payload, "page_size": 50})
        text = response.text.lower()
        check(f"SQL metacharacters are handled as data: {payload[:28]!r}",
              response.status_code == 200
              and response.headers.get("content-type", "").startswith("application/json")
              and "traceback" not in text and "psycopg" not in text
              and "syntax error" not in text,
              f"{response.status_code} {response.text[:100]}")
        check(f"the injection payload does not return the whole register: {payload[:28]!r}",
              response.status_code != 200
              or response.json().get("total", 0) <= baseline.get("total", 0),
              f"{response.json().get('total')} vs {baseline.get('total')}")

    for path in (
        "/api/reporting/reports/%2e%2e%2f%2e%2e%2fidentity/users",
        "/api/reporting/reports/..%2f..%2fidentity%2fusers",
        "/api/inventory/..%2f..%2fidentity/users",
        "/api/compliance/summary/%2e%2e%2f%2e%2e%2fidentity",
    ):
        response = admin.get(path)
        check(f"encoded path traversal is refused: {path[:52]!r}",
              response.status_code in (400, 404, 405), str(response.status_code))

    xss = "<script>alert(1)</script><img src=x onerror=alert(1)>"
    response = admin.get("/api/compliance/servers", params={"search": xss})
    check("a script payload is returned as JSON, not HTML",
          response.status_code == 200
          and response.headers.get("content-type", "").startswith("application/json")
          and "text/html" not in response.headers.get("content-type", ""),
          response.headers.get("content-type", ""))
    api_html = admin.get("/api/compliance/summary",
                         headers={"Accept": "text/html"})
    check("the API does not negotiate an HTML response",
          "text/html" not in api_html.headers.get("content-type", ""),
          api_html.headers.get("content-type", ""))

    # --- request limits and method handling -------------------------------
    parsed = urlparse(BASE)
    with socket.create_connection((parsed.hostname, parsed.port or 80),
                                  timeout=10) as raw:
        raw.sendall(
            b"POST /api/session/login HTTP/1.1\r\n"
            + f"Host: {parsed.hostname}:{parsed.port}\r\n".encode()
            + b"Content-Type: application/json\r\n"
              b"Content-Length: 70000000\r\n\r\n")
        status_line = raw.recv(256).split(b"\r\n", 1)[0]
    check("a declared body over 64 MB is refused before it is read",
          b" 413 " in status_line, status_line.decode(errors="replace"))
    check("reading a route with the wrong method is refused",
          admin.request("DELETE", "/api/compliance/summary",
                        headers=csrf(admin)).status_code == 405)
    form = admin.post("/api/session/login", content="username=admin&password=x",
                      headers={"content-type": "application/x-www-form-urlencoded"})
    check("a form body is not accepted as JSON",
          form.status_code in (415, 422), str(form.status_code))

    # --- enumeration and lockout -----------------------------------------
    unknown = anonymous.post("/api/session/login",
                             json={"username": PROBE_USER, "password": "wrong-password"})
    real = anonymous.post("/api/session/login",
                          json={"username": ADMIN[0], "password": "wrong-password"})
    check("unknown account and wrong password return the same 401 body",
          unknown.status_code == real.status_code == 401
          and unknown.json() == real.json(),
          f"{unknown.status_code} {unknown.json()} / {real.status_code} {real.json()}")

    created = admin.post("/api/identity/users", json={
        "username": LOCK_USER[0], "display_name": "Lockout Probe",
        "email": f"{LOCK_USER[0]}@example.com", "role": "viewer",
        "password": LOCK_USER[1],
    }, headers=csrf(admin))
    check("a throwaway account is created for the lockout check",
          created.status_code == 201, str(created.status_code))
    statuses = []
    for _ in range(5):
        response = anonymous.post("/api/session/login",
                                  json={"username": LOCK_USER[0],
                                        "password": "not-the-password"})
        statuses.append(response.status_code)
    locked = anonymous.post("/api/session/login",
                            json={"username": LOCK_USER[0], "password": LOCK_USER[1]})
    check("five failures lock the account even against the correct password",
          statuses == [401] * 5 and locked.status_code == 401,
          f"{statuses} then {locked.status_code}")

    # A second unknown-account pair trips the per-address/per-account throttle.
    throttle_user = f"throttle-probe-{os.getpid()}"
    statuses = []
    retry_after = None
    for _ in range(12):
        response = anonymous.post("/api/session/login",
                                  json={"username": throttle_user,
                                        "password": "wrong-password"})
        statuses.append(response.status_code)
        retry_after = retry_after or response.headers.get("retry-after")
    check("repeated failures are throttled with 429 and Retry-After",
          statuses[-1] == 429 and retry_after is not None,
          f"{statuses} retry-after={retry_after}")

    # --- session termination ---------------------------------------------
    admin.post("/api/session/logout", headers=csrf(admin))
    check("logout invalidates the session",
          admin.get("/api/compliance/summary").status_code in (401, 403))

    print(f"\n{passed} security checks passed, {len(failed)} failed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
