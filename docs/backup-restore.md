# Backup and restore

Two things must be backed up: the PostgreSQL database and the report volume.
Everything else (containers, images, configuration templates) can be rebuilt
from this repository plus `.env`.

PatchTrack can produce a **portable data archive** containing exactly those two
things, on a schedule and on demand, from the console. It is the recommended
starting point after a corruption or before a rebuild. The host-side commands
later in this document remain available for a database administrator who wants
full control of the dump and its destination.

## In-app data archive

An administrator opens **Administration → Data backups**. There is a
**Create backup now** button and a list of archives with their status, size,
version and SHA-256. Downloading an archive requires `settings:manage` and
writes a `backup.downloaded` audit event; requesting one writes
`backup.requested`.

### Schedule and on-demand requests

Each archive is produced by the `reporting.backup` system job. Its initial
cadence is `PATCHTRACK_BACKUP_INTERVAL` (24 hours by default) and the first
reporting start after installation or upgrade queues one promptly. Change the
interval or disable the job at **Reporting → Schedules → System jobs**; the
stored row wins over the environment default, like every other system job.
On-demand requests use the same queue, so a scheduled run and a button press
cannot produce two dumps at once. A second request while one is queued or
running is refused with `409`, and the screen shows which backup is active.

The reporting service only queues work. A dedicated `backup` container, built
from the same PostgreSQL image as the database, claims the request, runs
`pg_dump`, archives the report volume, and writes the file. Using that image is
deliberate: `pg_dump` must match the server major version, and the image is
already required and shipped in the air-gap bundle. The application image
carries no database client binaries. The worker rewrites a heartbeat every ten
seconds; the screen warns when it has not been seen for a minute, so "queued"
is distinguishable from "the worker is not running".

### What is in the archive

One `.tar.gz` named `patchtrack-backup-<UTC>-<id>.tar.gz`:

| Entry | Contents |
|---|---|
| `manifest.json` | Format version, PatchTrack release, highest migration, creation time, digests, report count, exclusions |
| `database.dump` | `pg_dump` custom format: every schema, table, sequence, index, constraint and data value |
| `reports.tar.gz` | The report volume (archived PDFs; half-written `*.tmp` files are excluded) |
| `SHA256SUMS` | SHA-256 of `database.dump` and `reports.tar.gz` |
| `RESTORE.txt` | Short restore checklist and the secret-handling caveat |

The database dump excludes **data** (not schema) for `identity.refresh_tokens`,
`identity.login_attempts`, `shared.backup_runs` and `shared.backup_worker`:
live sessions and the backup queue should not be revived on another host, and
the archive should not recursively contain itself. It includes user accounts
and Argon2 password hashes, so users keep their existing passwords after a
restore. It includes the SMTP password in its encrypted form, but **not** the
key that decrypts it.

The archive contains no `.env`, no JWT secret, no service token, no TLS private
key and no database password. Keep those in the organisation's secret store.

The database snapshot is transactionally consistent at the moment `pg_dump`
starts. A report generated after that point may exist as a file in the report
tar without a row in the dump, or be missing from the tar while its row is in
the dump. This is benign and is the same caveat as the host-side procedure in
section 3. Excluding `*.tmp` prevents a half-written PDF from being archived.

### Retention and storage

The worker keeps the newest *N* completed archives. The initial value is
`PATCHTRACK_BACKUP_RETENTION` (7) and the live value is edited on the same
screen. Lowering the count does not delete anything immediately; the worker
applies it after the next successful archive, so a backup write and a retention
sweep never race. The screen shows how many archives are waiting to be removed.
Failed attempts keep their row and error message but have no file; they are not
counted against the retention limit.

Size the `backups` volume for roughly *(database size + report volume size) ×
the retention count*. Archives are compressed, so this is an upper bound in
most installations. The in-app archive is not a substitute for off-host copies:
download important archives, or copy the `backups` volume to the organisation's
backup system. A corruption of the host still takes the archives with it.

### Verify a downloaded archive

```bash
mkdir -p /root/patchtrack-restore
cd /root/patchtrack-restore
tar -xzf /path/to/patchtrack-backup-20261029T163832Z-a121e430.tar.gz
sha256sum -c SHA256SUMS
```

Both lines must read `OK`. The outer archive's SHA-256 is shown on the Data
backups screen; compare it with the downloaded file if it crossed an untrusted
link. Read `manifest.json` and confirm `app_version` and `schema_version`
against the target installation — the "same version" rule means both.

### Restore an in-app archive

The archive carries a full schema, so it can rebuild a same-release database.
Restore into a **new** database first, verify it, and only then swap it in.
These commands assume the Compose project directory is `/opt/patchtrack` and the
database role/database are `patchtrack`; substitute the values from `.env` if
they differ. Add
`-f docker-compose.secrets.yml` to every `docker compose` command when the
deployment uses file-backed secrets, as the installer does.

1. **Extract and verify** as above, then stop every writer:

   ```bash
   cd /opt/patchtrack
   docker compose stop gateway identity inventory compliance reporting backup
   ```

   Stop Grafana as well if that profile is running; nothing else should hold a
   connection to the database.

2. **Restore into a new database.** Do not use `--clean` here: the target is a
   fresh database, and the dump creates every schema and object.

   ```bash
   docker compose exec -T postgres psql -U patchtrack -d postgres -c \
     "CREATE DATABASE patchtrack_restore OWNER patchtrack"

   docker compose exec -T postgres pg_restore -U patchtrack \
     -d patchtrack_restore --no-owner --no-privileges \
     < /root/patchtrack-restore/database.dump
   ```

3. **Verify before switching over.** The counts must match the estate at the
   backup time, and the migration must equal the manifest's `schema_version`:

   ```bash
   docker compose exec -T postgres psql -U patchtrack -d patchtrack_restore -c \
     "SELECT count(*) AS assets FROM inventory.assets;
      SELECT count(*) AS snapshots FROM compliance.snapshots;
      SELECT max(occurred_at) AS last_audit FROM shared.audit_events;
      SELECT max(version) AS schema_version FROM shared.schema_migrations;"
   ```

4. **Swap the databases**, keeping the old one until the new instance is
   checked:

   ```bash
   docker compose exec -T postgres psql -U patchtrack -d postgres <<'SQL'
   ALTER DATABASE patchtrack RENAME TO patchtrack_pre_restore;
   ALTER DATABASE patchtrack_restore RENAME TO patchtrack;
   SQL
   ```

5. **Restore the report volume.** Extract as root so the archived ownership and
   modes (`1001:0`, PDFs `0640`) are preserved. Find the volume name if the
   Compose project directory is not `patchtrack`:

   ```bash
   docker volume ls --format '{{.Name}}' | grep '_reports$'
   # example name: patchtrack_reports
   docker run --rm \
     -v patchtrack_reports:/data \
     -v /root/patchtrack-restore:/restore:ro \
     docker.io/library/postgres:17 \
     tar -xzf /restore/reports.tar.gz -C /data
   ```

   On a Podman host use `podman run` and `podman volume ls`.

6. **Start the stack and verify.** Migrations find the restored
   `shared.schema_migrations` already up to date.

   ```bash
   docker compose up -d
   curl -fsS https://patchtrack.example.com:8005/readyz
   ```

   Sign in with an account that existed at backup time, open the dashboard, and
   download a completed report. Its digest in the report table must still match
   the file — the reporting service already enforces that on download.

7. **Re-enter the SMTP password** at **Settings → Email delivery**. The stored
   value is encrypted with the old installation's `PATCHTRACK_SECRETS_KEY` (or
   JWT secret), which is intentionally not in the archive. Everything else
   works without it; report generation does not depend on email delivery.

8. Once satisfied, drop the old database and the extracted files:

   ```bash
   docker compose exec -T postgres psql -U patchtrack -d postgres -c \
     "DROP DATABASE patchtrack_pre_restore"
   ```

### Migrating to a new host

1. Install the **same PatchTrack release** on the new host and let it become
   ready. The temporary bootstrap administrator is only needed to sign in.
2. Copy the downloaded archive to the new host and follow the restore steps
   above.
3. The archive replaces the new host's users, integrations, policy, assets,
   snapshots and audit trail with the backed-up ones. The new host's `.env`,
   JWT secret, service token, TLS certificate and database password remain its
   own — do not copy the old host's secrets unless the organisation explicitly
   wants the old signing keys as well.
4. Re-enter the SMTP password as above, and re-check **Settings → General** for
   the display timezone, which is part of the database and therefore restored.

### Troubleshooting

| Symptom | Check |
|---|---|
| Backup stays `queued` | `docker compose ps backup`; `docker compose logs backup`. The screen's worker tile turns "Not running" when no heartbeat arrives for 60 seconds. |
| `failed` with a database error | The worker uses `PATCHTRACK_DATABASE_URL` (or its `_FILE`); confirm the database is healthy and the role can read every schema. The error is stored on the row. |
| `failed` with a tar error | Confirm the `reports` volume is mounted into the backup container and readable. Files being written concurrently are excluded via `*.tmp`. |
| Download says "file unavailable" | The row is complete but its file was removed outside PatchTrack. Restore the backups volume from an off-host copy; do not edit the digest. |
| Disk pressure | Lower the retention count, then wait for the next successful backup, or copy archives off-host and delete them deliberately. |
| Worker exits immediately after a `POSTGRES_IMAGE` change | The worker runs `psql`, `pg_dump`, `sha256sum` and `tar` from that image's `PATH`. The default `postgres:17` image has them; a vendor image must too. Use the default, or a mirror of it, for the `backup` service. |

## 1. What to back up

| Item | Contents | Frequency |
|------|----------|-----------|
| `patchtrack` database | Assets, assessments, findings, snapshots, users, audit trail, report metadata, policy versions | Daily |
| `reports` volume | Archived PDF bytes (the metadata and digests are in the database) | Daily, or after each report run |
| `/etc/patchtrack/.env` | Secrets, database URL | On change, to the secret store |
| Credential files | `/etc/patchtrack/secrets/*` | On change, to the secret store |
| Grafana volume | Grafana's own sqlite (dashboards are provisioned from code, so this is mostly user preferences) | Weekly, or skip |

The **audit trail is the record of who did what** and is append-only; treat the
database backup as a legal record and apply the organisation's retention
policy to it.

## 2. Database backup

Use `pg_dump` in custom format against the running container:

```bash
#!/usr/bin/env bash
# /usr/local/sbin/patchtrack-backup.sh
set -euo pipefail

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="/var/backups/patchtrack"
mkdir -p "$DEST"

cd /opt/patchtrack
docker compose exec -T postgres \
  pg_dump -U patchtrack -d patchtrack -Fc --no-owner --no-privileges \
  > "$DEST/patchtrack-db-$STAMP.dump"

docker run --rm \
  -v patchtrack_reports:/data:ro \
  -v "$DEST":/backup \
  registry.access.redhat.com/ubi9/ubi-minimal:latest \
  tar -czf "/backup/patchtrack-reports-$STAMP.tar.gz" -C /data .

sha256sum "$DEST/patchtrack-db-$STAMP.dump" \
          "$DEST/patchtrack-reports-$STAMP.tar.gz" \
  >> "$DEST/SHA256SUMS"

# 35 days of local backups; ship copies off-host as well.
find "$DEST" -name 'patchtrack-*' -mtime +35 -delete
```

Verify a backup by restoring it somewhere harmless (section 4) — at least
monthly, and always after a schema change.

## 3. Consistency

`pg_dump` gives a transactionally consistent snapshot of the database at the
moment it starts. Reports written after that point will exist as files without
metadata rows, or vice versa, if you back up while a report is generating.

The consequence is benign: a report row without its file is reported as
`completed` but the download returns 404; a file without a row is orphaned and
harmless. To avoid even that, run the report backup a minute after the report
scheduler tick, or briefly stop the reporting service during the backup:

```bash
docker compose stop reporting
# ... backup ...
docker compose start reporting
```

Where a report file and its row disagree after a restore, re-download the file
from the previous day's backup or regenerate from the snapshot.

## 4. Restore

### 4.1 Restore the database

Restore into a **new** database first and verify, rather than overwriting
production in place.

```bash
cd /opt/patchtrack
stamp=20261026T010000Z

# Stop writers so nothing changes during the restore.
docker compose stop gateway identity inventory compliance reporting

# Create an empty database owned by the application role.
docker compose exec -T postgres psql -U patchtrack -d postgres -c \
  "CREATE DATABASE patchtrack_restore OWNER patchtrack"

docker compose exec -T postgres pg_restore -U patchtrack -d patchtrack_restore \
  --no-owner --no-privileges --clean --if-exists \
  < /var/backups/patchtrack/patchtrack-db-$stamp.dump

# Verify before switching over.
docker compose exec -T postgres psql -U patchtrack -d patchtrack_restore -c \
  "SELECT count(*) AS assets FROM inventory.assets;
   SELECT count(*) AS snapshots FROM compliance.snapshots;
   SELECT max(occurred_at) AS last_audit FROM shared.audit_events;"
```

The expected counts should match the estate at the backup time. Then swap:

```bash
docker compose exec -T postgres psql -U patchtrack -d postgres <<'SQL'
ALTER DATABASE patchtrack RENAME TO patchtrack_old;
ALTER DATABASE patchtrack_restore RENAME TO patchtrack;
SQL

docker compose up -d
```

Keep `patchtrack_old` until the restored instance has been checked. Drop it
when satisfied.

The migrations table travels with the dump, so the restored database is already
at the backed-up schema version. Do **not** run migrations against a restored
database until you know which version it is:

```bash
docker compose exec -T postgres psql -U patchtrack -d patchtrack -c \
  "SELECT version, applied_at FROM shared.schema_migrations ORDER BY version;"
```

### 4.2 Restore the report volume

```bash
docker run --rm \
  -v patchtrack_reports:/data \
  -v /var/backups/patchtrack:/backup:ro \
  registry.access.redhat.com/ubi9/ubi-minimal:latest \
  tar -xzf /backup/patchtrack-reports-20261026T010000Z.tar.gz -C /data
```

Then re-verify integrity against the database — this is the point of storing the
digest:

```bash
docker compose exec -T reporting python - <<'PY'
import hashlib, pathlib
from patchtrack_common.db import fetch_all
bad = 0
for row in fetch_all("SELECT id, file_path, sha256 FROM reporting.reports "
                     "WHERE status = 'completed'"):
    p = pathlib.Path(row["file_path"])
    if not p.exists():
        print("missing", row["id"]); bad += 1; continue
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    if digest != row["sha256"]:
        print("digest mismatch", row["id"], digest, row["sha256"]); bad += 1
print("checked, problems:", bad)
PY
```

Any mismatch means the archive was altered or the restore was partial. Do not
overwrite the stored digest to make the check pass — investigate.

Note the report directory inside the container is the mounted volume at
`/var/lib/patchtrack/reports`; `file_path` rows store that container path, so
run the check inside the reporting container as above.

### 4.3 Restore secrets

Restore `/etc/patchtrack/.env` and credential files from the secret store with
mode 0600 and owner `patchtrack`. If the JWT secret was rotated since the
backup, do not restore the old one — issue new sessions.

## 5. Restore testing schedule

| Test | Frequency | Pass condition |
|------|-----------|----------------|
| Backup completes and digests verify | Every night (automated) | Exit 0, `sha256sum -c` OK |
| Restore into a scratch database | Monthly | Asset, snapshot and audit counts match; migrations table matches |
| Full restore to a scratch host | Quarterly | Sign-in works, dashboard renders, a report downloads and its digest matches |
| Restore the report volume | Quarterly | Every completed report's file digest matches the stored value |

## 6. Recovery objectives (guidance, not a guarantee)

| Objective | Suggested target |
|-----------|------------------|
| RPO (data loss) | 24 hours; 1 hour if you stream WAL archiving |
| RTO (time to service) | 2 hours for a database restore on the same host |

The estate can re-collect from the source systems, so a lost day of
assessments is recoverable. Snapshots and the audit trail are not — they are
the historical record.
