#!/usr/bin/env bash
# PatchTrack data-archive worker.
#
# Runs in a container built from the same PostgreSQL image as the database, so
# pg_dump always matches the server major version. It polls
# shared.backup_runs for a queued request and turns it into one self-describing
# archive:
#
#   manifest.json     format, application/schema version, digests, counts
#   database.dump     pg_dump custom format (schema + data)
#   reports.tar.gz    the report volume, excluding half-written *.tmp files
#   SHA256SUMS        digests of the two data files
#   RESTORE.txt       short restore instructions and the secret-handling caveat
#
# The archive deliberately excludes credentials and live session state:
# identity.refresh_tokens and identity.login_attempts data, the backup queue
# itself, and (by construction) .env, TLS keys and the database password.
# SMTP is the exception worth knowing about: the encrypted password travels in
# app_settings but cannot be decrypted on a new installation, so RESTORE.txt
# says to enter it again.
#
# Everything is written to a temporary directory on the backups volume and
# renamed into place only after every digest is known, so an interrupted run
# never leaves a file that looks complete.
set -Eeuo pipefail
umask 0077

BACKUP_DIR="${PATCHTRACK_BACKUP_DIR:-/var/lib/patchtrack/backups}"
REPORT_DIR="${PATCHTRACK_REPORT_DIR:-/var/lib/patchtrack/reports}"
UPLOAD_DIR="${PATCHTRACK_RESTORE_UPLOAD_DIR:-/var/lib/patchtrack/restore-uploads}"
POLL_SECONDS="${PATCHTRACK_BACKUP_POLL_SECONDS:-10}"
RETENTION_DEFAULT="${PATCHTRACK_BACKUP_RETENTION:-7}"
APP_VERSION="${PATCHTRACK_VERSION:-unknown}"
ONCE="${PATCHTRACK_BACKUP_ONCE:-0}"
STOP_REQUESTED=0

log() {
  printf '%s backup-worker: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

resolve_database_url() {
  local url="${PATCHTRACK_DATABASE_URL:-}"
  if [[ -z "$url" && -n "${PATCHTRACK_DATABASE_URL_FILE:-}" ]]; then
    if [[ ! -r "$PATCHTRACK_DATABASE_URL_FILE" ]]; then
      log "database URL file is not readable: $PATCHTRACK_DATABASE_URL_FILE" >&2
      return 2
    fi
    url="$(<"$PATCHTRACK_DATABASE_URL_FILE")"
  fi
  if [[ -z "$url" ]]; then
    log "PATCHTRACK_DATABASE_URL or PATCHTRACK_DATABASE_URL_FILE is required" >&2
    return 2
  fi
  # libpq does not understand SQLAlchemy's driver suffix.
  case "$url" in
    postgresql+psycopg://*) url="postgresql://${url#postgresql+psycopg://}" ;;
  esac
  printf '%s' "$url"
}

DB_URL="$(resolve_database_url)"
export PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-10}"
export PGAPPNAME="patchtrack-backup"
PSQL=(psql "$DB_URL" -X -q -A -t -v ON_ERROR_STOP=1)

case "$RETENTION_DEFAULT" in
  ''|*[!0-9]*) RETENTION_DEFAULT=7 ;;
esac
if [[ "$RETENTION_DEFAULT" -lt 1 ]]; then RETENTION_DEFAULT=1; fi

json_escape() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\t/\\t/g'
}

wait_for_database() {
  while ! "${PSQL[@]}" -c 'SELECT 1' >/dev/null 2>&1; do
    [[ "$STOP_REQUESTED" -eq 1 ]] && return 1
    log "waiting for the database"
    sleep 2 || true
  done
}

heartbeat() {
  # psql expands :'var' on standard input, not inside -c, so every command
  # carrying a value goes through a heredoc.
  "${PSQL[@]}" -v app="$APP_VERSION" >/dev/null <<'SQL'
INSERT INTO shared.backup_worker (singleton, last_seen_at, worker_version)
VALUES (true, now(), :'app')
ON CONFLICT (singleton) DO UPDATE SET
    last_seen_at = now(), worker_version = EXCLUDED.worker_version;
SQL
}

claim_request() {
  "${PSQL[@]}" -F '|' -c "
    WITH candidate AS (
        SELECT id FROM shared.backup_runs
        WHERE status = 'queued'
        ORDER BY requested_at, id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    UPDATE shared.backup_runs b
       SET status = 'running', started_at = now(), message = ''
      FROM candidate c
     WHERE b.id = c.id
    RETURNING b.id, b.trigger_kind, b.requested_by"
}

finish_failed() {
  local id="$1" message="$2"
  message="${message:0:1000}"
  "${PSQL[@]}" -v id="$id" -v message="$message" >/dev/null <<'SQL' || true
UPDATE shared.backup_runs
   SET status = 'failed', finished_at = now(), message = :'message'
 WHERE id = :'id'::uuid;
SQL
}

purge_old_archives() {
  local retention rows rid fname base
  retention="$("${PSQL[@]}" -c "
      SELECT COALESCE((value->>'count')::int, $RETENTION_DEFAULT)
        FROM shared.app_settings WHERE key = 'backup.retention'")"
  retention="${retention:-$RETENTION_DEFAULT}"
  case "$retention" in ''|*[!0-9]*) retention="$RETENTION_DEFAULT" ;; esac
  if [[ "$retention" -lt 1 ]]; then retention=1; fi

  rows="$("${PSQL[@]}" -F '|' -c "
      SELECT id, COALESCE(filename, '')
        FROM shared.backup_runs
       WHERE status = 'completed'
       ORDER BY finished_at DESC NULLS LAST, requested_at DESC
       OFFSET $retention")"
  while IFS='|' read -r rid fname; do
    [[ -z "$rid" ]] && continue
    base="${fname##*/}"
    if [[ -n "$fname" && "$base" == "$fname" ]]; then
      rm -f -- "$BACKUP_DIR/$fname"
    fi
    "${PSQL[@]}" -v id="$rid" >/dev/null <<'SQL'
DELETE FROM shared.backup_runs WHERE id = :'id'::uuid;
SQL
    log "retention removed $fname"
  done <<<"$rows"
}

create_archive() {
  local id="$1" work="$2"
  local created stamp schema_version report_count dump_size reports_size
  local dump_hash reports_hash final_name final_path size digest
  local app_escaped schema_escaped

  created="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  schema_version="$("${PSQL[@]}" -c \
    "SELECT COALESCE(max(version), 'unknown') FROM shared.schema_migrations")"
  [[ -n "$schema_version" ]] || schema_version="unknown"

  if [[ ! -d "$REPORT_DIR" ]]; then
    log "report directory does not exist: $REPORT_DIR" >&2
    return 1
  fi

  rm -rf -- "$work"
  mkdir -p -- "$work"

  log "dumping the database"
  pg_dump "$DB_URL" \
    --format=custom --no-owner --no-privileges \
    --lock-wait-timeout=60s \
    --exclude-table-data=identity.refresh_tokens \
    --exclude-table-data=identity.login_attempts \
    --exclude-table-data=shared.backup_runs \
    --exclude-table-data=shared.backup_worker \
    --file="$work/database.dump"

  log "archiving report files"
  report_count="$(find "$REPORT_DIR" -type f ! -name '*.tmp' | wc -l)"
  report_count="$(printf '%s' "$report_count" | tr -d '[:space:]')"
  tar -czf "$work/reports.tar.gz" --exclude='*.tmp' -C "$REPORT_DIR" .

  dump_size="$(stat -c %s "$work/database.dump")"
  reports_size="$(stat -c %s "$work/reports.tar.gz")"
  dump_hash="$(sha256sum "$work/database.dump" | cut -d' ' -f1)"
  reports_hash="$(sha256sum "$work/reports.tar.gz" | cut -d' ' -f1)"

  app_escaped="$(json_escape "$APP_VERSION")"
  schema_escaped="$(json_escape "$schema_version")"
  cat >"$work/manifest.json" <<EOF
{
  "format": "patchtrack-data-archive",
  "format_version": 1,
  "application": "PatchTrack",
  "app_version": "$app_escaped",
  "schema_version": "$schema_escaped",
  "backup_id": "$id",
  "created_at": "$created",
  "database": {
    "file": "database.dump",
    "format": "pg_dump-custom",
    "size_bytes": $dump_size,
    "sha256": "$dump_hash"
  },
  "reports": {
    "file": "reports.tar.gz",
    "format": "tar+gzip",
    "file_count": $report_count,
    "size_bytes": $reports_size,
    "sha256": "$reports_hash"
  },
  "excluded_table_data": [
    "identity.refresh_tokens",
    "identity.login_attempts",
    "shared.backup_runs",
    "shared.backup_worker"
  ],
  "restore": "Restore only into a PatchTrack installation of the same release. See RESTORE.txt and docs/backup-restore.md."
}
EOF

  cat >"$work/RESTORE.txt" <<EOF
PatchTrack data archive
=======================
Application version : $APP_VERSION
Schema version      : $schema_version
Created (UTC)       : $created
Backup ID           : $id

Verify before use:
    sha256sum -c SHA256SUMS

Restore only into a PatchTrack installation of the same release (compare the
application and schema versions above). Full steps are in
docs/backup-restore.md, section "Restore an in-app data archive".

Short form, as root from the PatchTrack application directory:
  1. Stop writers:
       docker compose stop gateway identity inventory compliance reporting backup
  2. Restore the database into a new database, verify it, then rename it over
     the old one. Do not restore straight over a running database.
  3. Extract reports.tar.gz into the reports volume.
  4. Start the stack with: docker compose up -d

Secrets are deliberately not in this archive: no .env, JWT signing secret,
service token, TLS private key or database password. The database does contain
password hashes and the SMTP password encrypted with the previous
installation's PATCHTRACK_SECRETS_KEY (or JWT secret); that key is not here, so
re-enter the SMTP password after restoring. Users sign in with their existing
passwords because the password hashes are part of the database.
EOF

  ( cd "$work" && sha256sum database.dump reports.tar.gz > SHA256SUMS )
  tar -czf "$work/archive.tar.gz" -C "$work" \
    manifest.json database.dump reports.tar.gz SHA256SUMS RESTORE.txt

  final_name="patchtrack-backup-${stamp}-${id:0:8}.tar.gz"
  final_path="$BACKUP_DIR/$final_name"
  mv -- "$work/archive.tar.gz" "$final_path"
  chmod 0640 "$final_path"
  chgrp 0 "$final_path" 2>/dev/null || true

  size="$(stat -c %s "$final_path")"
  digest="$(sha256sum "$final_path" | cut -d' ' -f1)"

  if ! "${PSQL[@]}" \
      -v id="$id" -v filename="$final_name" -v size="$size" -v sha="$digest" \
      -v app="$APP_VERSION" -v schema="$schema_version" \
      -v reports="$report_count" >/dev/null <<'SQL'
UPDATE shared.backup_runs SET
    status = 'completed', finished_at = now(),
    filename = :'filename', size_bytes = :'size'::bigint,
    sha256 = :'sha', app_version = :'app',
    schema_version = :'schema', report_files = :'reports'::integer,
    message = ''
WHERE id = :'id'::uuid;
SQL
  then
    # Do not leave an archive that the database does not claim.
    rm -f -- "$final_path"
    return 1
  fi

  log "completed $final_name ($size bytes, $report_count report file(s))"
}

run_request() {
  local id="$1" work="$2" logfile="$3" message
  if create_archive "$id" "$work" >"$logfile" 2>&1; then
    rm -f -- "$logfile"
    log "backup $id completed"
    purge_old_archives || log "retention cleanup failed; it will retry after the next backup"
    return 0
  fi
  message="$(tail -c 800 "$logfile" 2>/dev/null | tr '\n' ' ' \
    | sed -e 's/[[:space:]]\{1,\}/ /g')"
  [[ -n "$message" ]] || message="The backup worker could not create the archive."
  log "backup $id failed: $message"
  finish_failed "$id" "$message"
  rm -f -- "$logfile"
  return 1
}

# A container restart is the only way a row can be left 'running': there is a
# single worker. Mark those attempts failed so a new request is not blocked
# forever, and discard half-written temporary directories.
recover_interrupted() {
  "${PSQL[@]}" -c "
      UPDATE shared.backup_runs
         SET status = 'failed', finished_at = now(),
             message = 'The backup worker restarted before this archive completed.'
       WHERE status = 'running'" >/dev/null
  "${PSQL[@]}" -c "
      UPDATE shared.restore_runs
         SET status = 'failed', finished_at = now(),
             message = 'The backup worker restarted before this restore completed.'
       WHERE status = 'running'" >/dev/null 2>&1 || true
  mkdir -p -- "$BACKUP_DIR/.tmp"
  find "$BACKUP_DIR/.tmp" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
  chmod 0750 "$BACKUP_DIR"
  chgrp 0 "$BACKUP_DIR" 2>/dev/null || true
  # The uploads volume is created root-owned by the runtime, but reporting runs
  # as uid 1001 and writes the files. This worker runs as root with CHOWN.
  mkdir -p -- "$UPLOAD_DIR"
  chown 1001:0 "$UPLOAD_DIR" 2>/dev/null || true
  chmod 0770 "$UPLOAD_DIR"
}

# ------------------------------------------------------------------ restore
# A restore replaces the live database and the report volume with an archive's
# contents. It runs here because this is the only container with pg_restore at
# the server's major version, and because it can terminate the application's
# connections without a second service to coordinate with. The application
# engines use pool_pre_ping, so they reconnect to the restored database.
DB_BASE="${DB_URL%/*}"
DB_NAME="${DB_URL%%\?*}"
DB_NAME="${DB_NAME##*/}"
ADMIN_URL="${DB_BASE}/postgres"

claim_restore() {
  "${PSQL[@]}" -F '|' -c "
    WITH candidate AS (
        SELECT id FROM shared.restore_runs
        WHERE status = 'queued'
        ORDER BY requested_at, id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    UPDATE shared.restore_runs r
       SET status = 'running', started_at = now(), message = ''
      FROM candidate c
     WHERE r.id = c.id
    RETURNING r.id, r.backup_id, r.filename, r.requested_by, r.requested_at,
              r.source, r.sha256, r.display_name"
}

finish_restore_failed() {
  local id="$1" message="$2"
  message="${message:0:1000}"
  "${PSQL[@]}" -v id="$id" -v message="$message" >/dev/null <<'SQL' || true
UPDATE shared.restore_runs
   SET status = 'failed', finished_at = now(), message = :'message'
 WHERE id = :'id'::uuid;
SQL
}

manifest_value() {
  # The manifest is generated here with one value per line, so a targeted sed
  # avoids requiring a JSON parser the PostgreSQL image does not ship.
  sed -n "s/^  \"$1\": \"\\([^\"]*\\)\",\{0,1\}$/\\1/p" "$2" | head -n 1
}

schema_number() {
  # Migrations are named "021_restore_runs": compare the numeric prefix, so
  # "9_x" sorts before "021_x" and a name is not rejected for having a suffix.
  local n
  n="$(printf '%s' "${1:-}" \
    | sed -n 's/^[[:space:]]*0*\([0-9][0-9]*\).*/\1/p')"
  if [[ -n "$n" ]]; then printf '%s' "$((10#$n))"; fi
}

drop_database() {
  local name="$1"
  psql "$ADMIN_URL" -X -q -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE IF EXISTS \"$name\"" >/dev/null 2>&1 || true
}

swap_database() {
  local old_db="$1" new_db="$2" attempt
  for attempt in 1 2 3 4 5; do
    # Renaming requires no other session on either database. Application
    # connections are pre-pinged, so terminating them is safe: the services
    # reconnect to the same name once the swap is done.
    psql "$ADMIN_URL" -X -q -c \
      "SELECT pg_terminate_backend(pid) FROM pg_stat_activity \
        WHERE datname = '$DB_NAME' AND pid <> pg_backend_pid()" >/dev/null || true
    if psql "$ADMIN_URL" -X -q -v ON_ERROR_STOP=1 \
        -c "ALTER DATABASE \"$DB_NAME\" RENAME TO \"$old_db\"" >/dev/null 2>&1; then
      if psql "$ADMIN_URL" -X -q -v ON_ERROR_STOP=1 \
          -c "ALTER DATABASE \"$new_db\" RENAME TO \"$DB_NAME\"" >/dev/null 2>&1; then
        return 0
      fi
      # Put the original back rather than leave the installation without a
      # database under its configured name.
      psql "$ADMIN_URL" -X -q -c \
        "ALTER DATABASE \"$old_db\" RENAME TO \"$DB_NAME\"" >/dev/null 2>&1 || true
      return 1
    fi
    sleep 1
  done
  return 1
}

record_restore_completed() {
  # The archive's own restore_runs data may predate this table, so make sure it
  # exists before writing the outcome into the restored database. Columns added
  # after 021 are added here too, so an older archive can still record the run.
  "${PSQL[@]}" -v id="$1" -v backup_id="$2" -v filename="$3" \
      -v by="$4" -v requested_at="$5" -v message="$6" \
      -v source="$7" -v sha="$8" -v display="$9" >/dev/null <<'SQL' || return 1
CREATE SCHEMA IF NOT EXISTS shared;
CREATE TABLE IF NOT EXISTS shared.restore_runs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    backup_id       uuid,
    source          text NOT NULL DEFAULT 'archive',
    filename        text NOT NULL DEFAULT '',
    display_name    text NOT NULL DEFAULT '',
    sha256          text NOT NULL DEFAULT '',
    status          text NOT NULL DEFAULT 'queued',
    requested_by    text NOT NULL DEFAULT '',
    requested_at    timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    message         text NOT NULL DEFAULT ''
);
ALTER TABLE shared.restore_runs
    ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'archive';
ALTER TABLE shared.restore_runs
    ADD COLUMN IF NOT EXISTS sha256 text NOT NULL DEFAULT '';
ALTER TABLE shared.restore_runs
    ADD COLUMN IF NOT EXISTS display_name text NOT NULL DEFAULT '';
INSERT INTO shared.restore_runs
    (id, backup_id, source, filename, display_name, sha256, status,
     requested_by, requested_at, started_at, finished_at, message)
VALUES (:'id'::uuid, NULLIF(:'backup_id', '')::uuid, :'source', :'filename',
        :'display', :'sha', 'completed', :'by', :'requested_at'::timestamptz,
        now(), now(), :'message')
ON CONFLICT (id) DO UPDATE SET
    status = 'completed', finished_at = now(), message = EXCLUDED.message;
SQL
}

copy_backup_history() {
  local old_db="$1" old_url="$DB_BASE/$old_db"
  # backup_runs data is excluded from archives, so a restored installation
  # would otherwise forget which archives exist on its own volume.
  for table in shared.backup_runs shared.backup_worker; do
    pg_dump --dbname="$old_url" --data-only --no-owner --no-privileges \
      --table="$table" 2>/dev/null \
      | "${PSQL[@]}" >/dev/null 2>&1 || true
  done
}

restore_archive() {
  local id="$1" backup_id="$2" filename="$3" requested_by="$4"
  local requested_at="$5" source="$6" sha="$7" display="$8" work="$9"
  local archive path expected actual m_app m_schema current_schema
  local stamp old_db new_db message warning="" label

  label="${display:-$filename}"
  if [[ "$source" == "upload" ]]; then
    path="$UPLOAD_DIR/$filename"
  else
    path="$BACKUP_DIR/$filename"
  fi
  if [[ -z "$filename" || "${filename##*/}" != "$filename" || ! -f "$path" ]]; then
    finish_restore_failed "$id" "The archive file is missing from the server."
    return 1
  fi

  if [[ "$source" == "upload" ]]; then
    expected="$sha"
  else
    expected="$("${PSQL[@]}" -c \
      "SELECT COALESCE(sha256, '') FROM shared.backup_runs WHERE id = '$backup_id'::uuid")"
  fi
  actual="$(sha256sum "$path" | cut -d' ' -f1)"
  if [[ -z "$expected" || "$expected" != "$actual" ]]; then
    finish_restore_failed "$id" "The archive's SHA-256 does not match its record."
    return 1
  fi

  rm -rf -- "$work"
  mkdir -p -- "$work/extract"
  if ! tar -xzf "$path" -C "$work/extract"; then
    finish_restore_failed "$id" "The archive could not be extracted."
    return 1
  fi
  if ! ( cd "$work/extract" && sha256sum -c SHA256SUMS ) >"$work/verify.log" 2>&1; then
    finish_restore_failed "$id" "The archive's contents failed digest verification."
    return 1
  fi

  m_app="$(manifest_value app_version "$work/extract/manifest.json")"
  m_schema="$(manifest_value schema_version "$work/extract/manifest.json")"
  current_schema="$("${PSQL[@]}" -c \
    "SELECT COALESCE(max(version), 'unknown') FROM shared.schema_migrations")"
  if [[ "$m_app" != "$APP_VERSION" ]]; then
    finish_restore_failed "$id" \
      "The archive is from PatchTrack ${m_app:-unknown}, not $APP_VERSION."
    return 1
  fi
  if [[ -z "$(schema_number "$m_schema")" || -z "$(schema_number "$current_schema")" \
        || "$(schema_number "$m_schema")" -gt "$(schema_number "$current_schema")" ]]; then
    finish_restore_failed "$id" \
      "The archive's schema (${m_schema:-unknown}) is newer or unreadable."
    return 1
  fi

  # Unpack the report files before touching anything live, so a corrupt tar
  # cannot leave the installation half-restored.
  mkdir -p -- "$work/reports"
  if ! tar -xzf "$work/extract/reports.tar.gz" -C "$work/reports"; then
    finish_restore_failed "$id" "The report archive could not be extracted."
    return 1
  fi

  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  old_db="patchtrack_pre_restore_${stamp}"
  new_db="patchtrack_restore_${id:0:8}"
  drop_database "$new_db"
  if ! psql "$ADMIN_URL" -X -q -v ON_ERROR_STOP=1 \
      -c "CREATE DATABASE \"$new_db\"" >/dev/null; then
    finish_restore_failed "$id" "The restore database could not be created."
    return 1
  fi
  if ! pg_restore --dbname="$DB_BASE/$new_db" --no-owner --no-privileges \
      "$work/extract/database.dump" >"$work/restore.log" 2>&1; then
    drop_database "$new_db"
    finish_restore_failed "$id" "pg_restore reported an error; see the worker log."
    return 1
  fi

  # The swap is the point of no easy return. Keep a copy of the live report
  # volume beside the archives so a failed report restore can be undone by an
  # administrator.
  if ! tar -czf "$BACKUP_DIR/pre-restore-reports-${stamp}.tar.gz" \
      -C "$REPORT_DIR" . >/dev/null 2>&1; then
    drop_database "$new_db"
    finish_restore_failed "$id" "The pre-restore report snapshot could not be written."
    return 1
  fi
  if ! swap_database "$old_db" "$new_db"; then
    drop_database "$new_db"
    finish_restore_failed "$id" \
      "The database could not be swapped; the live database is unchanged."
    return 1
  fi

  if find "$REPORT_DIR" -mindepth 1 -delete 2>/dev/null; then
    if ! tar -xzf "$work/extract/reports.tar.gz" -C "$REPORT_DIR"; then
      warning=" The report volume could not be replaced; pre-restore-reports-${stamp}.tar.gz holds the previous files."
    fi
  else
    warning=" The report volume could not be cleared; pre-restore-reports-${stamp}.tar.gz holds the previous files."
  fi

  copy_backup_history "$old_db" || true
  message="Restored from $label. The previous database is $old_db; drop it after verification."
  if ! record_restore_completed "$id" "$backup_id" "$filename" \
      "$requested_by" "$requested_at" "$message" "$source" "$actual" "$label"; then
    log "restore $id completed but its status row could not be written"
  fi
  rm -rf -- "$work"
  log "restore $id completed from $label; previous database $old_db${warning}"
  return 0
}

run_restore() {
  local id="$1" backup_id="$2" filename="$3" requested_by="$4"
  local requested_at="$5" source="$6" sha="$7" display="$8"
  local work="$BACKUP_DIR/.tmp/restore-$id"
  log "starting restore $id from ${display:-${filename:-unknown}}"
  local ok=1
  if restore_archive "$id" "$backup_id" "$filename" "$requested_by" \
      "$requested_at" "$source" "$sha" "$display" "$work"; then
    ok=0
  fi
  rm -rf -- "$work"
  # An uploaded file was for this one attempt; the queue is the record, and
  # retaining it would grow the upload volume with no way to browse it.
  if [[ "$source" == "upload" ]]; then
    rm -f -- "$UPLOAD_DIR/$filename"
  fi
  return "$ok"
}

main() {
  # A container stop is a SIGTERM to this script. Finish the dump or tar that
  # is already running, record it, and exit at the top of the loop. If the
  # runtime's grace period expires first, the run stays 'running' until the
  # next start marks it failed and removes the temporary directory.
  trap 'STOP_REQUESTED=1; log "stop requested; finishing the current operation"' \
    TERM INT

  if ! wait_for_database; then
    log "stopping before the database became available"
    return 0
  fi
  recover_interrupted
  purge_old_archives || log "retention cleanup failed at startup"

  if [[ "$ONCE" == "1" ]]; then
    heartbeat || true
    local row id trigger_kind requested_by
    if ! row="$(claim_request)"; then
      log "could not read the backup queue" >&2
      return 1
    fi
    [[ -n "$row" ]] || return 0
    IFS='|' read -r id trigger_kind requested_by <<<"$row"
    run_request "$id" "$BACKUP_DIR/.tmp/$id" "$BACKUP_DIR/.tmp/$id.log"
    return $?
  fi

  local row id trigger_kind requested_by
  while [[ "$STOP_REQUESTED" -eq 0 ]]; do
    if ! heartbeat; then
      log "database heartbeat failed; retrying"
      sleep "$POLL_SECONDS" || true
      continue
    fi
    if ! row="$(claim_request)"; then
      log "could not read the backup queue; retrying"
      sleep "$POLL_SECONDS" || true
      continue
    fi
    if [[ -z "$row" ]]; then
      local restore_row rid rbackup rfilename rby rreq rsource rsha rdisplay
      if restore_row="$(claim_restore 2>/dev/null)"; then
        if [[ -n "$restore_row" ]]; then
          IFS='|' read -r rid rbackup rfilename rby rreq rsource rsha rdisplay \
            <<<"$restore_row"
          log "starting restore $rid requested by ${rby:-unknown}"
          run_restore "$rid" "$rbackup" "$rfilename" "$rby" "$rreq" \
            "$rsource" "$rsha" "$rdisplay" || true
          continue
        fi
      else
        log "could not read the restore queue; retrying"
      fi
      sleep "$POLL_SECONDS" || true
      continue
    fi
    [[ "$STOP_REQUESTED" -eq 1 ]] && break
    IFS='|' read -r id trigger_kind requested_by <<<"$row"
    log "starting $trigger_kind backup $id requested by ${requested_by:-unknown}"
    run_request "$id" "$BACKUP_DIR/.tmp/$id" "$BACKUP_DIR/.tmp/$id.log" || true
  done
  log "stopping"
}

main "$@"
