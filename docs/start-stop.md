# Starting and stopping PatchTrack gracefully

The order matters: the gateway and application services must stop before
PostgreSQL, and PostgreSQL must be accepting connections before the services
start. A normal stop asks each process to finish what it is doing; a forced
kill can leave an interrupted report, collection or backup for the next start
to clean up.

## Compose deployment (supported)

Run these commands from the directory that holds the deployment, normally
`/opt/patchtrack`. The installer uses the secrets override, so include it:

```bash
cd /opt/patchtrack
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.secrets.yml"
```

Omit `-f docker-compose.secrets.yml` only when the deployment does not use
file-backed secrets. All commands below also work with `podman-compose` (or
`podman compose`) in place of `docker compose`.

### Start

```bash
$COMPOSE up -d
```

This starts PostgreSQL, waits for its health check, runs the one-shot
`migrate` service, then starts the services that depend on it. Migrations are
idempotent; an up-to-date database changes nothing and applies only new
migration files. Grafana is optional and starts only with its profile.

Verify readiness rather than relying on the command's exit status:

```bash
$COMPOSE ps
curl -fsSk https://127.0.0.1:8005/readyz
curl -fsSk https://127.0.0.1:8005/healthz
```

`readyz` succeeds when the gateway can reach every backend and the database is
usable. The sign-in page should also load. On first start the gateway generates
a self-signed certificate, so `-k` (or the organisation's CA) is expected.

### Stop

```bash
$COMPOSE stop
```

`stop` sends `SIGTERM` to each container's main process and leaves the
containers and volumes in place. The application handles `SIGTERM` through
uvicorn's graceful shutdown: new connections stop, in-flight HTTP requests are
allowed to finish, and the in-app scheduler stops within about five seconds.
The Compose services allow 30 seconds before a forced `SIGKILL`
(`stop_grace_period`); PostgreSQL performs a fast shutdown and flushes its
write-ahead log.

If a long request needs more time, stop explicitly with a longer timeout and in
dependency order:

```bash
$COMPOSE stop -t 60 gateway
$COMPOSE stop -t 60 reporting compliance inventory identity
$COMPOSE stop -t 60 backup
$COMPOSE stop -t 120 postgres
```

Stopping in this order matters:

1. **gateway** first, so no new user request enters the backend while it is
   shutting down;
2. **reporting, compliance, inventory, identity** next. Each finishes its
   current HTTP request and stops its scheduler loop;
3. **backup** after the application services, so no new archive request is
   queued while the worker is stopping;
4. **postgres** last. The application no longer holds connections, so it can
   shut down without terminating them.

Do not stop PostgreSQL first, and do not use `docker kill`, `docker compose
kill`, `kill -9`, or `podman kill` on a healthy stack. Those skip graceful
shutdown and can require database crash recovery, leave a half-written report
or leave a backup row in `running` until the worker's next start marks it
failed.

`stop` does not delete data. `down` removes the containers and the Compose
network but still preserves named volumes; `down -v` deletes the database,
reports, backups and TLS certificate and must never be used on a live
deployment.

### Restart

```bash
# One service, keeping the others up:
$COMPOSE restart reporting

# Recreate a service after editing .env or the Compose file:
$COMPOSE up -d --force-recreate gateway

# Restart everything (same graceful semantics as up/stop):
$COMPOSE restart
```

`restart` sends the same `SIGTERM` followed by a start; it is appropriate for an
application-level restart. After a change to `.env`, use
`up -d --force-recreate`, because Compose must apply the new environment.
Changes to database schema only take effect through the `migrate` service,
which `up -d` runs; `restart` does not run it.

### Scheduled work across a stop

- An in-flight collection or snapshot may outlive the shutdown window. The
  scheduler deliberately stops waiting after five seconds; the job's
  `next_run_at` is still pending, so it runs after the next start. No data is
  marked collected that was not.
- A report being generated may be interrupted. On restart, a report left in
  `generating` is detectable on the Reports screen; generate it again. A
  completed report and its file are independent of the stop.
- A backup archive interrupted by the stop is left incomplete and is marked
  failed on the worker's next start. The database row says failed; create a new
  backup. Do not download the partial file (it is not listed as completed).
- Stopping PostgreSQL while the application is still writing can abort a
  transaction. The application's next request fails and is retried; stop the
  services first as above.

### Check that everything stopped

```bash
$COMPOSE ps                    # State should be "exited" for every container
$COMPOSE logs --tail=20 gateway
$COMPOSE logs --tail=20 postgres
```

The gateway's last lines should show a clean shutdown, and PostgreSQL's a
`database system is shut down` message. If a container remains `running` after
the timeout, inspect its log before escalating. A container that ignores
`SIGTERM` will be killed by the runtime after the grace period; that is the
only routine case where a forced stop is correct.

### Host reboot

All long-running services use `restart: unless-stopped`, so Docker or Podman
starts them again after the daemon starts. The dependency order in Compose is honoured on
start. After a reboot, check `$COMPOSE ps` and `/readyz`; the scheduler resumes
due work, and the backup worker resumes polling its queue. A deliberate `stop`
is remembered until the next explicit `up -d`.

## Native systemd deployment

The native deployment runs PostgreSQL 13 from the distribution and each service
under `patchtrack@.service`. Stop the application before the database, and
start the database before migrations and the application.

### Stop

```bash
sudo systemctl stop patchtrack@gateway
sudo systemctl stop patchtrack@reporting patchtrack@compliance \
  patchtrack@inventory patchtrack@identity
sudo systemctl stop postgresql-13
```

Each unit has `KillSignal=SIGTERM` and `TimeoutStopSec=30`. Uvicorn gets the
same graceful-shutdown behaviour as in a container; the scheduler stops within
about five seconds. If a unit does not stop within 30 seconds, systemd sends
`SIGKILL`; inspect `journalctl -u patchtrack@<service> -n 100` afterwards.

Do not use a glob (`systemctl stop 'patchtrack@*'`) for a planned stop: the
order is not guaranteed, and PostgreSQL could stop while a service is still
running. A glob is acceptable during an emergency, followed by a database
consistency check.

### Start

```bash
sudo systemctl start postgresql-13
sudo systemctl start patchtrack-migrate.service
sudo systemctl start patchtrack@identity patchtrack@inventory \
  patchtrack@compliance patchtrack@reporting patchtrack@gateway
```

`patchtrack-migrate.service` is a one-shot unit; starting it applies any
pending migrations. Each `patchtrack@` unit requires it, so systemd starts it
automatically when needed. Verify with:

```bash
systemctl status 'patchtrack@*' --no-pager
curl -fsSk https://127.0.0.1:8005/readyz
journalctl -u 'patchtrack@*' --since "10 minutes ago"
```

The native backup worker is not part of the default units. If it was installed
(the `backup` container equivalent), stop it before PostgreSQL and start it
after migrations, using the same order as the Compose worker.

### Restart one service after a configuration change

```bash
sudo systemctl restart patchtrack@gateway
journalctl -u patchtrack@gateway -f
```

`/etc/patchtrack/.env` is read at start, so a restart is required after
changing it. The per-instance port file is also read at start.

## Maintenance windows

| Change | Downtime? | Required action |
|---|---|---|
| Application configuration in `.env` | Brief | Recreate/restart affected services |
| Gateway certificate upload | None | Applies to new connections; no restart |
| SMTP relay saved in Settings | None | Applies to the next delivery |
| Retention or timezone saved in Settings | None | Applied by the next scheduler pass or report |
| New migration (upgrade) | Yes | `upgrade.sh` stops services, migrates, verifies and restarts |
| Database restore | Yes | Stop all writers first; see [backup-restore.md](backup-restore.md) |
| Rotating the JWT secret or service token | Brief | Restart every service that consumes it; users sign in again for the JWT |
| Rotating the PostgreSQL password | Brief | Update the secret file and restart every service, or the connection fails |
| Kernel/host reboot | Yes | Runtime restart policy brings the stack back; verify afterwards |

For a planned maintenance stop, disable the scheduler first if a long-running
job must not start during the window, or set the relevant job's enabled flag at
**Reporting → Schedules → System jobs**. A normal stop already interrupts new
scheduler work; disabling is useful when the window is long and jobs would
otherwise start immediately after the restart.

## Recovering from an ungraceful stop

If the host lost power or a process was killed:

1. Start PostgreSQL first and let crash recovery finish before starting the
   application. `pg_isready` or the Compose health check reports when it is
   accepting connections.
2. Start the stack normally. The `migrate` service verifies the schema.
3. Check `shared.scheduled_jobs` for a job whose `last_started_at` is newer
   than its `last_finished_at`, and `shared.backup_runs` for a `running`
   archive. The backup worker marks its own interrupted runs failed at
   startup; a scheduled job whose process died is retried because its
   `next_run_at` was not advanced when it did not finish.
4. Check **Operational health**, the Reports screen for `generating`, and the
   audit trail for the last events before the stop.
5. Generate a fresh report or backup if an interrupted one matters; do not
   reuse a partially written file.

Never delete a `-wal` file or the PostgreSQL data directory to "fix" a crash.
Let PostgreSQL recover, then use the documented restore procedure if the
database is genuinely corrupt.
