#!/usr/bin/env bash
# PatchTrack guided upgrade.
#
# Run from the unpacked directory of the new release:
#
#   sudo ./upgrade.sh --check --version 3.1.0
#   sudo ./upgrade.sh --version 3.1.0
#
# The upgrader reads the manifest written by install.sh, takes a mandatory
# backup of the application tree, configuration and database, stages the new
# release over the old one, applies migrations and verifies the result. If any
# step fails it restores the previous version, configuration and database.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PATCHTRACK_INSTALLER_PYTHON:-}"

if [ -z "$PYTHON_BIN" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  elif [ -x /usr/libexec/platform-python ]; then
    PYTHON_BIN=/usr/libexec/platform-python
  fi
fi

if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
  echo "PatchTrack upgrader: Python 3 is required but was not found." >&2
  echo "Install python3 with dnf, or set PATCHTRACK_INSTALLER_PYTHON." >&2
  exit 1
fi

# Every run is copied to /var/log/patchtrack/upgrade-<UTC>-<pid>.log (or
# $PATCHTRACK_LOG_DIR) with mode 0600. The wrapper still returns the upgrader's
# exit status, and Ctrl-C reaches it so a failed upgrade can roll back.
exec "$HERE/scripts/patchtrack-run-logged.sh" upgrade \
  "$PYTHON_BIN" "$HERE/scripts/patchtrack_upgrade.py" "$@"
