# Installation

Target: Red Hat Enterprise Linux 9.8 or Rocky Linux 9.8, SELinux enforcing,
Docker or Podman with Compose. Nothing in this procedure requires disabling
SELinux, and nothing requires outbound internet access at run time (see
[offline-deployment.md](offline-deployment.md) for an air-gapped host).

## 1. Preconditions

| Item | Requirement |
|------|-------------|
| OS | RHEL 9.8 or Rocky Linux 9.8, x86_64 |
| SELinux | `enforcing` (check `getenforce`) |
| Container runtime | Docker Engine 24+ with Compose v2, or Podman 4.6+ with `podman-compose` |
| CPU / RAM | 2 vCPU / 4 GB minimum; 4 vCPU / 8 GB for more than ~20,000 assets |
| Disk | 20 GB for the OS and images, plus ~1 KB per asset per assessment in PostgreSQL and ~1 MB per report |
| Network out | TCP/443 to AssetExplorer, Satellite, SCCM; TCP/587 to SMTP |
| Network in | TCP/443 to the reverse proxy from users |
| Database | Supplied by the bundled `postgres` service, or an external PostgreSQL 13+ with TLS |

Check the runtime:

```bash
getenforce                       # Enforcing
docker version --format '{{.Server.Version}}'
docker compose version

# Podman instead of Docker:
podman --version                 # 4.6 or later
podman-compose --version
```

**With Podman, build the image in Docker format.** Podman writes OCI images by
default, and the OCI format has no `HEALTHCHECK` field, so the image's health
check is silently dropped — and the gateway's `depends_on: service_healthy`
conditions then never become satisfied. Either build explicitly:

```bash
podman build --format docker -f deploy/container/Dockerfile -t patchtrack:3.0.0 .
podman-compose up -d
```

or accept that services start without a health gate and check `/healthz`
yourself. Nothing else in the deployment differs.

## 2. Create the service account and directories

```bash
sudo useradd --system --home-dir /opt/patchtrack --shell /sbin/nologin patchtrack

sudo install -d -m 0750 -o patchtrack -g patchtrack \
  /opt/patchtrack \
  /etc/patchtrack \
  /var/lib/patchtrack/reports \
  /var/lib/patchtrack/solaris-inbox

sudo install -d -m 0700 -o patchtrack -g patchtrack /etc/patchtrack/secrets
```

Directories under `/opt` and `/var/lib` are labelled `usr_t` and `var_t` by
default on RHEL/Rocky; the compose file mounts them with `:Z`, which relabels
them for container use. Do not relabel them yourself.

## 3. Install the application files

```bash
sudo tar -xzf patchtrack-3.0.0.tar.gz -C /opt/patchtrack --strip-components=1
sudo chown -R patchtrack:patchtrack /opt/patchtrack
```

(For an air-gapped host, use the bundle described in
[offline-deployment.md](offline-deployment.md) instead.)

## 4. Create the environment file

```bash
sudo install -m 0600 -o patchtrack -g patchtrack \
  /opt/patchtrack/.env.example /etc/patchtrack/.env
```

Generate the secrets — do not type them, and do not reuse them:

```bash
openssl rand -hex 32   # PATCHTRACK_JWT_SECRET
openssl rand -hex 32   # PATCHTRACK_SERVICE_TOKEN
openssl rand -hex 32   # POSTGRES_PASSWORD (also in PATCHTRACK_DATABASE_URL)
sudo -u patchtrack $EDITOR /etc/patchtrack/.env
```

There is **no** bootstrap password to set. The first administrator is created
through the web form in step 8.

Symlink it where compose expects it:

```bash
sudo ln -s /etc/patchtrack/.env /opt/patchtrack/.env
```

`.env` is ignored by git; verify with `git check-ignore -v .env`.

## 5. Start the database and apply migrations

```bash
cd /opt/patchtrack
sudo -u patchtrack docker compose up -d postgres
sudo -u patchtrack docker compose run --rm migrate
```

Expected output includes `Applied: 000_extensions, 001_bootstrap_shared, …` on
the first run and `Applied: nothing (up to date)` afterwards.

The migration runner verifies the SHA-256 of every previously applied file; a
modified migration aborts the run with an explicit error rather than applying a
silent change.

## 6. Create the Grafana read-only role

Required only if you will run the dashboard. Run it **after** migrations, as a
superuser, with a password you have generated:

```bash
GF_PASSWORD="$(openssl rand -base64 32)"
echo "$GF_PASSWORD"   # store it in the secret store and in .env as PATCHTRACK_GF_DB_PASSWORD

docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -v grafana_password="$GF_PASSWORD" \
       -U patchtrack -d patchtrack \
  < deploy/postgres/roles/010_grafana_readonly.sql
```

The script refuses an empty or short password, and re-running it rotates the
password in place. It creates a `LOGIN` role with `default_transaction_read_only`,
a 30-second statement timeout and `SELECT` on the reporting tables only — not
on `identity.*`.

## 7. Start the application

```bash
sudo -u patchtrack docker compose up -d
sudo -u patchtrack docker compose ps
```

Every service must show `healthy`:

```bash
for s in identity inventory compliance reporting gateway; do
  docker compose exec -T "$s" \
    python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read().decode())"
done
```

The gateway listens on `127.0.0.1:8005` by default. Put a TLS reverse proxy in
front of it (see [configuration.md](configuration.md#7-tls-termination)) and
keep the bind address on loopback.

## 8. Create the first administrator

Open `https://patchtrack.example.com/login`. Because no user exists, the page
offers a one-time "Create administrator" form. Pick a username and a password
of at least 12 characters. The account is created with role `administrator` and
is forced to change its password at first sign-in.

The form is refused (`409 Platform is already initialised`) once any user
exists. If you need a different first administrator later, create it from
**Administration → Users & roles** and deactivate the original.

## 9. Configure integrations

Nothing collects until you configure it — all four integrations are seeded
disabled with no endpoint and no credential.

1. Put each credential in `/etc/patchtrack/secrets/` (mode 0600) or in the
   environment, e.g. `/etc/patchtrack/secrets/satellite.password`.
2. **Administration → Integrations** → set endpoint, credential reference
   (`file:/etc/patchtrack/secrets/satellite.password`), enable, then **Run now**.
3. Confirm a `success` sync run and a non-zero `records_seen`.

Per-source detail: [AssetExplorer](integration-assetexplorer.md),
[Satellite](integration-satellite.md), [SCCM](integration-sccm.md),
[Solaris](integration-solaris.md).

## 10. Set the policy and the organisation name

- **Administration → Compliance policy**: review target percentage, freshness
  threshold and blocking severities. Create a version, then activate it.
- **Administration → Settings**: organisation name (used on PDF reports).

## 11. Schedule collection, snapshots and reports

Scheduling is done by the shipped **systemd timer**. Do not add a crontab or
an `/etc/cron.d` entry: PatchTrack does not support cron scheduling, and the
timer is the single place the cadence is configured.

```bash
sudo install -m 0644 deploy/systemd/patchtrack-tick.service \
  deploy/systemd/patchtrack-tick.timer /etc/systemd/system/
sudo install -m 0755 scripts/patchtrack-tick.sh scripts/patchtrack-job.sh \
  /opt/patchtrack/scripts/
sudo install -d -m 0750 -o patchtrack -g patchtrack /var/lib/patchtrack/state
sudo systemctl daemon-reload
sudo systemctl enable --now patchtrack-tick.timer
```

`patchtrack-tick.timer` fires hourly and `scripts/patchtrack-tick.sh` runs only
what is due, so every cadence lives in `/etc/patchtrack/tick.env` rather than
in the schedule. The runner delegates the request to
`scripts/patchtrack-job.sh`, which works for both deployments: it executes
inside the service container when one is running, and calls
`127.0.0.1:$PORT` for a native installation. In both cases it presents the
shared service token — never a human account and never the gateway, whose
`/api` routes need a browser session.

A container installation needs the `patchtrack` account to be able to reach
the container runtime (for example membership of the `docker` group) and the
compose project directory, since the job script execs into the container.

See [operations-runbook.md](operations-runbook.md#3-scheduled-jobs) for what
each job does, the available cadences, and the credential it uses.

## 12. Optional: start the Grafana dashboard

```bash
sudo -u patchtrack docker compose --profile dashboards up -d grafana
```

Open `http://127.0.0.1:3000` (or behind the proxy). The datasource and the
"PatchTrack — Patch Compliance Overview" dashboard are provisioned from
`deploy/grafana/`; do not edit them in the UI, because provisioning will
overwrite the changes.

If you disable the admin login for Grafana, keep it published only on
loopback or behind the proxy with authentication.

## 13. Verify the installation

| Check | Expected |
|-------|----------|
| `docker compose ps` | postgres, five services healthy; migrate exited 0 |
| `curl -s http://127.0.0.1:8005/healthz` | `{"status":"ok","service":"gateway"}` |
| Sign in | Succeeds; the UI shows the dashboard |
| `GET /api/compliance/summary` | Returns KPIs (zeros and `null` percentage before the first collection) |
| Audit trail | Contains your sign-in |
| `docker compose stop grafana` | Application, API and reports unaffected |
| `sudo ausearch -m avc -ts recent` | No denials mentioning patchtrack containers |

## 14. Uninstall

```bash
cd /opt/patchtrack
sudo -u patchtrack docker compose --profile dashboards down
# Data is in named volumes and is NOT removed by `down`:
docker volume ls | grep patchtrack
# Take a backup first, then, only if you really mean to destroy the data:
#   sudo -u patchtrack docker compose down -v
```

## Native installation (no containers)

The services are ordinary Python packages and can run under systemd with a
virtualenv:

```bash
sudo dnf install -y python3.12 python3.12-pip postgresql13-server
python3.12 -m venv /opt/patchtrack-venv
/opt/patchtrack-venv/bin/pip install -r /opt/patchtrack/deploy/container/requirements.txt
PATCHTRACK_DATABASE_URL=… /opt/patchtrack-venv/bin/python -m patchtrack_common.migrate
```

The container deployment is the supported one; native installation is
documented for environments where containers are prohibited. Unit files are
provided for the latter; the operator still supplies the SELinux contexts for
`/var/lib/patchtrack` and the TLS termination. See
[operations-runbook.md](operations-runbook.md#5-running-natively-under-systemd).

Four unit files ship in `deploy/systemd/`:

| File | Installs as | Purpose |
|------|-------------|---------|
| `patchtrack-migrate.service` | `/etc/systemd/system/` | One-shot migration; every service instance requires it |
| `patchtrack@.service` | `/etc/systemd/system/` | One instance per service; `%i` is `identity`, `inventory`, `compliance`, `reporting` or `gateway` |
| `patchtrack-tick.service` | `/etc/systemd/system/` | One run of `scripts/patchtrack-tick.sh`; every step not yet due is skipped |
| `patchtrack-tick.timer` | `/etc/systemd/system/` | Fires the job unit hourly |

The timer is the only supported scheduler — never a crontab or
`/etc/cron.d` entry. It is not started until you enable it, but it is shipped
so the schedule is reproducible rather than hand-written. The per-integration
cadences are configuration, in `/etc/patchtrack/tick.env` (see
[operations-runbook.md](operations-runbook.md#3-scheduled-jobs)).

```bash
sudo install -m 0644 deploy/systemd/patchtrack@.service \
  deploy/systemd/patchtrack-migrate.service \
  deploy/systemd/patchtrack-tick.service deploy/systemd/patchtrack-tick.timer \
  /etc/systemd/system/
sudo install -m 0755 scripts/patchtrack-tick.sh /opt/patchtrack/scripts/
sudo install -d -m 0750 -o patchtrack -g patchtrack /var/lib/patchtrack/state
sudo systemctl daemon-reload
sudo systemctl enable --now patchtrack-migrate.service
sudo systemctl enable --now patchtrack@identity patchtrack@inventory \
  patchtrack@compliance patchtrack@reporting patchtrack@gateway
# The only scheduler: collection, snapshots, reports and audit retention.
sudo systemctl enable --now patchtrack-tick.timer
```

Each instance reads its port from `/etc/patchtrack/ports/<instance>` (`PORT=8001`
for identity, and so on); the unit has no port default so a missing file is a
start-up failure rather than a service listening somewhere unintended. Verify
the units with `systemd-analyze verify deploy/systemd/*.service`.
