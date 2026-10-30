# Installation

Target: Red Hat Enterprise Linux 9.8 or Rocky Linux 9.8, SELinux enforcing,
Docker or Podman with Compose. Nothing in this procedure requires disabling
SELinux, and nothing requires outbound internet access at run time (see
[offline-deployment.md](offline-deployment.md) for an air-gapped host).

## Guided installation (recommended)

The guided installer performs the whole procedure below, asks only for the
deployment-specific values, and verifies the result. Run it from the unpacked
release directory as root:

```bash
sudo ./install.sh
```

The source tree and the application directory must be different: the installer
copies the tree it is run from. If the archive was unpacked directly into
`/opt/patchtrack`, choose another application directory or use the manual
procedure below.

Every run is copied in full to
`/var/log/patchtrack/install-<UTC>-<pid>.log` (mode 0600), with `install.log`
as a link to the newest one. The log includes the preflight output, rollback
messages and the tool's final exit status. Set `PATCHTRACK_LOG_DIR` to write
elsewhere; see [logging.md](logging.md#installer-upgrade-and-uninstaller-logs).

It asks for:

| Input | Default | Notes |
|-------|---------|-------|
| Application directory | `/opt/patchtrack` | Must be absent or empty; an existing deployment is never overwritten |
| Configuration directory | `/etc/patchtrack` | Holds `.env`, `install.json` and the secret files |
| Service account | `patchtrack` | Created if missing; reused if it already exists |
| Gateway bind address | `0.0.0.0` | Use `127.0.0.1` when a reverse proxy is in front |
| HTTPS port | `8005` | Published on the bind address; 8001–8004 stay internal |
| Certificate name | Host FQDN or IP | Used in the generated self-signed certificate |
| HTTP(S) proxy | Blank (direct) | For hosts whose outbound access is through a proxy; see [Hosts behind an HTTP proxy](#hosts-behind-an-http-proxy) |
| Proxy bypass list | Loopback and compose service names | Shown only when a proxy is set |
| Air-gapped host | No | When yes, asks for the verified bundle directory; see [Air-gapped installation](#air-gapped-installation) |
| First administrator | Create | Username, display name, optional email and password; the password is read without echo |

Before changing anything it checks root access, RHEL/Rocky 9 and architecture,
SELinux, CPU, memory, free disk, `dnf`, `systemctl`, account tools, the selected
port, the target directories and existing Compose resources. Image registries
and the package index are probed and reported as warnings, because a mirrored
or preloaded host can work without direct access. Every failed or warning check
is followed by a `how to fix` line naming the configuration variable, package,
repository or air-gap alternative that resolves it.

It then:

- installs Docker Engine with Compose v2 through `dnf` if no runtime is
  available and you approve it (or uses an existing Docker/Podman);
- creates the service account and directories, copies the application, and
  generates the database password, JWT secret, service token and complete
  database URL;
- writes the bootstrap values as file-backed secrets under
  `/etc/patchtrack/secrets` and blank placeholders in `.env`, so Compose is
  always run with `docker-compose.secrets.yml`;
- builds the image, starts PostgreSQL, applies migrations, starts all five
  services and waits for `/readyz`;
- writes and enables `/etc/systemd/system/patchtrack.service`, then starts it
  so the running stack is registered with systemd. Normal operation is then
  `sudo systemctl start|stop|restart patchtrack` and
  `sudo journalctl -u patchtrack`; see [start-stop.md](start-stop.md);
- creates the first administrator through the one-time bootstrap endpoint and
  confirms an authenticated session with the forced password-change flag set.

If any step fails, the installer reverses the actions it recorded, newest
first: containers, volumes and the built image, application files, the account
it created, a Docker service it started, package transactions it made, and
container data only if that path did not exist beforehand. An existing
directory, user, repository file or container project is never removed. The
rollback reports anything it could not reverse rather than claiming success.

Run the dependency checks alone, without changing the host:

```bash
sudo ./install.sh --check
```

To remove an installation later, use the
[guided uninstallation](#guided-uninstallation-recommended); it preserves data
volumes unless you explicitly ask to delete them. To move to a newer release,
run the new release's `upgrade.sh`; see
[Guided upgrade](upgrade-rollback.md#3-guided-upgrade).

The installer does not open a firewall port. Permit TCP/8005 (or the port you
chose) in the host firewall before users connect.

### Non-interactive use

`--yes` accepts the defaults and reads parameters from `PATCHTRACK_INSTALL_*`
environment variables. It creates the first administrator only when
`PATCHTRACK_INSTALL_ADMIN_PASSWORD` is set; otherwise it leaves the one-time
web form for the operator. Useful variables are
`PATCHTRACK_INSTALL_DIR`, `PATCHTRACK_INSTALL_CONFIG_DIR`,
`PATCHTRACK_INSTALL_SERVICE_USER`, `PATCHTRACK_INSTALL_BIND_ADDRESS`,
`PATCHTRACK_INSTALL_GATEWAY_PORT`, `PATCHTRACK_INSTALL_TLS_HOSTNAME`,
`PATCHTRACK_INSTALL_ADMIN_USERNAME`, `PATCHTRACK_INSTALL_ADMIN_DISPLAY_NAME`,
`PATCHTRACK_INSTALL_ADMIN_EMAIL`, `PATCHTRACK_INSTALL_RUNTIME`,
`PATCHTRACK_INSTALL_PROXY`, `PATCHTRACK_INSTALL_NO_PROXY`,
`PATCHTRACK_INSTALL_AIRGAP`, `PATCHTRACK_INSTALL_BUNDLE_DIR` and the preflight
minimums `PATCHTRACK_INSTALL_MIN_DISK_GB` and
`PATCHTRACK_INSTALL_MIN_MEMORY_GB`.

```bash
sudo PATCHTRACK_INSTALL_BIND_ADDRESS=0.0.0.0 \
  PATCHTRACK_INSTALL_TLS_HOSTNAME=patchtrack.example.com \
  ./install.sh --yes
```

`--runtime docker|podman` selects a runtime explicitly. `--allow-permissive`
and `--allow-unsupported-os` exist only for documented exceptions and are not
part of a supported production installation.

### Hosts behind an HTTP proxy

If the host can reach package mirrors and image registries only through an
HTTP(S) proxy, enter it when prompted (interactive) or set
`PATCHTRACK_INSTALL_PROXY` (non-interactive). Leave it blank for direct access.

```bash
sudo PATCHTRACK_INSTALL_PROXY=http://proxy.example.com:3128 \
  ./install.sh --yes
```

The installer then:

- runs `dnf` with `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` in its
  environment, so repository metadata and packages are fetched through the
  proxy;
- writes `/etc/systemd/system/docker.service.d/10-patchtrack-proxy.conf` and
  restarts the Docker daemon, because the daemon (not the CLI) pulls the base
  and service images. An existing daemon proxy or drop-in is left unchanged
  with a warning. Podman needs no daemon file: the variables are passed to
  `podman build` and `podman-compose`;
- passes the proxy to BuildKit for `RUN` steps, without putting the URL on a
  command line;
- writes `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` (and the lowercase
  spellings) into `/etc/patchtrack/.env`, so the services can reach external
  systems such as an internet-facing SMTP relay. The default bypass list is
  `localhost,127.0.0.1,::1` plus the compose service names, so database and
  gateway fan-out traffic never goes to the proxy.

Preflight connects to the proxy's TCP port and repeats the registry and
package-index probes through it. A proxy URL may contain credentials; the
summary and all installer output redact them, but the full URL is stored in
`.env` (mode 0640, owned by `root:patchtrack`). Prefer a proxy that
authenticates by network or host, or accept that the credential is readable by
the PatchTrack group.

If integrations or the relay are themselves reached through the proxy, adjust
the bypass list with `PATCHTRACK_INSTALL_NO_PROXY` (interactive: the "Hosts that
must bypass the proxy" prompt). When rolling back, the installer removes the
drop-in, reloads systemd and restarts Docker, restoring the previous daemon
configuration.

### Air-gapped installation

An air-gapped host has no internet access. Install from the verified bundle
described in [offline-deployment.md](offline-deployment.md); do not run the
installer against an unverified bundle.

```bash
sudo ./install.sh --airgap --bundle-dir /srv/bundles/patchtrack-3.0.3
sudo ./install.sh --airgap --bundle-dir /srv/bundles/patchtrack-3.0.3 --check
```

`--bundle-dir` implies `--airgap`. In interactive mode the installer asks
whether the host is air-gapped and, if so, for the bundle directory. The
directory may also be supplied with `PATCHTRACK_INSTALL_BUNDLE_DIR` (and
`PATCHTRACK_INSTALL_AIRGAP=1`); a blank bundle directory means the images were
already loaded by hand.

Air-gap mode changes the preflight and the installation:

- it does not probe registries, the package index, the proxy or any outbound
  endpoint;
- it requires `SHA256SUMS` in the bundle and runs
  `sha256sum -c --ignore-missing SHA256SUMS`. The application and PostgreSQL
  archives must be listed and every listed file that is present must verify
  before anything is changed. A removed optional pinned base, source archive
  or wheel directory is only a warning; a removed required archive fails the
  separate presence check;
- if packages must be installed it requires an enabled `dnf` repository and
  reminds you to confirm that repository is local. It never adds the remote
  Docker CE repository;
- before installing files it loads the application and PostgreSQL images from
  the bundle, so a stale local image with the same tag cannot replace the
  verified archive. The supplied application image is used as-is instead of
  being rebuilt;
- Docker Compose starts with `--pull never`.

The bundle must not be inside, or contain, the application or configuration
directory; the installer and uninstaller both refuse that layout because
uninstalling the application would otherwise delete the only copy of the
bundle.

The bundle directory is recorded in `/etc/patchtrack/install.json` as
`airgap_bundle` with `installation_mode: airgap`. The bundle itself is not
modified or copied; keep it for upgrades and rollback.

If an air-gap installation fails after an image was loaded, rollback removes
the image tag this installation created. The application image is not restored
from the bundle automatically; rerun the installer, which will load it again.

## Manual installation

The numbered procedure below is the same deployment performed step by step.
Use it when the organisation requires a reviewed command sequence, a custom
runtime, or an external PostgreSQL service.

## 1. Preconditions

| Item | Requirement |
|------|-------------|
| OS | RHEL 9.8 or Rocky Linux 9.8, x86_64 |
| SELinux | `enforcing` (check `getenforce`) |
| Container runtime | Docker Engine 24+ with Compose v2, or Podman 4.6+ with `podman-compose` |
| CPU / RAM | 2 vCPU / 4 GB minimum; 4 vCPU / 8 GB for more than ~20,000 assets |
| Disk | 20 GB for the OS and images, plus ~1 KB per asset per assessment in PostgreSQL and ~1 MB per report |
| Network out | TCP/443 to AssetExplorer, Satellite, SCCM; TCP/587 to SMTP |
| Network in | TCP/8005 to the gateway from users (the gateway serves HTTPS itself); TCP/443 only when an external reverse proxy terminates TLS |
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
podman build --format docker -f deploy/container/Dockerfile -t patchtrack:3.0.3 .
podman-compose up -d
```

or accept that services start without a health gate and check `/healthz`
yourself. Nothing else in the deployment differs.

## 2. Create the service account and directories

```bash
sudo useradd --system --home-dir /opt/patchtrack --shell /sbin/nologin patchtrack

# The manual procedure runs Compose as this account, which therefore needs
# access to the Docker socket. (The generated systemd unit runs as root and
# does not need this.) With Podman, grant access to the rootless socket
# instead. Verify with the command that follows.
sudo usermod -aG docker patchtrack
sudo -u patchtrack docker info >/dev/null && echo "docker access ok"

sudo install -d -m 0750 -o patchtrack -g patchtrack \
  /opt/patchtrack \
  /etc/patchtrack \
  /var/lib/patchtrack/reports \
  /var/lib/patchtrack/solaris-inbox \
  /var/lib/patchtrack/tls

sudo install -d -m 0700 -o patchtrack -g patchtrack /etc/patchtrack/secrets
```

Directories under `/opt` and `/var/lib` are labelled `usr_t` and `var_t` by
default on RHEL/Rocky; the compose file mounts them with `:Z`, which relabels
them for container use. Do not relabel them yourself.

## 3. Install the application files

```bash
sudo tar -xzf patchtrack-3.0.3.tar.gz -C /opt/patchtrack --strip-components=1
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

Optional but recommended for production: keep the bootstrap credentials out
of `.env` by using file-backed secrets. `docker-compose.secrets.yml` mounts
`database_url`, `jwt_secret`, `service_token` and `postgres_password` from
`/etc/patchtrack/secrets/`; see
[security.md](security.md#secret-files) for the file layout and the systemd
equivalent. Use it from the start, or migrate to it after the stack is
verified and then remove the plain values from `.env`.

If this host reaches the internet through a proxy, add the proxy to the same
file so the services inherit it. Keep loopback and the compose service names
in the bypass list:

```bash
HTTP_PROXY=http://proxy.example.com:3128
HTTPS_PROXY=http://proxy.example.com:3128
NO_PROXY=localhost,127.0.0.1,::1,postgres,identity,inventory,compliance,reporting,gateway,migrate
```

Configure `dnf` and the Docker daemon as well, because neither reads `.env`:

```bash
# Package manager (or set proxy= in /etc/dnf/dnf.conf)
sudo dnf --setopt=proxy=http://proxy.example.com:3128 install -y dnf-plugins-core

# Docker daemon: needed for image pulls during build and compose up
sudo install -d -m 0755 /etc/systemd/system/docker.service.d
sudo tee /etc/systemd/system/docker.service.d/10-patchtrack-proxy.conf >/dev/null <<'EOF'
[Service]
Environment="HTTP_PROXY=http://proxy.example.com:3128" "HTTPS_PROXY=http://proxy.example.com:3128" "NO_PROXY=localhost,127.0.0.1,::1"
EOF
sudo systemctl daemon-reload
sudo systemctl restart docker
```

With Podman, export `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` in the shell
that runs `podman build` and `podman-compose`; no daemon file is required.

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

## 6. Start the application

```bash
sudo -u patchtrack docker compose up -d
sudo -u patchtrack docker compose ps
```

Every service must show `healthy`:

```bash
for s in identity inventory compliance reporting; do
  docker compose exec -T "$s" \
    python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read().decode())"
done
docker compose exec -T gateway \
  python -c "import ssl,urllib.request;print(urllib.request.urlopen('https://127.0.0.1:8000/healthz', context=ssl._create_unverified_context()).read().decode())"
```

The gateway serves HTTPS on `127.0.0.1:8005` by default. On first start it
generates a self-signed certificate under the `tls` volume, so even a brand-new
installation is not exposed over plain HTTP; the browser warning disappears
once you install your own certificate (step 10). Keep the bind address on
loopback unless a reverse proxy is in front (see
[configuration.md](configuration.md#8-tls-termination)).

## 7. Create the first administrator

Open `https://patchtrack.example.com/login`. Because no user exists, the page
offers a one-time "Create administrator" form. Pick a username and a password
of at least 12 characters. The account is created with role `administrator` and
is forced to change its password at first sign-in.

The form is refused (`409 Platform is already initialised`) once any user
exists. If you need a different first administrator later, create it from
**Administration → Users & roles** and deactivate the original.

## 8. Configure integrations

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

## 9. Set the policy and the organisation name

- **Administration → Compliance policy**: review target percentage, freshness
  threshold and blocking severities. Create a version, then activate it.
- **Administration → Settings**: organisation name (used on PDF reports).
- **Administration → Settings → Email delivery**: SMTP relay used for scheduled
  reports; use **Send test email** to confirm the path. The `PATCHTRACK_SMTP_*`
  variables are only the fallback until a relay is saved here.
- **Administration → Settings → HTTPS certificate**: upload the organisation's
  PEM certificate (with its chain) and the unencrypted private key. Until then
  the gateway is serving the self-signed certificate generated at first start,
  and browsers warn. The replacement is checked, applied to the live listener
  without a restart, and recorded as `settings.tls_updated`. If an external
  proxy terminates TLS instead, this tab reports that certificates are managed
  externally.

## 10. Schedule collection, snapshots and reports

Scheduling is built into the services. Each one runs a small scheduler loop in
its own process and executes the jobs it owns: inventory collects from its
configured sources, compliance captures the daily snapshot and refreshes the
evaluation, reporting evaluates report schedules and applies report retention,
and identity applies the audit-retention window. Nothing has to be installed or
enabled for this, and there is no systemd timer, crontab or `/etc/cron.d`
entry to add.

The interval settings live in the service environment
([configuration.md](configuration.md#scheduler)); the defaults are six-hourly
AssetExplorer and Satellite collection, daily SCCM, weekly Solaris, a daily
snapshot and audit purge, and a report tick every minute. Due times are stored
in `shared.scheduled_jobs`, so restarting a service does not replay a job that
just ran. A failed job is retried after `PATCHTRACK_SCHEDULER_RETRY_SECONDS`
rather than waiting for its full interval.

`scripts/patchtrack-job.sh` remains available to an operator who needs to run
one job immediately (`sync <integration>`, `daily`, `tick`, `purge`,
`audit-purge`). It is a manual tool, not the scheduler. See
[operations-runbook.md](operations-runbook.md#3-scheduled-jobs) for what each
job does and the credential it uses.

## 11. Verify the installation

| Check | Expected |
|-------|----------|
| `docker compose ps` | postgres, five services healthy; migrate exited 0 |
| `curl -sk https://127.0.0.1:8005/healthz` | `{"status":"ok","service":"gateway"}` (use `-k` until your own certificate is installed) |
| Sign in | Succeeds; the UI shows the dashboard |
| `GET /api/compliance/summary` | Returns KPIs (zeros and `null` percentage before the first collection) |
| Audit trail | Contains your sign-in |
| `sudo ausearch -m avc -ts recent` | No denials mentioning patchtrack containers |

## 12. Manual uninstallation

```bash
cd /opt/patchtrack
sudo -u patchtrack docker compose --profile dashboards down
# Data is in named volumes and is NOT removed by `down`:
docker volume ls | grep patchtrack
# Take a backup first, then, only if you really mean to destroy the data:
#   sudo -u patchtrack docker compose down -v
```

## Guided uninstallation (recommended)

`uninstall.sh` uses the manifest written by `install.sh`. Run it from the
unpacked release directory as root:

```bash
sudo ./uninstall.sh --check     # prechecks and the removal plan only
sudo ./uninstall.sh             # asks for confirmation
sudo ./uninstall.sh --purge-data
```

It refuses to run when the manifest is missing, when the paths in it are not
absolute or overlap, when the application directory no longer looks like a
PatchTrack deployment, when the container runtime or Compose is unavailable,
or when the configuration directory is a symbolic link. If the installer used
a non-default configuration directory, pass `--config-dir /path` so it can
find `install.json`.

By default it removes:

- the Compose containers and network;
- the `patchtrack:<version>` image;
- the application directory;
- only the configuration files the installer created: `.env`,
  `secrets/postgres_password`, `secrets/database_url`, `secrets/jwt_secret`,
  `secrets/service_token` and `install.json`;
- the `patchtrack.service` systemd unit, after stopping and disabling it;
- the service user and group, but only if this installation created them and
  no application or configuration files remain. An account that existed
  before installation is kept.

It preserves by default:

- every named data volume, including PostgreSQL, reports, the Solaris inbox
  and the gateway TLS certificate (`--purge-data` deletes them, after a
  separate confirmation);
- configuration files it did not create, such as an operator's
  `secrets/satellite.password`; the configuration directory is left in place
  and the files are listed;
- the Docker or Podman packages and the Docker proxy drop-in. Use
  `--remove-proxy-config` only when the proxy drop-in written by the installer
  is no longer needed; it is removed only when it carries the installer's
  marker, and Docker is restarted;
- for an air-gap installation (`installation_mode: airgap` in `install.json`),
  the bundle directory and its archives. The uninstaller reports the mode and
  the recorded `airgap_bundle`, removes the application image it installed,
  and leaves the bundle in place for reinstalling, upgrading or rollback. The
  PostgreSQL and base images are not installer-owned and are not removed.

Other options: `--keep-image`, `--keep-account`, `--yes` (non-interactive;
combine with `--purge-data` only when you intend to destroy the data). There
is no rollback, because uninstallation deletes state — run `--check` first.

After removal, the uninstaller verifies that the application directory,
managed configuration, containers, network and image are gone, that the
account is gone when it was removed, and that volumes are either purged or
still present. It exits non-zero if any check fails and prints anything it
could not remove.

Back up the database and reports first if you will use `--purge-data`; see
[backup-restore.md](backup-restore.md).

The uninstaller's own run log is written before anything is removed, and
`/var/log/patchtrack` is not part of the installation manifest, so the record
of the uninstallation survives it. The directory is left in place by default;
copy it elsewhere before decommissioning the host. See
[logging.md](logging.md#installer-upgrade-and-uninstaller-logs).

## Native installation (no containers)

The services are ordinary Python packages and can run under systemd with a
virtualenv:

```bash
sudo dnf install -y python3.12 python3.12-pip postgresql13-server
python3.12 -m venv /opt/patchtrack-venv
/opt/patchtrack-venv/bin/pip install -r /opt/patchtrack/deploy/container/requirements.txt
PYTHONPATH=/opt/patchtrack/services/common:/opt/patchtrack/services \
PATCHTRACK_DATABASE_URL=… /opt/patchtrack-venv/bin/python -m patchtrack_common.migrate
```

The container deployment is the supported one; native installation is
documented for environments where containers are prohibited. Unit files are
provided for the latter; the operator still supplies the SELinux contexts for
`/var/lib/patchtrack` (including `tls/`, where the gateway writes its generated
certificate) and, if not using the gateway's own HTTPS, the TLS termination. See
[operations-runbook.md](operations-runbook.md#5-running-natively-under-systemd).

Two unit files ship in `deploy/systemd/`:

| File | Installs as | Purpose |
|------|-------------|---------|
| `patchtrack-migrate.service` | `/etc/systemd/system/` | One-shot migration; every service instance requires it |
| `patchtrack@.service` | `/etc/systemd/system/` | One instance per service; `%i` is `identity`, `inventory`, `compliance`, `reporting` or `gateway` |

Scheduled jobs are not a unit: each service instance runs its own scheduler
loop. The per-integration cadences and the scheduler switch are configuration
in the service environment (see
[configuration.md](configuration.md#scheduler)), not in a timer environment
file.

The in-app data archive is implemented by the `backup` container in the
Compose deployment. A native host does not get it automatically: run
`deploy/backup/backup-worker.sh` under its own systemd unit with `psql`,
`pg_dump` and `pg_restore` on `PATH` at the server's major version, and with
`PATCHTRACK_DATABASE_URL`, `PATCHTRACK_BACKUP_DIR` and
`PATCHTRACK_REPORT_DIR` set. Without it, requests from **Administration → Data
backups** stay queued; use the host-side `pg_dump` procedure in
[backup-restore.md](backup-restore.md#2-database-backup) instead.

```bash
sudo install -m 0644 deploy/systemd/patchtrack@.service \
  deploy/systemd/patchtrack-migrate.service /etc/systemd/system/
sudo install -m 0755 scripts/patchtrack-job.sh /opt/patchtrack/scripts/
sudo systemctl daemon-reload
sudo systemctl enable --now patchtrack-migrate.service
sudo systemctl enable --now patchtrack@identity patchtrack@inventory \
  patchtrack@compliance patchtrack@reporting patchtrack@gateway
```

Each instance reads its port from `/etc/patchtrack/ports/<instance>` (`PORT=8001`
for identity, and so on); the unit has no port default so a missing file is a
start-up failure rather than a service listening somewhere unintended. Verify
the units with `systemd-analyze verify deploy/systemd/*.service`.
