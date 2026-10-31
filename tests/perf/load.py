"""Performance and load check against a running PatchTrack stack.

Measures a mixed read workload, recorded-period reads and concurrent PDF
export, and prints p50/p95/p99 latency plus throughput. The stack must hold
enough seeded history for the recorded-period path to be meaningful.

    PATCHTRACK_URL=http://127.0.0.1:8005 PATCHTRACK_USER=admin \
    PATCHTRACK_PASSWORD=... LOAD_SECONDS=30 LOAD_WORKERS=8 \
    python tests/perf/load.py

Not part of the pytest suite: it needs a running stack and takes as long as
LOAD_SECONDS says.
"""

from __future__ import annotations

import asyncio
import io
import os
import time

import httpx
from pypdf import PdfReader

BASE = os.environ.get("PATCHTRACK_URL", "https://127.0.0.1:8005")
USER = os.environ.get("PATCHTRACK_USER", "admin")
PASSWORD = os.environ.get("PATCHTRACK_PASSWORD", "")
READ_SECONDS = int(os.environ.get("LOAD_SECONDS", "30"))
READ_WORKERS = int(os.environ.get("LOAD_WORKERS", "8"))
PDF_WORKERS = 4


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1))))
    return ordered[index]


async def login(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/session/login",
                                 json={"username": USER, "password": PASSWORD})
    response.raise_for_status()


async def read_worker(stop: float, results: list[tuple[str, float, bool]]) -> None:
    endpoints = ["/api/compliance/summary", "/api/compliance/servers?page=1&page_size=50",
                 "/api/compliance/findings?page=1&page_size=50",
                 "/api/compliance/trends?months=12", "/api/inventory/dimensions",
                 "/api/reporting/reports?limit=20"]
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as client:
        await login(client)
        index = 0
        while time.perf_counter() < stop:
            path = endpoints[index % len(endpoints)]
            index += 1
            started = time.perf_counter()
            try:
                response = await client.get(path)
                ok = response.status_code == 200
            except httpx.HTTPError:
                ok = False
            results.append((path, time.perf_counter() - started, ok))


async def pdf_worker(count: int, payload: dict, results: list[tuple[float, bool, int]]) -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=180) as client:
        await login(client)
        csrf = client.cookies.get("patchtrack_csrf", "")
        for _ in range(count):
            started = time.perf_counter()
            try:
                response = await client.post(
                    "/api/reporting/reports/dashboard-export", json=payload,
                    headers={"X-PatchTrack-CSRF": csrf})
                ok = response.status_code == 200 and response.content.startswith(b"%PDF-")
                pages = len(PdfReader(io.BytesIO(response.content)).pages) if ok else 0
            except Exception:  # noqa: BLE001
                ok, pages = False, 0
            results.append((time.perf_counter() - started, ok, pages))


async def main() -> int:
    if not PASSWORD:
        print("PATCHTRACK_PASSWORD is required")
        return 2
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as client:
        await login(client)
        summary = (await client.get("/api/compliance/summary")).json()
        periods = (await client.get("/api/compliance/snapshot-periods")).json()["periods"]
        # Warm the read path and every service before measuring.
        for path in ("/api/compliance/summary", "/api/compliance/trends?months=12",
                     "/api/compliance/servers?page=1&page_size=50",
                     "/api/compliance/findings?page=1&page_size=50",
                     "/api/inventory/dimensions", "/api/reporting/reports?limit=20"):
            await client.get(path)

    results: list[tuple[str, float, bool]] = []
    stop = time.perf_counter() + READ_SECONDS
    started = time.perf_counter()
    await asyncio.gather(*(read_worker(stop, results)
                           for _ in range(READ_WORKERS)))
    elapsed = time.perf_counter() - started
    good = [seconds for _, seconds, ok in results if ok]
    errors = sum(1 for _, _, ok in results if not ok)
    print(f"READ MIX: {len(results)} requests in {elapsed:.1f}s "
          f"({len(results)/elapsed:.1f} req/s), {errors} errors")
    for path in sorted({p for p, _, _ in results}):
        values = [s for p, s, ok in results if p == path and ok]
        if values:
            print(f"  {path:<58} n={len(values):<4} "
                  f"p50={percentile(values, 50)*1000:7.1f}ms "
                  f"p95={percentile(values, 95)*1000:7.1f}ms "
                  f"max={max(values)*1000:7.1f}ms")
    print(f"  overall p50={percentile(good, 50)*1000:.1f}ms "
          f"p95={percentile(good, 95)*1000:.1f}ms "
          f"p99={percentile(good, 99)*1000:.1f}ms")
    read_ok = errors == 0 and percentile(good, 95) < 2.0

    # Recorded-period reads are heavier: the summary is rebuilt from the
    # snapshot ledger rather than today's assessment.
    period_results: list[float] = []
    period_errors = 0
    chosen = periods[len(periods) // 2]["key"]

    async def period_worker() -> None:
        nonlocal period_errors
        async with httpx.AsyncClient(base_url=BASE, timeout=60) as client:
            await login(client)
            for _ in range(10):
                t0 = time.perf_counter()
                response = await client.get("/api/compliance/summary",
                                            params={"period": chosen})
                period_results.append(time.perf_counter() - t0)
                if response.status_code != 200:
                    period_errors += 1

    await asyncio.gather(*(period_worker() for _ in range(4)))
    print(f"PERIOD READS ({chosen}): n={len(period_results)} "
          f"p95={percentile(period_results,95)*1000:.1f}ms "
          f"max={max(period_results)*1000:.1f}ms errors={period_errors}")
    period_ok = period_errors == 0 and percentile(period_results, 95) < 3.0

    payload = {
        "mode": "live", "scope_label": "Load test",
        "environment": "", "kpis": summary["kpis"],
        "remediation": summary.get("dashboard", {}).get("remediation"),
        "management": summary.get("management"),
        "freshness": {"total_sources": 5, "stale_sources": 0, "oldest_age_hours": 1},
        "by_environment": summary.get("by_environment") or [],
        "by_business_group": summary.get("by_business_group") or [],
        "by_os": summary.get("by_os") or [],
        "severity": summary.get("severity") or [], "trend": [], "servers": [],
    }
    pdf_results: list[tuple[float, bool, int]] = []
    pdf_started = time.perf_counter()
    await asyncio.gather(*(pdf_worker(5, payload, pdf_results)
                           for _ in range(PDF_WORKERS)))
    pdf_elapsed = time.perf_counter() - pdf_started
    times = [t for t, ok, _ in pdf_results if ok]
    pages = {p for _, ok, p in pdf_results if ok}
    pdf_errors = sum(1 for _, ok, _ in pdf_results if not ok)
    print(f"PDF EXPORT: {len(pdf_results)} documents in {pdf_elapsed:.1f}s "
          f"({len(pdf_results)/pdf_elapsed:.2f}/s), {pdf_errors} errors, "
          f"pages={pages}")
    if times:
        print(f"  p50={percentile(times,50):.2f}s p95={percentile(times,95):.2f}s "
              f"max={max(times):.2f}s")
    pdf_ok = pdf_errors == 0 and pages == {1} and percentile(times, 95) < 10.0

    print("RESULT:", "PASS" if read_ok and period_ok and pdf_ok else "FAIL")
    return 0 if read_ok and period_ok and pdf_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
