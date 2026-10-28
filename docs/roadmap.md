# Implementation roadmap

What has been built, in what order, what remains before production, and what
belongs to a later release.

## Phase 1 — Foundation (complete)

| Item | Verdict |
|------|---------|
| Repository structure, service skeletons, shared configuration | Done |
| PostgreSQL schema, eight migrations, checksummed migration runner | Done |
| Identity: users, roles, Argon2id, lockout, refresh tokens, audit | Done |
| Gateway: session cookie, CSRF, origin check, security headers, proxy | Done |
| SPA shell and route structure | Done |

Exit criterion was: sign in on a fresh database and reach every route. Met.

## Phase 2 — Data and evaluation (complete)

| Item | Verdict |
|------|---------|
| AssetExplorer connector with configurable field map | Done; live field names unverified |
| Satellite errata connector | Done; live endpoint unverified |
| SCCM connector | Done; live endpoint unverified |
| Solaris inbox ingestion | Done |
| Reconciliation with conflict recording, no guessing | Done; integration-tested |
| Compliance engine, five states, policy versions | Done; unit-tested |
| Daily and on-demand snapshots | Done |

## Phase 3 — Reporting and dashboard (complete)

| Item | Verdict |
|------|---------|
| PDF renderer with validation and SHA-256 | Done; layout not asserted |
| Schedules, idempotent execution, delivery isolation | Done; SMTP untested |
| Retention purge | Done |
| Executive dashboard (vanilla SPA) | Done; smoke-tested in a browser |
| Provisioned Grafana dashboard and read-only role | Done; role script awaits operator execution |
| Numerical agreement between API, dashboard and PDF | Verified |

## Phase 4 — Hardening and delivery (complete)

| Item | Verdict |
|------|---------|
| Container image (UBI9, non-root, read-only) | Done |
| Compose topology with SELinux-appropriate mounts | Done; run end to end under Podman 5.8.2 with SELinux enforcing (Docker Engine itself not exercised) |
| `.env.example` with no working defaults | Done |
| Test suite: 207 unit + 24 integration | Done |
| Documentation set (this directory) | Done |

## Phase 5 — Production readiness (remaining, needs the target environment)

| # | Item | Owner | Blocking? |
|---|------|-------|-----------|
| 1 | Verify each connector against a live source system; adjust `extra.field_map` | Deployment engineer | Yes |
| 2 | Build and run the Compose stack with **Docker Engine** on RHEL 9.8 / Rocky 9.8 with SELinux enforcing (the same stack runs under Podman on this host) | Deployment engineer | Yes |
| 3 | Run `deploy/postgres/roles/010_grafana_readonly.sql` on the target to create `patchtrack_grafana` with its own generated password (the grants are exercised on the build host; the password is per-deployment) | Deployment engineer | Only for the dashboard |
| 4 | Run the four native systemd units on the target host (written and syntax-verified, never started here) | Deployment engineer | Only for native install |
| 5 | Configure SMTP and test a delivery plus a forced failure | Operations | No |
| 6 | Wire scheduled jobs and monitoring | Operations | Yes |
| 7 | Take a backup, restore it to a scratch host, and verify the digests | Operations | Yes |
| 8 | Run the first full month, and compare the first report against the manual process | Compliance | Yes |
| 9 | Measure collection run times at production scale; tune timeouts and page sizes | Operations | No |
| 10 | Review a generated PDF visually | Compliance | Yes |

## Phase 6 — Next release candidates

Ordered by value, not committed:

1. **OIDC / SAML sign-in.** The schema supports `auth_source='oidc'`; the code
   path refuses such users with a clear message today. This is the largest
   single gap for enterprise adoption.
2. **MFA** for administrator accounts.
3. **Per-business-group scoping** so a Viewer sees only their estate.
4. **Prometheus metrics endpoint** per service, replacing the query-based
   monitoring described in the runbook.
5. **Scheduled collection inside the application.** Today the schedule lives in
   the systemd timer. An in-app scheduler (gated by the same idempotency ledger
   as reports) would remove an external dependency, at the cost of a second
   scheduler to reason about.
6. **Framework mapping** (PCI DSS, APRA CPS 234, ISO 27001 control references
   against each finding category).
7. **Asset-level change history** in the UI (the alias and conflict tables hold
   the data already).
8. **Report templates per business unit** (branding and recipient routing).
9. **Windows agent-free WMI collection** — only if the organisation prohibits an
   SCCM read view; it would still be read-only.
10. **Retention policy UI** for report files and audit exports.

## Deliberately not on the roadmap

- Patch deployment, reboot orchestration, or anything that changes a managed
  host.
- Storing source-system credentials in the database, under any encryption
  scheme. References to a secret store are the boundary.
- Auto-reconciling conflicts by "best guess" scoring. Ambiguity is surfaced to
  a human; that is the product's promise.
- Recomputing an archived period's figures after the fact. Snapshots are
  records.
