#!/usr/bin/env bash
# Build a clean PatchTrack release archive from a committed tree.
#
# The previous hand-made ZIP shipped .env, the TLS private key, PostgreSQL
# data and Grafana runtime state. `git archive` only ships committed files,
# and every path is re-checked against a deny list before the archive is
# published. Requires a clean working tree so the archive matches a commit.
#
#   scripts/build-release.sh [VERSION]
#   scripts/build-release.sh --verify path/to/archive.tar.gz
#
# Secret *values* are never rotated by this script; rotate anything that was
# ever included in an archive that left the host.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${OUT_DIR:-$ROOT/dist}"

# Paths that must never appear in a release. `.env.example` is deliberately
# allowed: it documents configuration without containing a secret.
DENY=(
  '(^|/)\.env$'
  '(^|/)\.env\.(local|dev|development|prod|production|staging|test)$'
  '(^|/)\.runtime/'
  '(^|/)\.git/'
  '(^|/)pgdata/'
  '(^|/)postgres-data/'
  '(^|/)grafana\.db$'
  '(^|/)grafana-data/'
  '\.(pem|key|crt|p12|pfx)$'
  '(^|/)id_(rsa|dsa|ecdsa|ed25519)$'
  '(^|/)\.pgpass$'
  '(^|/)\.netrc$'
  '(^|/)node_modules/'
  '(^|/)__pycache__/'
  '\.pyc$'
  '\.log$'
)

verify_archive() {
  local archive="$1" names bad=0 blocked
  names="$(tar -tzf "$archive")"
  for pattern in "${DENY[@]}"; do
    while IFS= read -r blocked; do
      [ -n "$blocked" ] || continue
      printf 'release archive contains forbidden path: %s\n' "$blocked" >&2
      bad=1
    done < <(printf '%s\n' "$names" | grep -E -- "$pattern" || true)
  done
  if [ "$bad" -ne 0 ]; then
    printf 'refusing to publish %s\n' "$archive" >&2
    return 1
  fi
  printf 'verified: %s (%s files)\n' "$archive" "$(printf '%s\n' "$names" | wc -l)"
}

if [ "${1:-}" = "--verify" ]; then
  [ $# -eq 2 ] || { echo "usage: $0 --verify ARCHIVE" >&2; exit 2; }
  verify_archive "$2"
  exit 0
fi

VERSION="${1:-$(git -C "$ROOT" describe --tags --always)}"
case "$VERSION" in
  *[!A-Za-z0-9._+-]*) echo "invalid version: $VERSION" >&2; exit 2 ;;
esac

if [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
  echo "working tree is dirty; commit the release before packaging" >&2
  echo "override for a local dry run with ALLOW_DIRTY=1" >&2
  [ "${ALLOW_DIRTY:-0}" = "1" ] || exit 1
fi

mkdir -p "$OUT_DIR"
ARCHIVE="$OUT_DIR/patchtrack-$VERSION.tar.gz"
git -C "$ROOT" archive --format=tar.gz \
  --prefix="patchtrack-$VERSION/" --output="$ARCHIVE" HEAD

verify_archive "$ARCHIVE"

SUM="$ARCHIVE.sha256"
if command -v sha256sum >/dev/null 2>&1; then
  (cd "$OUT_DIR" && sha256sum "$(basename "$ARCHIVE")" > "$(basename "$SUM")")
else
  (cd "$OUT_DIR" && shasum -a 256 "$(basename "$ARCHIVE")" > "$(basename "$SUM")")
fi
printf 'built: %s\n' "$ARCHIVE"
cat "$SUM"
