#!/usr/bin/env bash
# Scheduled jobs for PatchTrack, driven by systemd (patchtrack-tick.timer).
#
# This is the only supported scheduler. There is deliberately no cron entry:
# systemd gives the job a defined environment, a failure state, journald
# output and a persistent run after downtime, rather than a mail spool.
#
# Each due job is delegated to scripts/patchtrack-job.sh, which owns the
# transport. That keeps container and native installations on the same timer:
# the job script sends the request inside the service's container when one is
# running, and to 127.0.0.1:$PORT otherwise. Every request presents the shared
# service token (from /etc/patchtrack/.env) and the `scheduler` principal —
# never a human account, and never the gateway, whose /api routes need a
# browser session cookie.
#
# Every step is skipped when it is not yet due, so one hourly timer covers all
# cadences:
#
#   PATCHTRACK_SYNC_ASSETEXPLORER_INTERVAL   21600   (6h)
#   PATCHTRACK_SYNC_SATELLITE_INTERVAL       21600
#   PATCHTRACK_SYNC_SCCM_INTERVAL            86400
#   PATCHTRACK_SYNC_SOLARIS_INTERVAL         604800
#
# The daily snapshot and audit retention run once a day. The report tick runs
# on every firing and is idempotent per schedule and period.
# Job state is kept in /var/lib/patchtrack/state so a restart does not cause a
# burst of catch-up collections.
#
# Exit status is non-zero if any step failed, so `systemctl status` and
# journalctl show the failure instead of a silent log line.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JOB="${PATCHTRACK_JOB_SCRIPT:-$ROOT/scripts/patchtrack-job.sh}"
STATE_DIR="${PATCHTRACK_STATE_DIR:-/var/lib/patchtrack/state}"
FAILED=0

# due <name> <interval-seconds> — true when the stamp is older than the
# interval (or absent). Stamps are updated only on success, so a failing
# collection retries on the next tick rather than waiting a full interval.
due() {
  local name="$1" interval="$2"
  local stamp now last
  stamp="$STATE_DIR/$name.last"
  now="$(date +%s)"
  last=0
  [[ -r "$stamp" ]] && last="$(cat "$stamp" 2>/dev/null || echo 0)"
  [[ $(( now - last )) -ge $interval ]]
}

stamp() {
  mkdir -p "$STATE_DIR"
  date +%s > "$STATE_DIR/$1.last"
}

# A step returns 0 (done), 3 (not due) or non-zero (failed). The three cases
# are reported differently so an operator can tell "nothing to do" from
# "worked" without reading timestamps. The failure is recorded without
# aborting the rest: one dead source system must not stop the daily snapshot.
STEP_SKIPPED=3
step() {
  local label="$1" status; shift
  status=0
  "$@" || status=$?
  case "$status" in
    0) echo "ok   $label" ;;
    "$STEP_SKIPPED") echo "skip $label (not due)" ;;
    *) echo "FAIL $label" >&2; FAILED=1 ;;
  esac
  return 0
}

# due_run <name> <interval-seconds> <job action...>
due_run() {
  local name="$1" interval="$2"; shift 2
  if due "$name" "$interval"; then
    if "$JOB" "$@"; then
      stamp "$name"
      return 0
    fi
    return 1
  fi
  return "$STEP_SKIPPED"
}

collection() {
  local integration="$1" interval="$2"
  due_run "sync-$integration" "$interval" sync "$integration"
}

daily_snapshot() {
  due_run daily "$(( 24 * 3600 ))" daily
}

audit_retention() {
  # The window itself lives in the database (Settings → Audit trail
  # retention), so this job only applies it.
  due_run audit-purge "$(( 24 * 3600 ))" audit-purge
}

step "sync assetexplorer" collection assetexplorer "${PATCHTRACK_SYNC_ASSETEXPLORER_INTERVAL:-21600}"
step "sync satellite"     collection satellite     "${PATCHTRACK_SYNC_SATELLITE_INTERVAL:-21600}"
step "sync sccm"          collection sccm          "${PATCHTRACK_SYNC_SCCM_INTERVAL:-86400}"
step "sync solaris"       collection solaris       "${PATCHTRACK_SYNC_SOLARIS_INTERVAL:-604800}"
step "daily snapshot"     daily_snapshot
step "audit retention"    audit_retention
# The report tick is idempotent and cheap; it runs on every timer firing.
step "report tick"        "$JOB" tick

exit "$FAILED"
