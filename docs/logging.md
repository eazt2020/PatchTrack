# Logging

PatchTrack writes four kinds of logs. The audit trail is different: it is
business data in `shared.audit_events`, not an operating-system log, and is
covered in [data-model.md](data-model.md#sharedaudit_events--append-only).

| Log | Written by | Where |
|---|---|---|
| Installation, upgrade and uninstallation | `install.sh`, `upgrade.sh`, `uninstall.sh` | `/var/log/patchtrack/` |
| Application services | Each FastAPI service's stdout/stderr | Container log driver, or the systemd journal |
| PostgreSQL | The database container or `postgresql-13` | Container log driver, the journal, or the PostgreSQL log directory |
| Backup worker | `backup` container | Container log driver |

## Installer, upgrade and uninstaller logs

The three maintenance wrappers run the Python tools through
`scripts/patchtrack-run-logged.sh`. Every line printed on the terminal —
including output inherited from `dnf`, the container runtime, `pg_dump` and
`psql` — is also written to a file:

```
/var/log/patchtrack/install-20261030T015500Z-4821.log
/var/log/patchtrack/upgrade-20261030T020512Z-9963.log
/var/log/patchtrack/uninstall-20261030T031044Z-11352.log
```

- The newest run of each tool is also available as `install.log`,
  `upgrade.log` or `uninstall.log` in the same directory, a symlink to the
  timestamped file.
- Files are mode `0600`; the directory is mode `0750`. Run the wrappers with
  `sudo`, as the installer requires.
- The header records the UTC start time, the command line, the invoking user
  and the host. The footer records the end time and the tool's exit status, so
  a failed run is identifiable without reading the whole file.
- Logs are kept for 90 days. `PATCHTRACK_LOG_RETENTION_DAYS` changes that.
- `PATCHTRACK_LOG_DIR` moves the directory, for example to a mounted log
  partition. If it, or the default `/var/log/patchtrack`, is not writable, the
  wrapper falls back to
  `${XDG_STATE_HOME:-$HOME/.local/state}/patchtrack/logs` and says so on the
  terminal.
- If neither location can be created, the tool still runs; the wrapper prints a
  warning that no run log exists. It does not refuse an installation over a
  logging failure.

The log contains no environment file, credential file, generated secret or
password. Passwords are read from the terminal without echo and are not part
of the process output. The command line is recorded; none of the supported
options accepts a password, token or private key.

The uninstaller writes its log before removing anything, and the log directory
is not part of the installation manifest, so the evidence of the uninstallation
survives it. Preserve `/var/log/patchtrack` if the host is being decommissioned.

### Reading a failed run

```bash
sudo tail -80 /var/log/patchtrack/install.log
sudo grep -n "Exit status" /var/log/patchtrack/install-*.log
```

The installer prints the failing precheck or step with a `how to fix` line,
and its rollback actions, to the same log. The upgrader records the backup
location and any rollback in the log; keep the log with that backup.

## Application logs

Each service logs to stdout/stderr. Nothing is written to a file inside the
container: the application filesystem is read-only apart from `/tmp`, and the
container runtime is responsible for retaining output.

### Docker Compose (the supported deployment)

Compose uses the `json-file` driver with `max-size: 20m` and `max-file: 5` for
every PatchTrack service, PostgreSQL and the backup worker. Each container
therefore keeps at most about 100 MB, and older output is discarded
automatically.

```bash
cd /opt/patchtrack
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.secrets.yml"

$COMPOSE logs --tail=200 identity
$COMPOSE logs -f --since 15m reporting
$COMPOSE logs --no-color > /var/tmp/patchtrack-services.log
$COMPOSE logs postgres --tail=100
$COMPOSE logs backup --tail=100
$COMPOSE logs migrate             # one-shot migration run
```

Omit `-f docker-compose.secrets.yml` only when the deployment does not use
file-backed secrets. `docker compose logs` reads the same driver files the
runtime keeps, so it works after a container restart until the container is
removed. To find the raw file for one container:

```bash
docker inspect --format '{{.LogPath}}' patchtrack-reporting-1
```

On Podman, use `podman logs <container>` (or `podman-compose logs`). Rootful
Podman often uses `journald` rather than `json-file`; check with:

```bash
podman inspect --format '{{.HostConfig.LogConfig.Type}}' <container>
journalctl CONTAINER_NAME=patchtrack-reporting --since today
```

The `patchtrack` Compose project names containers `<project>-<service>-<n>`,
for example `patchtrack-gateway-1`.

On an installer deployment the `patchtrack.service` systemd unit runs Compose
in the foreground, so the same service output is also collected by the
journal. This is the simplest place for a host administrator to look:

```bash
journalctl -u patchtrack -f
journalctl -u patchtrack --since today
```

The container driver still keeps its own copy, so `docker compose logs` remains
valid for a container that the unit has stopped.

### Native systemd deployment

The units run uvicorn in the foreground, so its output goes to the journal:

```bash
journalctl -u patchtrack@identity --since today
journalctl -u 'patchtrack@*' --since yesterday
journalctl -u patchtrack@gateway -f
journalctl -u patchtrack-migrate --since today
```

Use `journalctl -o json-pretty` when a log line needs to be parsed. Journal
rotation is configured in `/etc/systemd/journald.conf` (`SystemMaxUse`, for
example `2G`), not per service.

### Log level

`PATCHTRACK_LOG_LEVEL` controls the Python services. The container entrypoint
accepts `critical`, `error`, `warning`, `info`, `debug` or `trace` in any
letter case; the native unit requires the lowercase spelling because uvicorn
does not normalise it. The default is `info`.

Changing it requires recreating the affected containers
(`docker compose up -d --force-recreate identity …`) or restarting the systemd
instances. `debug` and `trace` are for a bounded investigation: they raise log
volume and can include request paths, so return to `info` afterwards.

### Requests and errors

- Uvicorn writes one access line per HTTP request and a traceback for an
  unhandled error. A handled API error is returned as JSON and is not a
  traceback.
- The gateway logs TLS handshake and upstream failures; the backend services
  log their own database and scheduler failures.
- Scheduler results are additionally stored in `shared.scheduled_jobs` and
  visible at **Reporting → Schedules → System jobs**, so a missed run can be
  diagnosed without the raw log.
- Report generation and delivery failures are recorded on the report and
  delivery rows, not only in the log. A failed email never invalidates a
  generated report.

Application logs never contain credentials, session tokens or encrypted
settings. If one does, treat it as a defect: rotate the credential and report
the path and log line without attaching the secret itself.

## PostgreSQL logs

### Compose

The `postgres` service logs to stderr, which the container driver captures:

```bash
cd /opt/patchtrack
docker compose logs postgres --tail=200
docker compose logs postgres --since 1h | grep -i -E "error|fatal|panic"
```

The default `postgres:17` image does not enable `logging_collector`, so there
is no separate server log file inside the container. The container log is the
server log. It includes startup/shutdown, connection failures, `FATAL` errors
and any statement that fails. Routine statements are not logged.

The database is not published on a host port, so client-side connection
attempts appear only through the Compose network or from `psql` run with
`docker compose exec`. To inspect the server's actual settings:

```bash
docker compose exec -T postgres psql -U patchtrack -d patchtrack -c \
  "SHOW log_destination; SHOW logging_collector; SHOW log_min_messages;"
```

If the organisation requires statement logging or a longer database log
history, configure the PostgreSQL image's `command`/`postgresql.conf` and ship
those logs with the host log system. Do not enable `log_statement = all` in
normal operation: it records every SQL statement, including the text of
queries that may contain report filters, and fills the volume quickly.

### Native PostgreSQL 13

The unit installed by `dnf` logs to the systemd journal by default:

```bash
journalctl -u postgresql-13 --since today
journalctl -u postgresql-13 -f
```

If `logging_collector` is enabled in `postgresql.conf`, files are under
`log_directory` relative to the data directory, by default
`/var/lib/pgsql/data/log/`:

```bash
sudo -u postgres psql -Atc "SHOW data_directory; SHOW log_directory;"
sudo ls -lt /var/lib/pgsql/data/log | head
```

The application role is not a superuser and cannot read the server log; use
the `postgres` OS account or `journalctl` as an administrator.

## Backup worker logs

The `backup` container logs each claimed request, the dump and report
archiving, retention removals and failures:

```bash
cd /opt/patchtrack
docker compose logs backup --tail=100
docker compose logs -f backup
```

A failed archive stores a short reason in `shared.backup_runs.message` and
shows it on **Administration → Data backups**, so the row is the first place to
look; the container log has the command output that produced it. A worker that
was killed mid-archive marks the run failed on its next start and removes the
half-written temporary directory.

## Retention

| Log | Retention |
|---|---|
| Installer / upgrade / uninstaller files | 90 days (`PATCHTRACK_LOG_RETENTION_DAYS`) |
| Container logs | 20 MB × 5 files per container (Compose configuration) |
| systemd journal | `SystemMaxUse` in `/etc/systemd/journald.conf` |
| PostgreSQL native logs | `log_rotation_age` / `log_rotation_size` in `postgresql.conf` |
| Audit trail | Business retention policy (default 2,555 days); see [data-model.md](data-model.md#retention) |

Do not delete a running container's log file to free space. Lower
`max-size`, rotate with the runtime, or archive the output with
`docker compose logs` before pruning.

## Collecting logs for support

```bash
cd /opt/patchtrack
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.secrets.yml"
$COMPOSE ps > /var/tmp/patchtrack-ps.txt
$COMPOSE logs --since 24h --no-color > /var/tmp/patchtrack-services.log
sudo ls -l /var/log/patchtrack > /var/tmp/patchtrack-installer-logs.txt
sudo cp /var/log/patchtrack/install.log /var/tmp/ 2>/dev/null || true
```

Attach the service log, the installer log for the run that changed the host,
and the output of the database queries in
[operations-runbook.md](operations-runbook.md#9-escalation-data-to-collect).
Never attach `.env`, credential files, a TLS private key or a database dump.

## Troubleshooting the logs themselves

| Symptom | Cause and action |
|---|---|
| No `install-*.log` after `./install.sh` | The run predates this version, or neither log directory was writable. The wrapper prints a warning in the latter case; set `PATCHTRACK_LOG_DIR` to a writable path and rerun. |
| `install.log` points to a missing file | Someone removed the timestamped file; the symlink is not self-healing. The next run replaces it. |
| `docker compose logs` shows only recent output | The json-file rotation limit was reached. Raise `max-size`/`max-file` before reproducing, or ship logs to the host log system. |
| Application output is absent from the journal | The native unit failed before uvicorn started; check `systemctl status patchtrack@<service>` and `journalctl -u patchtrack@<service> -b`. |
| PostgreSQL log is empty | On Compose this means no messages have been emitted; on native hosts the collector may be disabled, so check the journal as above. |
