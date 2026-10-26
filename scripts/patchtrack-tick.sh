#!/usr/bin/env bash
# Scheduled jobs for a NATIVE (systemd, no-container) installation.
#
# scripts/patchtrack-job.sh runs its request inside the service container.
# Under systemd there is no container: each service binds 127.0.0.1:$PORT and
# the unit's EnvironmentFile has exported PATCHTRACK_SERVICE_TOKEN, so the
# request is made directly here. Each step calls the owning service with the
# shared service token and the `scheduler` principal — never a human account,
# and never over the gateway (whose /api routes need a session cookie).
#
# Driven by patchtrack-tick.timer (hourly). Every step is skipped when it is
# not yet due, so one hourly timer covers all cadences:
#
#   PATCHTRACK_SYNC_ASSETEXPLORER_INTERVAL   21600   (6h)
#   PATCHTRACK_SYNC_SATELLITE_INTERVAL       21600
#   PATCHTRACK_SYNC_SCCM_INTERVAL            86400
#   PATCHTRACK_SYNC_SOLARIS_INTERVAL         604800
#
# The daily snapshot and report tick run once a day / every tick respectively.
# Job state is kept in /var/lib/patchtrack/state so a restart does not cause a
# burst of catch-up collections.
#
# Exit status is non-zero if any step failed, so `systemctl status` and
# journalctl show the failure instead of a silent log line.
set -euo pipefail

PYTHON="${PATCHTRACK_PYTHON:-/opt/patchtrack-venv/bin/python}"
STATE_DIR="${PATCHTRACK_STATE_DIR:-/var/lib/patchtrack/state}"
FAILED=0

: "${PATCHTRACK_SERVICE_TOKEN:?PATCHTRACK_SERVICE_TOKEN must be set (EnvironmentFile=/etc/patchtrack/.env)}"

port_for() {
  local service="$1" port="${PORT:-}"
  if [[ -z "$port" && -r "/etc/patchtrack/ports/$service" ]]; then
    port="$(sed -n 's/^PORT=//p' "/etc/patchtrack/ports/$service" | tr -d "\"' \t")"
  fi
  if [[ -z "$port" ]]; then
    case "$service" in
      identity) port=8001 ;; inventory) port=8002 ;;
      compliance) port=8003 ;; reporting) port=8004 ;; gateway) port=8005 ;;
    esac
  fi
  printf '%s' "$port"
}

# One request as the scheduler principal. The program is passed with -I so no
# import path from the working directory is honoured, and the path/body travel
# as environment values rather than being interpolated into code.
post() {
  local service="$1" path="$2" body="${3:-}"
  PATCHTRACK_JOB_URL="http://127.0.0.1:$(port_for "$service")${path}" \
  PATCHTRACK_JOB_BODY="$body" \
    "$PYTHON" -I -c '
import json, os, sys, urllib.error, urllib.request

request = urllib.request.Request(
    os.environ["PATCHTRACK_JOB_URL"],
    data=os.environ.get("PATCHTRACK_JOB_BODY", "").encode(),
    method="POST",
    headers={
        "x-patchtrack-internal": os.environ["PATCHTRACK_SERVICE_TOKEN"],
        "x-patchtrack-user": "scheduler",
        "x-patchtrack-role": "administrator",
        "x-patchtrack-user-id": "",
        "content-type": "application/json",
    },
)
try:
    with urllib.request.urlopen(request, timeout=900) as response:
        payload = response.read().decode()
except urllib.error.HTTPError as exc:
    print(exc.read().decode(), file=sys.stderr)
    sys.exit(1)
except urllib.error.URLError as exc:
    print(f"cannot reach the service: {exc.reason}", file=sys.stderr)
    sys.exit(1)
print(payload)
try:
    result = json.loads(payload)
except ValueError:
    sys.exit(0)
if isinstance(result, dict) and result.get("status") == "failed":
    sys.exit(1)
'
}

# Runs a step and records the failure without aborting the rest: one dead
# source system must not stop the daily snapshot or the report tick.
# A step returns 0 (done), 3 (not due) or non-zero (failed). The three cases
# are reported differently so an operator can tell "nothing to do" from
# "worked" without reading timestamps.
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

collection() {
  local integration="$1" interval="$2"
  if due "sync-$integration" "$interval"; then
    if post inventory /sync "{\"integration\": \"${integration}\"}"; then
      stamp "sync-$integration"
      return 0
    fi
    return 1
  fi
  return "$STEP_SKIPPED"
}

daily_snapshot() {
  if due daily "$(( 24 * 3600 ))"; then
    if post compliance /jobs/daily; then stamp daily; return 0; fi
    return 1
  fi
  return "$STEP_SKIPPED"
}

step "sync assetexplorer" collection assetexplorer "${PATCHTRACK_SYNC_ASSETEXPLORER_INTERVAL:-21600}"
step "sync satellite"     collection satellite     "${PATCHTRACK_SYNC_SATELLITE_INTERVAL:-21600}"
step "sync sccm"          collection sccm          "${PATCHTRACK_SYNC_SCCM_INTERVAL:-86400}"
step "sync solaris"       collection solaris       "${PATCHTRACK_SYNC_SOLARIS_INTERVAL:-604800}"
step "daily snapshot"     daily_snapshot
step "report tick"        post reporting /jobs/tick

exit "$FAILED"
