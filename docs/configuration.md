# Configuration

All application settings are environment variables with the `PATCHTRACK_`
prefix, read by `pydantic-settings` from the process environment or `.env`.
Integration credentials are **not** environment-only: they are references
stored in the database and resolved at collection time.

The authoritative template is [`.env.example`](../.env.example). Copy it, do
not edit it.

## 1. Application

| Variable | Default | Purpose |
|----------|---------|---------|
| `PATCHTRACK_ENVIRONMENT` | `development` | Free text; `production` in real deployments. Do not rely on it for security — but it does gate one dangerous setting (below). |
| `PATCHTRACK_SESSION_COOKIE_SECURE` | `auto` | `auto` marks the session and CSRF cookies `Secure` for every host other than loopback, so an unencrypted deployment cannot hold a session. `always` forces it even on loopback. `never` is a **development-only** escape hatch for browsing by IP over plain HTTP; the service refuses to start with `never` when `PATCHTRACK_ENVIRONMENT=production`. |
| `PATCHTRACK_APP_TIMEZONE` | — | **Removed.** The deployment timezone is a database setting, changed in **Settings → Platform**, so every service picks it up without a restart. See [Time zone](#time-zone) below. |
| `PATCHTRACK_LOG_LEVEL` | `INFO` | Uvicorn log level |
| `PATCHTRACK_MAX_REQUEST_BYTES` | `67108864` (64 MiB) | Largest accepted request body. Justification evidence is submitted base64-encoded, so keep this comfortably above the combined attachment size. A larger body is refused with `413` before parsing. |
| `PATCHTRACK_VERSION` | `3.0.0` | Image tag used by compose |

### Time zone

`shared.app_settings.system_timezone` holds one IANA zone for the whole
installation. It is seeded to `UTC` by migration `014_system_timezone.sql` and
changed only through **Settings → Platform** (`PUT /api/identity/settings/timezone`),
which requires `settings:manage` and writes an audit event.

The setting decides:

- the local calendar day a daily snapshot covers;
- the first and last instant of a monthly or quarterly reporting period;
- the default zone for newly created schedules, and the zone a schedule runs
  in when it has none of its own;
- how timestamps are displayed in the console and printed on generated PDFs.

Instants remain stored in UTC. Changing the zone re-labels calendar
boundaries from the next period onward; it does not rewrite existing
snapshots or reports.

## 2. Database

| Variable | Default | Notes |
|----------|---------|-------|
| `PATCHTRACK_DATABASE_URL` | **required** | `postgresql+psycopg://user:password@host:5432/patchtrack?sslmode=require` |
| `PATCHTRACK_DB_POOL_SIZE` | 5 | Per service |
| `PATCHTRACK_DB_POOL_MAX_OVERFLOW` | 5 | Burst above the pool |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | — | Used by the bundled postgres container only |
| `POSTGRES_IMAGE` | `docker.io/library/postgres:17` | Override for an air-gapped mirror, or for Red Hat's `registry.redhat.io/rhel9/postgresql-17` (needs a subscription and a terms acceptance) |
| `POSTGRES_DATA_DIR` | `/var/lib/postgresql/data` | Data directory inside the database image; Red Hat's image uses `/var/lib/pgsql/data` |
| `PROMETHEUS_IMAGE` | `docker.io/prom/prometheus:v3.15.1` | Scraper image for the optional `monitoring` profile |
| `PATCHTRACK_PROMETHEUS_PORT` | `9090` | Loopback port for the bundled Prometheus |

The services refuse to start without `PATCHTRACK_DATABASE_URL`. Use
`sslmode=require` at minimum and `verify-full` where the server certificate is
verifiable. Never `disable`.

**Sizing.** Five services × (pool 5 + overflow 5) = up to 50 connections.
PostgreSQL's default `max_connections` is 100, which leaves headroom for
Grafana, psql and maintenance. If you run several instances of anything, raise
`max_connections` or lower the pools.

## 3. Secrets

| Variable | Notes |
|----------|-------|
| `PATCHTRACK_JWT_SECRET` | Required, ≥ 32 characters. Rotating invalidates access tokens only. |
| `PATCHTRACK_SERVICE_TOKEN` | Required in production. Empty disables trusted-header auth (fails closed). |

Generate with `openssl rand -hex 32`. Both must be identical across all five
services.

## 4. Reporting

| Variable | Default | Notes |
|----------|---------|-------|
| `PATCHTRACK_REPORT_DIR` | `/var/lib/patchtrack/reports` | Must be the mounted volume path in containers |
| `PATCHTRACK_REPORT_RETENTION_DAYS` | 2555 | File retention; metadata is kept |
| `PATCHTRACK_SMTP_HOST` | empty | Empty disables delivery; reports still generate |
| `PATCHTRACK_SMTP_PORT` | 587 | |
| `PATCHTRACK_SMTP_USERNAME` | empty | |
| `PATCHTRACK_SMTP_PASSWORD` | empty | Environment only, never the database |
| `PATCHTRACK_SMTP_STARTTLS` | `true` | Do not turn off; install the relay CA instead |
| `PATCHTRACK_SMTP_SENDER` | `patchtrack@localhost` | Must be accepted by your relay |

### Dynamic settings (database, no restart)

These are edited in the UI (**Settings**) by an administrator and read by the
services from `shared.app_settings`, so a change takes effect without
restarting anything.

| Setting | Default | Notes |
|---------|---------|-------|
| Organisation name | empty | PDF report branding |
| Environment and business-service lists | empty (free text) | Placement editor vocabulary and display names |
| Audit trail retention | 2555 days | Events older than the window are deleted by the daily job; range 30–36,500 days |

Audit retention is applied by `POST /jobs/audit-purge` on the identity service,
which the in-app scheduler runs once a day (`PATCHTRACK_AUDIT_PURGE_INTERVAL`).
The trail is otherwise append-only: the API can set the window, but there is no
route that edits or deletes an individual event. Changing the window is itself
audited.

## 5. Scheduler

Scheduled work runs inside each service; there is no external timer or cron
entry. These settings are read at service start, so changing one requires
restarting the service that owns the job.

| Variable | Default | Notes |
|----------|---------|-------|
| `PATCHTRACK_SCHEDULER_ENABLED` | `true` | `false` pauses all scheduled work while leaving the HTTP job endpoints usable |
| `PATCHTRACK_SCHEDULER_POLL_SECONDS` | `15` | How often each service checks for due work |
| `PATCHTRACK_SCHEDULER_RETRY_SECONDS` | `300` | Retry delay after a failed run |
| `PATCHTRACK_SCHEDULER_STARTUP_DELAY_SECONDS` | `10` | Grace period before the first pass |
| `PATCHTRACK_SYNC_ASSETEXPLORER_INTERVAL` | `21600` (6 h) | Inventory |
| `PATCHTRACK_SYNC_SATELLITE_INTERVAL` | `21600` (6 h) | Inventory |
| `PATCHTRACK_SYNC_SCCM_INTERVAL` | `86400` (24 h) | Inventory |
| `PATCHTRACK_SYNC_SOLARIS_INTERVAL` | `604800` (7 d) | Inventory |
| `PATCHTRACK_DAILY_SNAPSHOT_INTERVAL` | `86400` (24 h) | Compliance snapshot and refresh |
| `PATCHTRACK_AUDIT_PURGE_INTERVAL` | `86400` (24 h) | Identity audit-retention purge |
| `PATCHTRACK_REPORT_TICK_INTERVAL` | `60` (1 min) | Reporting schedules and report retention |

Due times are stored in `shared.scheduled_jobs`, so a restart does not replay
work that just ran. A failed run is retried after
`PATCHTRACK_SCHEDULER_RETRY_SECONDS`, not after the full interval. Each job
holds a PostgreSQL advisory lock while it runs, so replicas do not duplicate
work.

## 6. Collection

| Variable | Default | Notes |
|----------|---------|-------|
| `PATCHTRACK_SOLARIS_INBOX` | `/var/lib/patchtrack/solaris-inbox` | Must match the mounted volume |
| `PATCHTRACK_ASSETEXPLORER_BASE_URL` | empty | Informational; the integration row is authoritative |
| `PATCHTRACK_SATELLITE_BASE_URL` | empty | Informational |
| `PATCHTRACK_SCCM_BASE_URL` | empty | Informational |

Integration endpoints, timeouts, page sizes, TLS verification and the
credential **reference** live in `inventory.integrations` and are edited
through the UI/API. That is deliberate: changing a collection endpoint should
not require a restart, and a credential must never be in a file the app can be
tricked into printing.

### Credential references

```http
PATCH /api/inventory/integrations/satellite
{ "secret_ref": "file:/etc/patchtrack/secrets/satellite.password" }
```

- `env:NAME` → `os.environ["NAME"]`
- `file:/path` → first line of the file, stripped
- `""` → no credential

A bare value (anything not starting with `env:` or `file:`) is rejected with a
422 and an explanation. This is what stops a password ending up in the
database, the audit trail and a backup.

Mount credential files into the container; do not put them in `.env` if you can
avoid it, because compose reads `.env` for interpolation and it is easy to leak
into logs.

## 7. Network and proxy

| Variable | Default | Notes |
|----------|---------|-------|
| `PATCHTRACK_BIND_ADDRESS` | `127.0.0.1` | Published port bind address; keep on loopback behind a proxy |
| `PATCHTRACK_GATEWAY_PORT` | 8005 | |
| `PATCHTRACK_GRAFANA_PORT` | 3000 | |
| `PATCHTRACK_FORWARDED_IPS` | `127.0.0.1` | Comma-separated proxies allowed to set `X-Forwarded-*`. **Never `*`.** |
| `PATCHTRACK_INTERNAL_FORWARDED_IPS` | `172.16.0.0/12` | Compose only. The backend services' `PATCHTRACK_FORWARDED_IPS`: the compose network that the gateway reaches them from. Without it, audit records show the gateway container's address instead of the operator's. **Never `*`.** |
| `PATCHTRACK_IDENTITY_URL` … `PATCHTRACK_REPORTING_URL` | loopback ports | Internal service URLs used by the gateway |

## 8. TLS termination

The application speaks HTTP internally and expects a TLS proxy in front of the
gateway. Minimal nginx example:

```nginx
server {
  listen 443 ssl;
  server_name patchtrack.example.com;

  ssl_certificate     /etc/pki/tls/certs/patchtrack.crt;
  ssl_certificate_key /etc/pki/tls/private/patchtrack.key;
  ssl_protocols TLSv1.2 TLSv1.3;

  add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

  # Match PATCHTRACK_FORWARDED_IPS so uvicorn trusts only this proxy.
  proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto $scheme;
  proxy_set_header Host              $http_host;

  location / {
    proxy_pass http://127.0.0.1:8005;
    proxy_read_timeout 300s;      # long enough for a report download
    client_max_body_size 20m;     # Solaris ingest payloads
  }
}

server {
  listen 80;
  server_name patchtrack.example.com;
  return 301 https://$host$request_uri;
}
```

Set `PATCHTRACK_FORWARDED_IPS` to the proxy's source address as seen by the
gateway. If the proxy is on the same host, that is `127.0.0.1`.

The gateway is the only origin of `X-Forwarded-For` as far as the backend
services are concerned. Each backend accepts the header only from the compose
network address range (`PATCHTRACK_INTERNAL_FORWARDED_IPS`), and none of the
four publishes a port, so a client cannot reach them to spoof it. No service
parses the raw header itself: uvicorn rewrites the peer address for trusted
sources only, and the application records an address it cannot parse as NULL
rather than rejecting the request.

## 9. Grafana

| Variable | Notes |
|----------|-------|
| `GF_ADMIN_PASSWORD` | Generated at install |
| `GF_SERVER_ROOT_URL` | External URL shown in links |
| `PATCHTRACK_GF_DB_HOST/PORT/NAME` | Point at `postgres:5432` |
| `PATCHTRACK_GF_DB_USER` | `patchtrack_grafana` |
| `PATCHTRACK_GF_DB_PASSWORD` | The password set when creating the role |
| `PATCHTRACK_GF_DB_SSLMODE` | `verify-full` in production |

The datasource YAML reads these with `${…}` interpolation at container start.
Do not edit the provisioned YAML in a running instance; provisioning overwrites
`allowUiUpdates: false` dashboards and `editable: false` datasources.

## 10. Policy (not environment)

Compliance policy is data, not configuration. It lives in
`compliance.policy_versions` and is managed through
**Administration → Compliance policy**. See
[compliance-methodology.md](compliance-methodology.md#5-policy).

Keep the policy definition in version control alongside the deployment (a copy,
not the source of truth), so a review can show what changed between quarters.

## 11. Settings that are not configurable, on purpose

| Behaviour | Fixed because |
|-----------|---------------|
| Unknown and stale are in the denominator | Allowing their exclusion would let an outage report as 100% |
| Report methodology section | A report without it is not defensible |
| Audit events are append-only | A writable audit trail is not an audit trail |
| TLS verification defaults to on | Requirements §C-4 |
| Integrations start disabled | Requirements §C-1, §C-3 |
| No bootstrap password | Requirements §C-1 |

## 12. Changing configuration

Most changes need a restart of the affected service:

```bash
sudo -u patchtrack docker compose up -d --force-recreate identity inventory compliance reporting gateway
```

Changes to integration settings, policy and schedules take effect without a
restart — they are database rows read on each request or each job.

If you change `PATCHTRACK_JWT_SECRET`, every user must sign in again. If you
change `PATCHTRACK_SERVICE_TOKEN`, restart **all** services at once; a mixed
state returns 401 from the backends until they agree.
