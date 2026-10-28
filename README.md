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

## What is in this repository

| Path | Contents |
|------|----------|
| `services/identity` | Users, roles, sessions, audit trail (port 8001) |
| `services/inventory` | Asset and patch ingestion, reconciliation (8002) |
| `services/compliance` | Evaluation engine, snapshots, findings (8003) |
| `services/reporting` | PDF generation, schedules, email delivery (8004) |
| `services/gateway` | Web UI, session/CSRF handling, API fan-out (8005) |
| `services/common` | Config, database helpers, security, migrations |
| `deploy/postgres` | SQL migrations and the Grafana read-only role |
| `deploy/container` | Service image, entrypoint and pinned requirements |
| `deploy/grafana` | Provisioned datasource and dashboard |
| `deploy/systemd` | Native (non-container) service units |
| `scripts` | Development launcher, demo data, dashboard generator, manual job runner |
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
enforcing. See [docs/install.md](docs/install.md) and
[docs/configuration.md](docs/configuration.md).

```bash
cp .env.example .env      # then fill it in; chmod 600
docker compose up -d --build
```

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

Integration tests create and drop their own scratch database; never point them
at a database holding real data.

Browser smoke test (needs Playwright and Chromium, development only):

```bash
PATCHTRACK_URL=https://127.0.0.1:8005 PATCHTRACK_USER=admin \
PATCHTRACK_PASSWORD=... python tests/ui/smoke.py
```
