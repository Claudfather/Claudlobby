#!/usr/bin/env bash
# plane-shadow.sh — launcher the composed <fleet>.plane-shadow fleet timer execs
# (cutover chunk 3, J4). Records the legacy-vs-plane open-set comparison for
# every bot on the roster: `claudlobby --fleet <fleet> plane shadow --record`.
# DORMANT and self-gated on PLANE_SHADOW_ENABLED (the arming carrier stamps it
# on this unit from the fleet .env tier); a comparison is a recorded fact and
# must not start being written by a root pull. Exec ladder: venv CLI -> PATH
# -> python -m. NOT the ingest daemon (scope tripwire).
set -euo pipefail
if [ "${PLANE_SHADOW_ENABLED:-0}" != "1" ]; then
    printf 'plane-shadow: dormant (set PLANE_SHADOW_ENABLED=1 to arm the shadow comparison)\n' >&2
    exit 0
fi
FLEET="${CLAUDLOBBY_FLEET:-${FLEET_NAME:-}}"
if [ -z "$FLEET" ]; then
    printf 'plane-shadow: no fleet in CLAUDLOBBY_FLEET/FLEET_NAME - the open set is per fleet\n' >&2
    exit 0
fi
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${CLAUDLOBBY_ROOT:-$(cd "$LIB_DIR/.." && pwd)}"
export CLAUDLOBBY_ROOT="$ROOT"
ARGS=(--root "$ROOT" --fleet "$FLEET" plane shadow --record "$@")
if [ -x "$ROOT/.venv/bin/claudlobby" ]; then
    exec "$ROOT/.venv/bin/claudlobby" "${ARGS[@]}"
fi
if command -v claudlobby >/dev/null 2>&1; then
    exec claudlobby "${ARGS[@]}"
fi
if command -v python3 >/dev/null 2>&1 \
    && (cd "$ROOT" && python3 -c "import claudlobby" >/dev/null 2>&1); then
    cd "$ROOT"
    exec python3 -m claudlobby "${ARGS[@]}"
fi
printf 'plane-shadow.sh: no claudlobby CLI resolvable from %s\n' "$ROOT" >&2
exit 127
