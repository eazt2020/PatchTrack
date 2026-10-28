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
# Like the deployment artifacts, the development stack serves HTTPS by
# default: the gateway generates a certificate under .runtime/tls on first
# start, with the bind address in its SANs, so https://<bind>:8005 works
# immediately. Set PATCHTRACK_TLS_ENABLED=false to develop over plain HTTP or
# when an external proxy terminates TLS.
TLS_ENABLED="$(printf '%s' "${PATCHTRACK_TLS_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')"
case "$TLS_ENABLED" in
  1|true|yes|on) SCHEME=https ;;
  *) TLS_ENABLED=false; SCHEME=http ;;
esac

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
  # Over HTTPS the default 'auto' marks the session cookie Secure, which is
  # correct. A plain-HTTP development stack still needs 'never', or the
  # browser discards the cookie after a correct sign-in; it is refused outside
  # development, which is the only environment this script starts.
  if [[ "$SCHEME" == https ]]; then
    export PATCHTRACK_SESSION_COOKIE_SECURE="${PATCHTRACK_SESSION_COOKIE_SECURE:-auto}"
  else
    export PATCHTRACK_SESSION_COOKIE_SECURE="${PATCHTRACK_SESSION_COOKIE_SECURE:-never}"
  fi
  export PATCHTRACK_TLS_ENABLED="$TLS_ENABLED"
  export PATCHTRACK_TLS_DIR="${PATCHTRACK_TLS_DIR:-$RUN/tls}"
  # An empty hostname makes the generated certificate use the machine name;
  # using the bind address puts that IP in the SANs, so browsing by IP matches.
  export PATCHTRACK_TLS_HOSTNAME="${PATCHTRACK_TLS_HOSTNAME:-$BIND}"
  export PATCHTRACK_LOG_LEVEL=INFO
  export PATCHTRACK_REPORT_DIR="$RUN/reports"
  export PATCHTRACK_SOLARIS_INBOX="$RUN/solaris-inbox"
  export PATCHTRACK_IDENTITY_URL=http://127.0.0.1:8001
  export PATCHTRACK_INVENTORY_URL=http://127.0.0.1:8002
  export PATCHTRACK_COMPLIANCE_URL=http://127.0.0.1:8003
  export PATCHTRACK_REPORTING_URL=http://127.0.0.1:8004
  export PYTHONPATH="$ROOT/services/common:$ROOT/services"
  # Remembered so `status` checks the address and scheme the gateway actually
  # listens on.
  printf '%s\n' "$BIND" >"$RUN/bind"
  printf '%s\n' "$SCHEME" >"$RUN/scheme"

  /opt/patchtrack-venv/bin/python -m patchtrack_common.migrate

  for spec in identity:8001 inventory:8002 compliance:8003 reporting:8004 gateway:8005; do
    name="${spec%%:*}"; port="${spec##*:}"
    host=127.0.0.1
    [[ "$name" == gateway ]] && host="$BIND"
    # The shared launcher is what adds TLS for the gateway and reloads an
    # uploaded certificate without a restart; plain uvicorn would serve HTTP.
    "$VENV/bin/python" -m patchtrack_common.serve "app.main:app" \
      --app-dir "$ROOT/services/$name" --host "$host" --port "$port" \
      --service "$name" --log-level info \
      >"$LOGS/$name.log" 2>&1 &
    echo $! >>"$RUN/dev.pid.tmp"
  done
  mv "$RUN/dev.pid.tmp" "$RUN/dev.pid"
  echo "started: gateway ${SCHEME}://${BIND}:8005 (logs in $LOGS)"
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
  local scheme
  scheme="$(cat "$RUN/scheme" 2>/dev/null || echo "$SCHEME")"
  for name in identity inventory compliance reporting gateway; do
    printf '%-11s ' "$name"
    host=127.0.0.1
    [[ "$name" == gateway ]] && host="$BIND"
    port="$(case $name in
      identity) echo 8001;; inventory) echo 8002;; compliance) echo 8003;;
      reporting) echo 8004;; gateway) echo 8005;; esac)"
    if [[ "$name" == gateway && "$scheme" == https ]]; then
      # The development certificate is self-signed; this checks the listener
      # rather than the trust chain.
      curl -fsSk "https://${host}:${port}/healthz" || echo "down"
    else
      curl -fsS "http://${host}:${port}/healthz" || echo "down"
    fi
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
