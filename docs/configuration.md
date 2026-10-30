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
| `PATCHTRACK_VERSION` | `3.0.4` | Image tag used by compose |

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

### Maintenance run logs

These are read by `install.sh`, `upgrade.sh` and `uninstall.sh`, not by the
application services, so they belong in the operator's environment rather than
in `.env`:

| Variable | Default | Purpose |
|----------|---------|---------|
| `PATCHTRACK_LOG_DIR` | `/var/log/patchtrack` | Directory for installation, upgrade and uninstallation run logs. A path that cannot be written falls back to `${XDG_STATE_HOME:-$HOME/.local/state}/patchtrack/logs`. |
| `PATCHTRACK_LOG_RETENTION_DAYS` | `90` | Age after which timestamped `<tool>-*.log` files are removed. A non-numeric value uses the default. |

See [logging.md](logging.md#installer-upgrade-and-uninstaller-logs) for the
file names, permissions and contents.

## 2. Database

| Variable | Default | Notes |
|----------|---------|-------|
| `PATCHTRACK_DATABASE_URL` | **required** | `postgresql+psycopg://user:password@host:5432/patchtrack?sslmode=prefer` for the bundled database; `sslmode=require` or `verify-full` for an external one |
| `PATCHTRACK_DB_POOL_SIZE` | 5 | Per service |
| `PATCHTRACK_DB_POOL_MAX_OVERFLOW` | 5 | Burst above the pool |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | — | Used by the bundled postgres container only |
| `POSTGRES_IMAGE` | `docker.io/library/postgres:17` | Override for an air-gapped mirror, or for Red Hat's `registry.redhat.io/rhel9/postgresql-17` (needs a subscription and a terms acceptance) |
| `POSTGRES_DATA_DIR` | `/var/lib/postgresql/data` | Data directory inside the database image; Red Hat's image uses `/var/lib/pgsql/data` |
| `PROMETHEUS_IMAGE` | `docker.io/prom/prometheus:v3.15.1` | Scraper image for the optional `monitoring` profile |
| `PATCHTRACK_PROMETHEUS_PORT` | `9090` | Loopback port for the bundled Prometheus |

The services refuse to start without `PATCHTRACK_DATABASE_URL`. The bundled
`postgres` image does not enable server TLS by default, and it publishes no
host port: the database is reachable only over the private Compose bridge, so
the bundled URL uses `sslmode=prefer`. That negotiates TLS if the image is
later configured to offer it, and otherwise uses the isolated bridge. An
external PostgreSQL service must use `sslmode=require` at minimum and
`verify-full` where the server certificate is verifiable. Never `disable`.

**Sizing.** Five services × (pool 5 + overflow 5) = up to 50 connections.
PostgreSQL's default `max_connections` is 100, which leaves headroom for psql
and maintenance. If you run several instances of anything, raise
`max_connections` or lower the pools.

## 3. Secrets

| Variable | Notes |
|----------|-------|
| `PATCHTRACK_JWT_SECRET` | Required, ≥ 32 characters. Rotating invalidates access tokens only. |
| `PATCHTRACK_SERVICE_TOKEN` | Required in production. Empty disables trusted-header auth (fails closed). |
| `PATCHTRACK_SECRETS_KEY` | Optional. Encrypts credentials saved through the UI (currently the SMTP password); falls back to `PATCHTRACK_JWT_SECRET`. Set a distinct value so rotating the JWT secret does not invalidate the saved SMTP password. |

Generate with `openssl rand -hex 32`. Both must be identical across all five
services.

### Secret files

Any of the values above, plus the SMTP environment fallback, can be supplied
from a file instead of the environment. Set the `_FILE` sibling and the file's
content becomes the value; the trailing newline is removed. A file value wins
over the plain variable, and explicit constructor arguments still win over
both.

| Plain variable | File variable |
|----------------|---------------|
| `PATCHTRACK_DATABASE_URL` | `PATCHTRACK_DATABASE_URL_FILE` |
| `PATCHTRACK_JWT_SECRET` | `PATCHTRACK_JWT_SECRET_FILE` |
| `PATCHTRACK_SECRETS_KEY` | `PATCHTRACK_SECRETS_KEY_FILE` |
| `PATCHTRACK_SERVICE_TOKEN` | `PATCHTRACK_SERVICE_TOKEN_FILE` |
| `PATCHTRACK_SMTP_PASSWORD` | `PATCHTRACK_SMTP_PASSWORD_FILE` |

The database URL file must contain the complete SQLAlchemy URL, including the
password. A missing, unreadable, non-UTF-8 or oversized file stops the service
at start-up rather than falling back to the plain variable; an empty
`*_FILE` variable means "not configured". Files readable by group or other
users are reported in the log without their content.

For Compose, the opt-in `docker-compose.secrets.yml` mounts
`/etc/patchtrack/secrets/{database_url,jwt_secret,service_token,postgres_password}`
as secrets and masks the plain variables. For systemd, use
`LoadCredential=` or `LoadCredentialEncrypted=` and point the `_FILE`
variables at `%d/<name>`; see
[security.md](security.md#secret-files).

## 4. Reporting

| Variable | Default | Notes |
|----------|---------|-------|
| `PATCHTRACK_REPORT_DIR` | `/var/lib/patchtrack/reports` | Must be the mounted volume path in containers |
| `PATCHTRACK_REPORT_RETENTION_DAYS` | 2555 | File retention; metadata is kept |
| `PATCHTRACK_BACKUP_DIR` | `/var/lib/patchtrack/backups` | Where the backup worker writes data archives; the reporting service reads the same mount read-only to serve downloads |
| `PATCHTRACK_BACKUP_INTERVAL` | `86400` (24 h) | Initial cadence of the `reporting.backup` job; the stored row wins after the first run |
| `PATCHTRACK_BACKUP_RETENTION` | `7` | Initial archive count; live value is on the Data backups screen (1–90) |
| `PATCHTRACK_SMTP_HOST` | empty | Fallback only: empty disables delivery until a relay is saved in Settings; reports still generate |
| `PATCHTRACK_SMTP_PORT` | 587 | Fallback only |
| `PATCHTRACK_SMTP_USERNAME` | empty | Fallback only |
| `PATCHTRACK_SMTP_PASSWORD` | empty | Fallback only; a password saved in Settings is encrypted at rest |
| `PATCHTRACK_SMTP_STARTTLS` | `true` | Fallback only. Do not turn off; install the relay CA instead |
| `PATCHTRACK_SMTP_SENDER` | `patchtrack@localhost` | Fallback only. Must be accepted by your relay |

The SMTP relay is normally configured at **Administration → Settings → Email
delivery**. The saved row in `shared.app_settings` (`smtp_relay`) is
authoritative over the environment and applies to the next delivery without a
restart. The password is write-only: it is encrypted with
`PATCHTRACK_SECRETS_KEY` (or the JWT secret when that is unset) and no endpoint
returns it. Changing either key makes the stored password unreadable; the
Settings screen reports that and asks for the password again.

### Dynamic settings (database, no restart)

These are edited in the UI (**Settings**) by an administrator and read by the
services from `shared.app_settings`, so a change takes effect without
restarting anything.

| Setting | Default | Notes |
|---------|---------|-------|
| Organisation name | empty | PDF report branding |
| Environment and business-service lists | empty (free text) | Placement editor vocabulary and display names |
| Audit trail retention | 2555 days | Events older than the window are deleted by the daily job; range 30–36,500 days |
| Report file retention | 2555 days | Files older than the window are deleted by the daily report-retention job |
| SMTP relay | environment values | Host, port, STARTTLS, username, password (encrypted) and sender used for scheduled report email |
| System timezone | UTC | Calendar day used by schedules, snapshots, period boundaries and report timestamps |
| Data backup retention | `PATCHTRACK_BACKUP_RETENTION` (7) | Completed archives kept on the backups volume; range 1–90. The worker applies a new value after the next successful archive |

Audit retention is applied by `POST /jobs/audit-purge` on the identity service,
which the in-app scheduler runs once a day (`PATCHTRACK_AUDIT_PURGE_INTERVAL`).
The trail is otherwise append-only: the API can set the window, but there is no
route that edits or deletes an individual event. Changing the window is itself
audited.

Data archives are written by the `backup` container, not by the reporting
service. That container runs the same PostgreSQL image as the database, so
`pg_dump` always matches the server major version, and it mounts the `backups`
and `reports` volumes. It reads its configuration from the same `.env`; the
only extra variables it understands are `PATCHTRACK_BACKUP_POLL_SECONDS`
(default 10) and `PATCHTRACK_BACKUP_ONCE` (used by tests and by an operator who
wants one pass before starting the loop). `PATCHTRACK_VERSION` is recorded in
each manifest; Compose passes it from `.env`. A manually assembled deployment
that omits it records `unknown`, and the Data backups screen falls back to the
worker's heartbeat value. See
[backup-restore.md](backup-restore.md#in-app-data-archive).

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
| `PATCHTRACK_REPORT_TICK_INTERVAL` | `60` (1 min) | Reporting schedules |
| `PATCHTRACK_REPORT_PURGE_INTERVAL` | `86400` (24 h) | Report-file retention sweep |

Due times are stored in `shared.scheduled_jobs`, so a restart does not replay
work that just ran. A failed run is retried after
`PATCHTRACK_SCHEDULER_RETRY_SECONDS`, not after the full interval. Each job
holds a PostgreSQL advisory lock while it runs, so replicas do not duplicate
work.

These values are **initial defaults**. Administrators change a job's interval
and enabled flag at **Reporting → Schedules → System jobs**; the stored row is
then authoritative and a restart does not reset it. Editing the environment
variable affects only a job whose row has not yet been created.

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
| `PATCHTRACK_FORWARDED_IPS` | `127.0.0.1` | Comma-separated proxies allowed to set `X-Forwarded-*`. **Never `*`.** |
| `PATCHTRACK_INTERNAL_FORWARDED_IPS` | `172.16.0.0/12` | Compose only. The backend services' `PATCHTRACK_FORWARDED_IPS`: the compose network that the gateway reaches them from. Without it, audit records show the gateway container's address instead of the operator's. **Never `*`.** |
| `PATCHTRACK_IDENTITY_URL` … `PATCHTRACK_REPORTING_URL` | loopback ports | Internal service URLs used by the gateway |

## 8. TLS termination

New installations ship with `PATCHTRACK_TLS_ENABLED=true`, so the **gateway
terminates TLS itself** and serves HTTPS on its published port. Backend
services still speak HTTP on the private network; only the gateway is
published. An upgraded deployment whose existing `.env` predates the setting
stays on plain HTTP until it adds the variable; a reverse proxy deployment
sets it to `false` deliberately.

| Variable | Default | Notes |
|----------|---------|-------|
| `PATCHTRACK_TLS_ENABLED` | `true` in `.env.example`; `false` when unset | Gateway serves HTTPS. Set `false` only when a reverse proxy terminates TLS; set it explicitly in an upgraded deployment's `.env` (new installation files ship `true`) |
| `PATCHTRACK_TLS_DIR` | `/var/lib/patchtrack/tls` | Directory holding the certificate and key; mount it as a volume |
| `PATCHTRACK_TLS_CERT_FILE` | `server.crt` | Relative names resolve under `PATCHTRACK_TLS_DIR` |
| `PATCHTRACK_TLS_KEY_FILE` | `server.key` | Relative names resolve under `PATCHTRACK_TLS_DIR`; must not be readable by others |
| `PATCHTRACK_TLS_HOSTNAME` | host name | DNS name placed in the generated certificate |

### First start

On the first start with TLS enabled and no material present, the gateway
generates a self-signed certificate (RSA 2048, SHA-256, 825 days, valid for
`PATCHTRACK_TLS_HOSTNAME`, `localhost`, `127.0.0.1` and `::1`) and writes the
key as mode 0600. A new installation therefore never serves the sign-in page
over plain HTTP. The browser warns that the certificate is not trusted until
the organisation's own certificate is installed.

The generated certificate is **not** replaced on later starts. If only one of
the two files exists, the gateway refuses to start rather than overwrite the
remaining half; restore the pair or empty the directory deliberately.

### Install your own certificate

**Administration → Settings → HTTPS certificate** accepts a PEM certificate
(chain included if applicable) and its unencrypted PEM private key (PKCS#8 or
traditional). Before writing anything the gateway checks that the certificate
parses, is currently valid, and matches the key; a passphrase-protected key is
refused. Both files are replaced atomically, the key is written mode 0600, and
the new certificate is loaded into the live listener — new connections use it
without a restart. The public metadata (subject, issuer, validity, SHA-256) is
shown in Settings and written to the `settings.tls_updated` audit event; the
key is never returned or logged.

TLS 1.2 is the floor. HSTS is sent by the gateway whenever a request arrives
over HTTPS. No TLS material is stored in the database.

The same files can be replaced out of band in `PATCHTRACK_TLS_DIR`; restart the
gateway afterwards.

### External reverse proxy

Set `PATCHTRACK_TLS_ENABLED=false` when a reverse proxy terminates TLS. The
gateway then speaks HTTP on its port as before, and the HTTPS tab reports that
certificates are managed externally. Minimal nginx example:

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

## 9. Policy (not environment)

Compliance policy is data, not configuration. It lives in
`compliance.policy_versions` and is managed through
**Administration → Compliance policy**. See
[compliance-methodology.md](compliance-methodology.md#5-policy).

Keep the policy definition in version control alongside the deployment (a copy,
not the source of truth), so a review can show what changed between quarters.

## 10. Settings that are not configurable, on purpose

| Behaviour | Fixed because |
|-----------|---------------|
| Unknown and stale are in the denominator | Allowing their exclusion would let an outage report as 100% |
| Report methodology section | A report without it is not defensible |
| Audit events are append-only | A writable audit trail is not an audit trail |
| TLS verification defaults to on | Requirements §C-4 |
| Integrations start disabled | Requirements §C-1, §C-3 |
| No bootstrap password | Requirements §C-1 |

## 11. Changing configuration

Most changes need a restart of the affected service:

```bash
sudo -u patchtrack docker compose up -d --force-recreate identity inventory compliance reporting gateway
```

Changes to integration settings, policy and schedules take effect without a
restart — they are database rows read on each request or each job.

If you change `PATCHTRACK_JWT_SECRET`, every user must sign in again. If you
change `PATCHTRACK_SERVICE_TOKEN`, restart **all** services at once; a mixed
state returns 401 from the backends until they agree.
