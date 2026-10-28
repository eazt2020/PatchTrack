#!/usr/bin/env bash
# Run a single PatchTrack background job from cron or a systemd timer.
#
# The gateway is the only published port, and its /api routes need a browser
# session cookie — the wrong credential for a timer. Jobs therefore call the
# owning service directly, presenting the shared service token, exactly as the
# gateway would on a user's behalf. Nothing here needs a user password; the
# token is per-deployment.
#
#   scripts/patchtrack-job.sh sync assetexplorer
#   scripts/patchtrack-job.sh sync satellite
#   scripts/patchtrack-job.sh daily
#   scripts/patchtrack-job.sh tick
#   scripts/patchtrack-job.sh purge
#   scripts/patchtrack-job.sh audit-purge
#
# Two deployments are supported, selected per request:
#
#   container  the service is running under a compose project (docker compose,
#              or podman-compose — the usual runtime on RHEL/Rocky) and the
#              request is made inside its container, over its loopback;
#   native     the service binds 127.0.0.1:$PORT under systemd, and the
#              request is made from here.
#
# The choice is made for each call: if the runtime reports a running container
# for the service, the request goes through it; otherwise the script talks to
# the local port. PATCHTRACK_MODE=container|native overrides the detection,
# which is useful where a stale container is present, and
# PATCHTRACK_CONTAINER_CLI=docker|podman pins the runtime when both are
# installed.
#
# scripts/patchtrack-tick.sh is the "run everything that is due" variant; this
# script runs exactly one named job, which is what a cron entry wants.
#
# Exit status is non-zero when the job reports a failure, so cron mail and
# systemd failures work as expected.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${1:-}"
PYTHON="${PATCHTRACK_PYTHON:-/opt/patchtrack-venv/bin/python}"
MODE="${PATCHTRACK_MODE:-auto}"
CONTAINER_CLI="${PATCHTRACK_CONTAINER_CLI:-auto}"

port_for() {
  local service="$1" port=""
  if [[ -r "/etc/patchtrack/ports/$service" ]]; then
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

# Which container runtime to use. Docker is preferred when it is genuinely
# available; otherwise Podman, which is what ships on RHEL and Rocky. Both are
# supported because the shipped deployment guide uses podman-compose on those
# hosts, and a runner that only understood docker would silently fall back to
# the native transport and then fail to reach a published port that does not
# exist.
runtime() {
  case "$CONTAINER_CLI" in
    docker) printf 'docker'; return 0 ;;
    podman) printf 'podman'; return 0 ;;
    none) printf 'none'; return 0 ;;
  esac
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    printf 'docker'
  elif command -v podman >/dev/null 2>&1 \
    || command -v podman-compose >/dev/null 2>&1; then
    printf 'podman'
  else
    printf 'none'
  fi
}

# The compose labels are the same for both runtimes, so service lookup is a
# label filter in both cases; docker compose also has a per-service `ps`.
container_running() {
  local service="$1"
  case "$(runtime)" in
    docker)
      command -v docker >/dev/null 2>&1 || return 1
      [[ -n "$(docker compose --project-directory "$ROOT" ps --quiet "$service" 2>/dev/null)" ]]
      ;;
    podman)
      command -v podman >/dev/null 2>&1 || return 1
      [[ -n "$(podman ps --filter "label=com.docker.compose.service=$service" \
        --format '{{.Names}}' 2>/dev/null)" ]]
      ;;
    *) return 1 ;;
  esac
}

container_exec() {
  local service="$1"
  shift
  case "$(runtime)" in
    docker)
      docker compose --project-directory "$ROOT" exec -T "$service" "$@"
      ;;
    podman)
      if command -v podman-compose >/dev/null 2>&1; then
        podman-compose -f "$ROOT/docker-compose.yml" exec -T "$service" "$@"
      else
        local name
        name="$(podman ps --filter "label=com.docker.compose.service=$service" \
          --format '{{.Names}}' | head -n1)"
        [[ -n "$name" ]] || { echo "no running container for $service" >&2; return 1; }
        podman exec -i "$name" "$@"
      fi
      ;;
    *) return 1 ;;
  esac
}

in_container() {
  local service="$1"
  case "$MODE" in
    native) return 1 ;;
    container) return 0 ;;
  esac
  container_running "$service"
}

# The request program, shared by both transports. The base URL is an argument:
# the container path passes "" so the service's own PORT is used, and the
# native path passes the host address it resolved. The token always comes from
# the environment of whichever process runs this.
request_program() {
  cat <<'PY'
import json, os, sys, urllib.error, urllib.request

path, body, base = sys.argv[1], sys.argv[2], sys.argv[3]
if not base:
    base = f"http://127.0.0.1:{os.environ.get('PORT', '8000')}"
request = urllib.request.Request(
    base + path,
    data=body.encode() if body else b"",
    method="POST",
    headers={
        # The scheduler acts as itself, not as any human user.
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
PY
}

# post <service> <path> [json-body]
post() {
  local service="$1" path="$2" body="${3:-}"
  if in_container "$service"; then
    # The container's own environment already carries the token and PORT, so
    # nothing has to be readable by the calling account on the host.
    request_program | container_exec "$service" python - "$path" "$body" ""
  else
    # Native: the token comes from the caller's environment (the systemd
    # EnvironmentFile), and -I keeps the working directory off the import path.
    request_program | "$PYTHON" -I - "$path" "$body" \
      "http://127.0.0.1:$(port_for "$service")"
  fi
}

case "$ACTION" in
  sync)
    integration="${2:?usage: $0 sync <assetexplorer|satellite|sccm|solaris>}"
    post inventory /sync "{\"integration\": \"$integration\"}"
    ;;
  daily)  post compliance /jobs/daily ;;
  tick)   post reporting  /jobs/tick ;;
  purge)  post reporting  /jobs/purge ;;
  audit-purge) post identity /jobs/audit-purge ;;
  *)
    echo "usage: $0 sync <integration> | daily | tick | purge | audit-purge" >&2
    exit 2
    ;;
esac
