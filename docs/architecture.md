# Architecture

## 1. Shape of the system

PatchTrack is five small FastAPI services around one PostgreSQL database, with
an optional Grafana instance reading the same tables through a read-only role.

```
                     ┌──────────────────────────────┐
  browser ──HTTPS──▶ │ gateway             :8005    │  session, CSRF, static UI
                     └───────┬──────────────────────┘
                             │ trusted internal headers + bearer token
        ┌──────────────┬─────┴────────┬──────────────────┐
        ▼              ▼              ▼                  ▼
  ┌──────────┐  ┌────────────┐  ┌─────────────┐  ┌──────────────┐
  │ identity │  │ inventory  │  │ compliance  │  │  reporting   │
  │  :8001   │  │   :8002    │  │   :8003     │  │    :8004     │
  └────┬─────┘  └─────┬──────┘  └──────┬──────┘  └──────┬───────┘
       │              │                │                │
       └──────────────┴────────────────┴────────────────┘
                             │
                    ┌────────▼─────────┐        ┌────────────────────┐
                    │   PostgreSQL 13  │◀───────│ Grafana (optional) │
                    │  schema per svc  │  RO    │  :3000             │
                    └──────────────────┘        └────────────────────┘
                             ▲
                             │ pull (outbound only)
        ┌────────────────────┼────────────────────┬──────────────────┐
    AssetExplorer       Satellite              SCCM            Solaris inbox
    (inventory)         (errata)               (compliance)    (signed files)
```

Every arrow between the application and a source system points *outward*.
PatchTrack never listens for a management system to push into it, never opens
SSH, and never writes to a source.

## 2. Services and responsibilities

| Service | Port | Owns (writes) | Reads | Responsibility |
|---------|------|---------------|-------|----------------|
| identity | 8001 | `identity.*` | `shared.*` | Users, password hashing, sessions, refresh tokens, login throttling, audit queries |
| inventory | 8002 | `inventory.*` | `shared.*` | Asset ingestion and reconciliation, integration configuration, sync runs, Solaris inbox, organisation settings, placement vocabulary |
| compliance | 8003 | `compliance.*`, writes assessments into its own schema | `inventory.asset_effective`, `inventory.assets` (read) | Evaluation engine, policy versions, snapshots, findings, daily job |
| reporting | 8004 | `reporting.*` | `compliance.snapshots`, `inventory.*` (read) | PDF generation, schedules, email delivery, retention |
| gateway | 8005 | nothing | nothing | Session cookie, CSRF, security headers, request fan-out, static UI |

The gateway holds no business logic and no database connection. A compromise
of the gateway does not yield database credentials; a compromise of a backend
service does not yield the session cookie of a browser.

### Why the schema read-crossing is allowed

`compliance` and `reporting` read `inventory` tables directly rather than
calling the inventory API. This is a deliberate, bounded exception:

- reads only, and only of tables that are append/upsert-only from inventory;
- the alternative — an HTTP round trip per asset during evaluation — would make
  the daily job's runtime dependent on a second service's availability;
- the API remains the only *write* path across service boundaries.

The ownership rule that is enforced without exception: **a service writes only
its own schema.** `shared.audit_events` is written by all services and is
protected by a database trigger that rejects `UPDATE` and `DELETE`.

## 3. Request path

1. The browser reaches the gateway only.
2. `POST /api/session/login` forwards credentials to identity over the
   internal network. Identity verifies the Argon2id hash, applies lockout
   rules, writes an audit event and issues a short-lived JWT plus an opaque
   refresh token (stored as a SHA-256 digest).
3. The gateway stores the JWT in an `HttpOnly`, `SameSite=Strict` cookie
   (plus `Secure` on any non-loopback host) and issues a separate CSRF cookie.
4. On each API call the gateway verifies the cookie, then forwards the request
   to the owning service with **both** the bearer token and the trusted
   internal headers (`x-patchtrack-user`, `x-patchtrack-role`,
   `x-patchtrack-internal`). The internal header set is only honoured when the
   shared service token matches, compared in constant time.
5. The backend re-derives the principal and applies `require_permission(...)`.
   A backend never trusts the UI and never trusts a role claim it did not
   verify.

## 4. Collection and evaluation flow

```
schedule/manual ─▶ inventory.sync_runs (running)
                   │
                   ├─ AssetExplorer client ─▶ upsert inventory.assets
                   │        failure ⇒ run marked failed, no asset rows touched
                   │
                   └─ Satellite / SCCM / Solaris client
                            failure ⇒ run marked failed, NO assessment written
                            success ⇒ reconciliation → compliance.assessments
                                      → compliance.findings
                                      → compliance.asset_latest_assessment

compliance job ──▶ evaluate_all()  (pure function over inventory + assessments)
                   ├─ persist compliance.asset_compliance
                   └─ capture compliance.snapshots + snapshot_groups  (immutable)
```

The evaluator is a pure function of (asset rows, latest assessment, policy
definition, clock). That is what makes the same numbers appear in the API, the
dashboard and the PDF: they are all derived from the same evaluation, not three
independent queries.

## 5. Technology decisions

| Decision | Reason |
|----------|--------|
| FastAPI + Uvicorn | Small services, typed request models, dependency-injected authorization |
| SQLAlchemy Core with `text()` | No ORM session semantics across services; SQL stays explicit and reviewable |
| psycopg 3 | Native driver for PostgreSQL 13 shipped with RHEL/Rocky 9 |
| PostgreSQL, schema-per-service | One database to back up, one connection pool to tune, clear write ownership |
| SQL migration runner with checksums | Deterministic, idempotent, refuses to run a modified applied migration |
| JWT (HS256) + opaque refresh token | Access tokens are stateless across services; revocation happens on the refresh token |
| Argon2id | Current password-hashing recommendation; parameters recorded in code |
| ReportLab | Deterministic PDF generation with no browser or headless service dependency |
| Vanilla ES modules for the UI | No build step, no supply chain in the browser, CSP can forbid inline scripts |
| Grafana provisioned as code | The dashboard is reviewed in git, not clicked together in an instance |
| Grafana reads via a dedicated DB role | Grafana cannot alter data even if it is compromised |

## 6. Failure behaviour

| Failure | Behaviour |
|---------|-----------|
| AssetExplorer unreachable | Sync run marked failed; assets keep their last known state; the dashboard shows the inventory sync as stale |
| Satellite/SCCM unreachable | Sync run marked failed; **no** assessment written; affected servers become Stale once the freshness threshold passes, then Unknown only if there is no assessment at all |
| Grafana down | No user-visible effect on the application, API or reports |
| SMTP down | Report stays `completed`; delivery attempt recorded `failed` |
| compliance service down | Gateway returns 503 for compliance routes; reports and inventory continue |
| Database down | Every service reports unhealthy on `/readyz`; no partial writes are possible |
| Duplicate scheduler instances | `reporting.job_executions` unique key makes the second instance a no-op |

## 7. Deployment topology

A single host, Docker Compose (or Podman Compose), with:

- `postgres` on a named volume;
- seven application containers, all non-root, read-only root filesystem,
  `no-new-privileges`, all capabilities dropped;
- a one-shot `migrate` container gating the rest;
- optional `grafana` under the `dashboards` profile.

A TLS-terminating reverse proxy sits in front of the gateway. The services
speak HTTP only on a loopback or private network; no service other than the
gateway is published.

See [install.md](install.md) and [offline-deployment.md](offline-deployment.md).

## 8. Deliberate limitations

- **No message broker.** Collection and report generation are synchronous
  within a request or a scheduler tick. This is appropriate for a population
  of tens of thousands of servers collected a few times a day; it would not be
  for continuous streaming.
- **Single writer per schema.** The design assumes one instance of each
  service. The idempotency keys in reporting make two schedulers safe, but
  horizontal scaling of the services is not a supported configuration.
- **No object storage.** Report files live on a volume; the database holds
  metadata and the SHA-256.
- **Staleness is computed at evaluation time, from the source's `assessed_at`.**
  A source that silently stops updating a record will show as Stale, but
  PatchTrack cannot detect a source that lies about its own timestamp.
