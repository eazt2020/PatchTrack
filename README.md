# PatchTrack

PatchTrack records and reports server patch-compliance status. It is a
read-only observer: it consumes inventory from ManagedEngine AssetExplorer and
patch assessments from Red Hat Satellite, Microsoft SCCM/ConfigMgr and an
approved Solaris file drop, evaluates them against configurable policy, and
presents the result on a dashboard, through an API, and in archivable PDF
reports.

It does **not** deploy patches, reboot servers, execute commands, or modify
Satellite, SCCM or AssetExplorer. It never claims a patch is installed without
evidence, and it never declares regulatory compliance on the organisation's
behalf.

Its own inventory mirror accepts two operator annotations — an exclusion from
scope and a local correction to environment or business service — both with a
mandatory reason, both audited, and neither written back to a source system or
overwritten by the next sync. The environment and business-service values an
operator can choose are a curated list maintained in Settings, so a correction
cannot introduce a second spelling of an existing value. Renaming a value there
propagates to every server holding it, without rewriting what the source system
reported and without the next sync undoing it.

## Capabilities

- **Compliance view** — dashboard KPIs, monthly/quarterly/yearly trends,
  recorded snapshots for past periods, server and remediation registers with
  search, filtering, sorting and CSV export, inventory overrides and
  verification evidence.
- **Reporting** — branded PDF reports on monthly or quarterly schedules.
  Scheduled reports can be emailed through an SMTP relay configured in
  Settings; the password is write-only and encrypted at rest.
- **Scheduling** — collection, daily snapshot, audit purge, report tick and
  report-retention jobs run inside the services and are listed at
  **Reporting → Schedules → System jobs**, where an administrator can change an
  interval or disable a job. No cron entry or systemd timer is involved.
- **Administration** — users and roles, policy versions, integrations,
  reconciliation, audit trail, report branding, retention windows, placement
  lists and the deployment timezone in the UI.
- **Installation** — `install.sh` guides a fresh deployment through its
  parameters, checks every dependency first, verifies HTTPS, migrations, all
  services and the first administrator afterwards, and automatically reverses
  its own changes if any step fails. It can route packages, images and service
  traffic through an HTTP(S) proxy when the host has no direct internet access.
  `uninstall.sh` prechecks the installed deployment, removes only what the
  installation created, preserves data volumes unless `--purge-data` is given,
  and verifies the result. `upgrade.sh` backs up the tree, configuration and
  database, stages a new release, applies migrations, verifies the stack and
  restores everything automatically if any step fails.
- **Transport and secrets** — HTTPS from first start with a generated
  certificate, replacement with the organisation's own certificate in
  Settings, and optional file-backed bootstrap secrets for Docker, Podman or
  systemd deployments.
- **Operations** — Grafana dashboards are optional; the application, API and
  reports work with Grafana stopped.

## What is in this repository

| Path | Contents |
|------|----------|
| `install.sh` | Guided production installer with preflight, verification and automatic rollback |
| `uninstall.sh` | Guided uninstaller with preflight, data-preserving removal and post-uninstallation checks |
| `upgrade.sh` | Guided upgrade with preflight, mandatory backup, staged replacement, verification and automatic rollback |
| `services/identity` | Users, roles, sessions, audit trail (port 8001) |
| `services/inventory` | Asset and patch ingestion, reconciliation (8002) |
| `services/compliance` | Evaluation engine, snapshots, findings (8003) |
| `services/reporting` | PDF generation, schedules, email delivery (8004) |
| `services/gateway` | Web UI, session/CSRF handling, API fan-out and TLS termination (8005) |
| `services/common` | Config, file-backed secrets, database and scheduler helpers, security, TLS, migrations |
| `deploy/postgres` | SQL migrations and the Grafana read-only role |
| `deploy/container` | Service image, entrypoint and pinned requirements |
| `docker-compose.secrets.yml` | Opt-in Compose override for file-backed bootstrap secrets |
| `deploy/grafana` | Provisioned datasource and dashboard |
| `deploy/systemd` | Native (non-container) service units |
| `scripts` | HTTPS development launcher, demo data, dashboard generator, manual job runner |
| `tests` | Unit tests, database integration tests, browser smoke test |
| `docs` | Requirements, design, operations and user documentation |

## Quick start (development)

Requires Python 3.12 and a local PostgreSQL 13 with a `patchtrack` database.

```bash
python3.12 -m venv /opt/patchtrack-venv
/opt/patchtrack-venv/bin/pip install -r deploy/container/requirements-dev.txt

scripts/dev-run.sh start      # migrations + five services on 8001-8005
scripts/dev-run.sh status
```

Open <https://127.0.0.1:8005>. The development stack serves HTTPS with a
self-signed certificate generated under `.runtime/tls`, so the browser warns
until you accept it (or install the organisation's certificate in Settings).
On a fresh database the login page shows a one-time "create administrator"
form. There is no shipped default password.

To browse from another machine, publish the gateway on this host's address
instead of loopback. Only the gateway moves; the four backends stay on
127.0.0.1 so the session, CSRF and permission checks cannot be bypassed.

```bash
PATCHTRACK_BIND_ADDRESS=<this host's IP> scripts/dev-run.sh start
```

The generated certificate includes the bind address in its SANs, so browsing
by IP matches the certificate. Set `PATCHTRACK_TLS_ENABLED=false` to develop
over plain HTTP; the launcher then sets the cookie policy to `never`
automatically, because browsers discard `Secure` cookies over HTTP.
For anything beyond a development test, run the gateway with
`PATCHTRACK_TLS_ENABLED=true` (the default in `.env.example`): it serves HTTPS
with a certificate generated at first start, which an administrator replaces
with the organisation's own at **Settings → HTTPS certificate**. With an
external proxy, set it to `false`. Leave the cookie policy at its default
`auto`.

Optional demo data (synthetic, never production figures):

```bash
PATCHTRACK_JWT_SECRET=$(openssl rand -hex 32) \
  /opt/patchtrack-venv/bin/python scripts/seed-demo.py --reset --assets 100 --years 5
```

The default seed is 100 assets and five years of daily snapshots. The history
follows an explicit month/quarter/year scenario — at every basis some periods
meet the 95% target and some miss it, and one quarter shows a collection
outage — so the trend, history views and period picker have varied, internally
consistent observations to display. Every snapshot records
`synthetic_history` in its methodology.

## Deployment

Production deployment uses Docker Compose on RHEL 9 or Rocky 9 with SELinux
enforcing. The guided installer is the recommended path: it checks the host
and dependencies, installs a missing runtime with your approval, generates the
secrets, starts and verifies the stack, and rolls back its own changes on
failure. See [docs/install.md](docs/install.md) and
[docs/configuration.md](docs/configuration.md).

```bash
sudo ./install.sh          # guided; sudo ./install.sh --check for preflight only
```

The equivalent manual procedure remains available:

```bash
cp .env.example .env      # then fill it in; chmod 600
docker compose up -d --build
```

Open `https://<host>:8005`. The gateway terminates TLS itself. On first start
it generates a self-signed certificate in the `tls` volume, so the sign-in page
is never served over plain HTTP; accept the browser warning until an
administrator installs the organisation's certificate at **Settings → HTTPS
certificate**. Set `PATCHTRACK_TLS_ENABLED=false` when a reverse proxy
terminates TLS instead; see
[TLS configuration](docs/configuration.md#8-tls-termination) and
[TLS troubleshooting](docs/troubleshooting.md#the-browser-warns-the-certificate-is-not-trusted).

To keep the bootstrap credentials out of `.env`, use the opt-in secrets
override after creating the required files:

```bash
docker compose -f docker-compose.yml -f docker-compose.secrets.yml up -d
```

It mounts the database URL, JWT secret, service token and PostgreSQL password
from `/etc/patchtrack/secrets/`; see
[secret files](docs/security.md#secret-files) for the layout and the systemd
equivalent.

Grafana is an optional profile (`--profile dashboards`). The application,
API and reports work with Grafana stopped.

## Documentation

- [Business requirements](docs/business-requirements.md)
- [Functional requirements](docs/functional-requirements.md)
- [Architecture](docs/architecture.md)
- [Data model](docs/data-model.md)
- [Compliance methodology](docs/compliance-methodology.md)
- Integrations: [AssetExplorer](docs/integration-assetexplorer.md),
  [Satellite](docs/integration-satellite.md), [SCCM](docs/integration-sccm.md),
  [Solaris](docs/integration-solaris.md)
- [Reporting specification](docs/reporting.md)
- [Security design](docs/security.md)
- [Role model](docs/roles.md)
- [Installation](docs/install.md) · [Configuration](docs/configuration.md) ·
  [Offline deployment](docs/offline-deployment.md)
- [Backup and restore](docs/backup-restore.md) ·
  [Upgrade and rollback](docs/upgrade-rollback.md)
- [Operations runbook](docs/operations-runbook.md) ·
  [Troubleshooting](docs/troubleshooting.md)
- [User guide](docs/user-guide.md) · [Release notes](docs/release-notes.md)
- [Requirements traceability](docs/traceability-matrix.md) ·
  [User stories](docs/user-stories.md) · [Roadmap](docs/roadmap.md) ·
  [Testing strategy](docs/testing-strategy.md)

## Tests

```bash
/opt/patchtrack-venv/bin/python -m pytest                 # unit + integration
/opt/patchtrack-venv/bin/python -m pytest tests/unit      # no database needed
PATCHTRACK_TEST_DATABASE_URL=postgresql+psycopg://patchtrack@/patchtrack_test?host=/tmp \
  /opt/patchtrack-venv/bin/python -m pytest tests/integration
```

The suite currently contains 580 tests: 444 unit tests and 136 database
integration tests. Integration tests create and drop their own scratch
database; never point them at a database holding real data.

Browser smoke test (needs Playwright and Chromium, development only):

```bash
PATCHTRACK_URL=https://127.0.0.1:8005 PATCHTRACK_USER=admin \
PATCHTRACK_PASSWORD=... python tests/ui/smoke.py
```

It ignores certificate errors so it can run against the development stack's
self-signed certificate. Point it at an `http://` URL with
`PATCHTRACK_TLS_ENABLED=false scripts/dev-run.sh restart` to test a plain-HTTP
stack instead.
