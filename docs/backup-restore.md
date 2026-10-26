# Backup and restore

Two things must be backed up: the PostgreSQL database and the report volume.
Everything else (containers, images, configuration templates) can be rebuilt
from this repository plus `.env`.

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
