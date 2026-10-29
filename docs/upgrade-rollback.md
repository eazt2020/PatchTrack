# Upgrade and rollback

## 1. Model

- Code and schema move together, one image tag, one migration set.
- Migrations are forward-only and checksummed. An applied migration is never
  edited; a new one is added.
- An upgrade is: back up → load the new image → run migrations → start → verify.
- A rollback is: restore the backup → run the previous image.
- `upgrade.sh` performs those steps from the unpacked new release, including
  the backup and automatic rollback; the manual commands below remain the
  reference for what it does.

A schema change that cannot be undone by restoring a backup is not allowed in
a release. Every migration in this repository is additive (`CREATE TABLE IF NOT
EXISTS`, `ADD CONSTRAINT`, `CREATE INDEX`); none drops a column or rewrites
data.

## 2. Before you start

- [ ] Read the release notes ([release-notes.md](release-notes.md)) for
      migration numbers and required configuration changes.
- [ ] Take a verified backup ([backup-restore.md](backup-restore.md)).
- [ ] Note the currently running image tag and the migrations applied.
- [ ] Have the previous image **on the host** (`docker image ls`), because an
      air-gapped host cannot pull it back.
- [ ] Announce the maintenance window. The gateway returns 503 for a few
      seconds per service restart; in-flight report generations fail and must
      be retried.

```bash
docker compose ps --format '{{.Service}}\t{{.Image}}\t{{.Status}}'
docker compose exec -T postgres psql -U patchtrack -d patchtrack -c \
  "SELECT version FROM shared.schema_migrations ORDER BY version;"
```

## 3. Guided upgrade

Run the new release's `upgrade.sh` as root from the directory where you
unpacked it. Do **not** unpack it over `/opt/patchtrack`; the script compares
the source and target paths and refuses to continue if they overlap.

```bash
sudo ./upgrade.sh --version 3.1.0 --check   # preflight only
sudo ./upgrade.sh --version 3.1.0           # asks for confirmation
sudo ./upgrade.sh --version 3.1.0 --yes     # non-interactive
```

`--version` defaults to `PATCHTRACK_UPGRADE_VERSION`, then
`PATCHTRACK_VERSION`. The target must differ from the installed version and
must not be older; a downgrade needs a database restore, not an upgrade.

The prechecks read `/etc/patchtrack/install.json`, so they know the runtime,
image, account and directories the installation created. They require root, a
healthy current installation (all four services ready), a running PostgreSQL
container, the service account, the container runtime, free space and a source
tree that looks like a PatchTrack release. They also refuse existing
`patchtrack.upgrade-new` or `patchtrack.upgrade-old` staging directories, so a
previous interrupted run must be inspected before trying again.

The backup is mandatory and is written to
`/var/lib/patchtrack/upgrades/upgrade-<timestamp>/`:

| Item | Contents |
|------|----------|
| `app/` | Previous application tree |
| `config/` | `.env`, `install.json`, secrets and any operator files |
| `database.dump` | PostgreSQL custom-format dump taken with `pg_dump -Fc` |
| `upgrade.json` | From/to version, image, paths and creation time |

Backups are never deleted automatically. The dump contains the whole
`patchtrack` database; the database password stays in the existing secret
files and is not passed on a command line.

The upgrade then stages the new tree beside the current one, swaps it into
place, changes `PATCHTRACK_VERSION`, builds `patchtrack:<version>`, stops the
five application services (PostgreSQL stays up), runs the `migrate` service
and starts the stack. It verifies `/readyz`, `/healthz`, the sign-in page and
the migration table, then records the version in `install.json` and removes
the previous tree. The previous image is kept on the host.

If any step after staging fails, the script stops the new services, restores
the previous tree, `.env` and `install.json`, and restores the database when
migrations had started. It renames the failed database to
`patchtrack_failed_upgrade_<timestamp>`, creates a fresh `patchtrack` database
and restores into it from `database.dump`, so tables introduced by the failed
release cannot survive the rollback and the failed state remains available for
investigation. It then rebuilds the previous image if it is missing, starts the
previous version and waits for it to become ready. If the rollback itself
cannot complete, it prints the exact backup paths and database names for manual
recovery and leaves them in place. It never claims success when a check
failed.

Post-upgrade checks use the gateway directly on the bind address and port
recorded in `.env`; like the installer, they skip proxy settings and accept
the generated certificate. They do not create or sign in an administrator.

## 4. Manual upgrade

Use this when upgrading without the script, for example from a signed bundle
that predates `upgrade.sh`.

```bash
cd /opt/patchtrack
old_version=$(grep -oP 'PATCHTRACK_VERSION=\K.*' .env || echo 3.0.2)
echo "$old_version" > /var/lib/patchtrack/previous-version

# 1. Back up
/usr/local/sbin/patchtrack-backup.sh

# 2. Load the new image
docker load -i /srv/bundles/patchtrack-3.1.0/patchtrack-app-3.1.0.tar

# 3. Point compose at it
sudo sed -i 's/^PATCHTRACK_VERSION=.*/PATCHTRACK_VERSION=3.1.0/' /etc/patchtrack/.env

# 4. Apply migrations while nothing writes
docker compose stop gateway identity inventory compliance reporting
docker compose run --rm migrate

# 5. Start everything
docker compose up -d --pull never
docker compose ps
```

If a migration fails, the runner aborts the transaction and leaves the schema
unchanged. Fix the cause (a privilege problem, a lock) and re-run; do not edit
the migration file.

## 5. Verify the upgrade

| Check | How |
|-------|-----|
| Services healthy | `docker compose ps` all `healthy` |
| Gateway responding | `curl -fsSk https://127.0.0.1:8005/healthz` |
| Sign-in works | Sign in in a browser |
| Figures unchanged | `GET /api/compliance/summary` matches the value recorded before the upgrade |
| Reports still download | Download the most recent report; its SHA-256 matches `reporting.reports.sha256` |
| Scheduled jobs resume | Next `jobs/tick` returns `processed` without errors |
| Migrations applied | The new version appears in `shared.schema_migrations` |
| No errors | `docker compose logs --since 15m | grep -i error` |

The compliance figures must not change as a result of an upgrade alone. A
change means the upgrade altered evaluation semantics; that belongs in the
release notes and in a policy version, not in a silent code change. Stop and
investigate.

## 6. Rollback

```bash
cd /opt/patchtrack
previous=$(cat /var/lib/patchtrack/previous-version)

docker compose stop gateway identity inventory compliance reporting

# Restore the database taken before the upgrade.
docker compose exec -T postgres psql -U patchtrack -d postgres <<'SQL'
ALTER DATABASE patchtrack RENAME TO patchtrack_failed_upgrade;
CREATE DATABASE patchtrack_restore OWNER patchtrack;
SQL
docker compose exec -T postgres pg_restore -U patchtrack -d patchtrack_restore \
  --no-owner --no-privileges < /var/backups/patchtrack/patchtrack-db-<stamp>.dump
docker compose exec -T postgres psql -U patchtrack -d postgres <<'SQL'
ALTER DATABASE patchtrack RENAME TO patchtrack_new_unverified;
ALTER DATABASE patchtrack_restore RENAME TO patchtrack;
SQL

sudo sed -i "s/^PATCHTRACK_VERSION=.*/PATCHTRACK_VERSION=$previous/" /etc/patchtrack/.env
docker compose up -d --pull never
```

Rolling back the image **without** restoring the database is supported only if
the new release added migrations that the old code ignores. In practice, always
restore. Keeping the failed database as `patchtrack_failed_upgrade` preserves
evidence for the vendor.

### What rollback costs

- Any reports generated after the backup are lost, unless you restore the
  report volume from the same point or re-generate them from snapshots.
- Any collection performed after the backup repeats on the next sync.
- The audit trail returns to the backup point. Export the post-upgrade audit
  rows first if an investigation is under way:

  ```bash
  docker compose exec -T postgres psql -U patchtrack -d patchtrack -c \
    "COPY (SELECT * FROM shared.audit_events WHERE occurred_at > '<backup time>')
     TO STDOUT WITH (FORMAT csv, HEADER)" > /var/backups/patchtrack/audit-post-upgrade.csv
  ```

## 7. Rolling back a single service

Because all five services share one image and one database schema, rolling back
one service independently is only safe for gateway and identity (no schema
ownership conflict). For inventory/compliance/reporting, roll back the whole
set or not at all.

## 8. Configuration changes that accompany an upgrade

An upgrade may add a required environment variable. The release notes list
them; the pattern is always "add it with a safe default" so an operator who
misses it gets a working system, not a silent behaviour change.

## 9. Version numbering

`MAJOR.MINOR.PATCH`:

- **MAJOR** — a change to compliance semantics, or a migration that cannot be
  reversed by restoring a backup.
- **MINOR** — new capability, new additive migration.
- **PATCH** — fix with no schema change and no change to any published figure.

The image tag is the release identity; `PATCHTRACK_VERSION` in `.env` must match
it.
