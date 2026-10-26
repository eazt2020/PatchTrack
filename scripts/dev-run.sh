#!/usr/bin/env bash
# Local development launcher (no Docker).
#
# Generates ephemeral secrets per launch and exports them to the child
# processes only. Nothing is written to disk, and nothing here is suitable
# for production — use deploy/ artifacts with real secret management there.
#
#   scripts/dev-run.sh start|stop|status
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${PATCHTRACK_VENV:-/opt/patchtrack-venv}"
RUN="$ROOT/.runtime"
LOGS="$RUN/logs"
mkdir -p "$LOGS" "$RUN/reports"

DB_URL="${PATCHTRACK_DATABASE_URL:-postgresql+psycopg://patchtrack@/patchtrack?host=/tmp}"
# Only the gateway is published. The four backends keep listening on loopback
# because the gateway reaches them there; exposing them would bypass the
# session, CSRF and permission checks that live in front of them.
BIND="${PATCHTRACK_BIND_ADDRESS:-$(cat "$RUN/bind" 2>/dev/null || echo 127.0.0.1)}"

start() {
  if [[ -f "$RUN/dev.pid" ]] && kill -0 "$(cat "$RUN/dev.pid")" 2>/dev/null; then
    echo "already running (pid $(cat "$RUN/dev.pid"))"; exit 0
  fi
  # Secrets live only in this shell's environment.
  PATCHTRACK_JWT_SECRET="$(openssl rand -hex 32)"
  PATCHTRACK_SERVICE_TOKEN="$(openssl rand -hex 32)"
  export PATCHTRACK_JWT_SECRET PATCHTRACK_SERVICE_TOKEN
  export PATCHTRACK_DATABASE_URL="$DB_URL"
  export PATCHTRACK_ENVIRONMENT=development
  export PATCHTRACK_LOG_LEVEL=INFO
  export PATCHTRACK_REPORT_DIR="$RUN/reports"
  export PATCHTRACK_SOLARIS_INBOX="$RUN/solaris-inbox"
  export PATCHTRACK_IDENTITY_URL=http://127.0.0.1:8001
  export PATCHTRACK_INVENTORY_URL=http://127.0.0.1:8002
  export PATCHTRACK_COMPLIANCE_URL=http://127.0.0.1:8003
  export PATCHTRACK_REPORTING_URL=http://127.0.0.1:8004
  export PYTHONPATH="$ROOT/services/common:$ROOT/services"
  # Remembered so `status` checks the address the gateway actually listens on.
  printf '%s\n' "$BIND" >"$RUN/bind"

  /opt/patchtrack-venv/bin/python -m patchtrack_common.migrate

  for spec in identity:8001 inventory:8002 compliance:8003 reporting:8004 gateway:8005; do
    name="${spec%%:*}"; port="${spec##*:}"
    host=127.0.0.1
    [[ "$name" == gateway ]] && host="$BIND"
    "$VENV/bin/python" -m uvicorn "app.main:app" \
      --app-dir "$ROOT/services/$name" --host "$host" --port "$port" \
      >"$LOGS/$name.log" 2>&1 &
    echo $! >>"$RUN/dev.pid.tmp"
  done
  mv "$RUN/dev.pid.tmp" "$RUN/dev.pid"
  echo "started: gateway http://${BIND}:8005 (logs in $LOGS)"
}

stop() {
  if [[ -f "$RUN/dev.pid" ]]; then
    while read -r pid; do kill "$pid" 2>/dev/null || true; done <"$RUN/dev.pid"
    rm -f "$RUN/dev.pid"
    echo "stopped"
  else
    echo "not running"
  fi
}

status() {
  for name in identity inventory compliance reporting gateway; do
    printf '%-11s ' "$name"
    host=127.0.0.1
    [[ "$name" == gateway ]] && host="$BIND"
    curl -fsS "http://${host}:$(case $name in
      identity) echo 8001;; inventory) echo 8002;; compliance) echo 8003;;
      reporting) echo 8004;; gateway) echo 8005;; esac)/healthz" || echo "down"
    echo
  done
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  restart) stop; sleep 1; start ;;
  status) status ;;
  *) echo "usage: $0 start|stop|restart|status" >&2; exit 2 ;;
esac
