#!/usr/bin/env bash
# Container entrypoint for every PatchTrack service.
#
# SERVICE selects which application module to run. Migrations are opt-in
# (PATCHTRACK_RUN_MIGRATIONS=1) and are normally run once by a dedicated
# migrate job so that five services starting simultaneously do not race.
set -euo pipefail

# A supplied command replaces the default service start. The migrate service
# uses this: compose overrides CMD, not ENTRYPOINT, so without this branch the
# one-shot migration would be swallowed and the container would try to start a
# service with no PATCHTRACK_SERVICE.
if [[ $# -gt 0 ]]; then
  exec "$@"
fi

SERVICE="${PATCHTRACK_SERVICE:?PATCHTRACK_SERVICE is required (identity|inventory|compliance|reporting|gateway)}"
PORT="${PORT:-8000}"

case "$SERVICE" in
  identity|inventory|compliance|reporting|gateway) ;;
  *) echo "unknown PATCHTRACK_SERVICE: $SERVICE" >&2; exit 2 ;;
esac

if [[ "${PATCHTRACK_RUN_MIGRATIONS:-0}" == "1" ]]; then
  echo "running database migrations"
  python -m patchtrack_common.migrate
fi

# uvicorn accepts only lowercase level names, and the documented setting is
# conventionally written in capitals (INFO); normalise rather than refuse to
# start over letter case.
LOG_LEVEL="$(printf '%s' "${PATCHTRACK_LOG_LEVEL:-info}" | tr '[:upper:]' '[:lower:]')"

exec python -m uvicorn "app.main:app" \
  --app-dir "/opt/patchtrack/services/${SERVICE}" \
  --host 0.0.0.0 --port "$PORT" \
  --proxy-headers --forwarded-allow-ips "${PATCHTRACK_FORWARDED_IPS:-127.0.0.1}" \
  --no-server-header --log-level "$LOG_LEVEL"
