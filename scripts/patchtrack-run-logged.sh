#!/usr/bin/env bash
# Run a PatchTrack maintenance tool with its console output copied to a log.
#
#   scripts/patchtrack-run-logged.sh <tool> <command> [arguments...]
#
# The install.sh, upgrade.sh and uninstall.sh wrappers use this so a run leaves
# evidence on disk even when the operator only saw a terminal. The log contains
# everything the tool prints, including output inherited from dnf, the container
# runtime and psql. It never contains environment files or generated secrets;
# passwords are read from the terminal without echo and are not printed.
#
# Location: $PATCHTRACK_LOG_DIR, or /var/log/patchtrack. If that is not
# writable (the tools normally run as root; --check may not), the per-user
# state directory is used instead. Mode is 0600 and the directory 0750.
# `<tool>.log` is a symlink to the newest run. Logs older than
# $PATCHTRACK_LOG_RETENTION_DAYS (default 90) are removed.
set -uo pipefail

TOOL="${1:-}"
if [ -z "$TOOL" ] || [ "$#" -lt 2 ]; then
  echo "usage: $0 <tool> <command> [arguments...]" >&2
  exit 2
fi
shift

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RETENTION_DAYS="${PATCHTRACK_LOG_RETENTION_DAYS:-90}"
case "$RETENTION_DAYS" in
  ''|*[!0-9]*) RETENTION_DAYS=90 ;;
esac

umask 0077

make_log_file() {
  local dir="$1" path
  if [ ! -d "$dir" ]; then
    mkdir -p -- "$dir" 2>/dev/null || return 1
    # mkdir honours the 0077 umask; the log directory is readable by the
    # deployment group, not by other users.
    chmod 0750 -- "$dir" 2>/dev/null || true
  fi
  [ -d "$dir" ] && [ -w "$dir" ] || return 1
  path="$dir/${TOOL}-${STAMP}-$$.log"
  ( set -o noclobber; : >"$path" ) 2>/dev/null || return 1
  chmod 0600 -- "$path" 2>/dev/null || true
  printf '%s' "$path"
}

LOG_FILE=""
REQUESTED_DIR="${PATCHTRACK_LOG_DIR:-/var/log/patchtrack}"
if LOG_FILE="$(make_log_file "$REQUESTED_DIR")"; then
  :
else
  LOG_FILE=""
  FALLBACK_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/patchtrack/logs"
  if [ "$FALLBACK_DIR" != "$REQUESTED_DIR" ] \
      && LOG_FILE="$(make_log_file "$FALLBACK_DIR")"; then
    printf 'PatchTrack: cannot write %s; run log is at %s\n' \
      "$REQUESTED_DIR" "$LOG_FILE" >&2
  fi
fi

if [ -z "$LOG_FILE" ]; then
  printf 'PatchTrack: warning: no run log could be created; continuing without one\n' >&2
  exec "$@"
fi

LOG_DIR="$(dirname "$LOG_FILE")"
LOG_NAME="$(basename "$LOG_FILE")"

# The command line is recorded because it explains what was asked for. No
# supported option carries a password, token or private key.
{
  printf 'PatchTrack %s run log\n' "$TOOL"
  printf 'Log file        : %s\n' "$LOG_FILE"
  printf 'Started (UTC)   : %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'Tool            : %s\n' "$*"
  printf 'User            : %s\n' "$(id -un 2>/dev/null || printf unknown)"
  printf 'Host            : %s\n' "$(uname -n 2>/dev/null || printf unknown)"
  printf '\n'
} >>"$LOG_FILE"

printf 'PatchTrack %s log: %s\n\n' "$TOOL" "$LOG_FILE"

# tee ignores SIGINT so a Ctrl-C is handled by the Python tool, which can still
# write its cancellation or rollback messages. The tool's exit status, not
# tee's, is what the wrapper returns.
set -o pipefail
PYTHONUNBUFFERED=1 "$@" 2>&1 | ( trap '' INT; exec tee -a -- "$LOG_FILE" )
STATUS=${PIPESTATUS[0]}

{
  printf '\nFinished (UTC)  : %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'Exit status     : %s\n' "$STATUS"
} >>"$LOG_FILE"

# A stable name for "the newest run of this tool". Relative target so moving
# the log directory does not leave a dangling absolute link.
ln -sfn -- "$LOG_NAME" "$LOG_DIR/$TOOL.log" 2>/dev/null || true

# Prune only this tool's timestamped logs; a file the operator copied here is
# left alone. Failures are not fatal.
find "$LOG_DIR" -maxdepth 1 -type f -name "${TOOL}-*.log" \
  -mtime +"$RETENTION_DAYS" -delete 2>/dev/null || true

exit "$STATUS"
