#!/usr/bin/env bash
# PatchTrack guided uninstaller.
#
#   sudo ./uninstall.sh --check
#   sudo ./uninstall.sh
#   sudo ./uninstall.sh --purge-data
#
# The uninstaller reads the manifest written by install.sh, so it knows which
# paths, account, image and compose project that installation created. It keeps
# data volumes unless --purge-data is given, and keeps any configuration file
# the installer did not create.
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
  echo "PatchTrack uninstaller: Python 3 is required but was not found." >&2
  echo "Install python3 with dnf, or set PATCHTRACK_INSTALLER_PYTHON." >&2
  exit 1
fi

# Every run is copied to /var/log/patchtrack/uninstall-<UTC>-<pid>.log (or
# $PATCHTRACK_LOG_DIR) with mode 0600. The wrapper still returns the
# uninstaller's exit status. The log is written outside the installed tree and
# configuration directory, so it survives the uninstallation.
exec "$HERE/scripts/patchtrack-run-logged.sh" uninstall \
  "$PYTHON_BIN" "$HERE/scripts/patchtrack_uninstall.py" "$@"
