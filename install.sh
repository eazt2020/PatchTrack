#!/usr/bin/env bash
# PatchTrack guided installer.
#
#   sudo ./install.sh
#   sudo ./install.sh --check
#
# The installer is written in Python because the work it does is easier to make
# safe there: structured prompts, JSON for the first-run administrator call,
# ordered rollback actions and no shell quoting around passwords. RHEL and
# Rocky 9 always ship a Python 3 interpreter as part of the base platform, so
# this adds no dependency to the supported host.
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
  echo "PatchTrack installer: Python 3 is required but was not found." >&2
  echo "Install python3 with dnf, or set PATCHTRACK_INSTALLER_PYTHON." >&2
  exit 1
fi

# Every run is copied to /var/log/patchtrack/install-<UTC>-<pid>.log (or
# $PATCHTRACK_LOG_DIR) with mode 0600. The wrapper still returns the installer's
# exit status, and Ctrl-C reaches the installer so it can roll back.
exec "$HERE/scripts/patchtrack-run-logged.sh" install \
  "$PYTHON_BIN" "$HERE/scripts/patchtrack_install.py" "$@"
