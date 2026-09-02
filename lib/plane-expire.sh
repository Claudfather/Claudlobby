#!/bin/bash
# plane-expire.sh — the composed `claudlobby-plane-expire` host-job timer
# execs this (the attention-expiry sweep). Thin by rule: env resolution
# here, the sweep itself in `claudlobby plane expire`.
#
# NOT the ingest daemon (INGEST ONLY by scope tripwire) — a separate door
# on its own timer, writing through its own WAL connection while the daemon
# ingests. Emits a terminal `expired` task event for assignments overdue
# past the horizon — a Lane-B fact through normal ingest, never a write. `--root` is a GLOBAL flag and precedes the subcommand.
# Extra args pass through (the timer's script line may append --days N).

set -euo pipefail

# SELF-GATE — arming is an env flag, not the timer's mere existence. The
# `enroll: false` manifest is NOT enforced for host timers (setup-system
# enrolls every composed claudlobby-* unit with no dormancy gate), so an
# event-emitting POLICY sweep must not arrive switched on via a root
# pull. A host arms expiry by setting PLANE_EXPIRE_ENABLED=1 (the SESSION_DIGEST_ENABLED
# pattern); unarmed, the timer fires and no-ops loudly. Defense in depth:
# the manifest still marks it dormant, this makes dormancy TRUE.
if [ "${PLANE_EXPIRE_ENABLED:-0}" != "1" ]; then
    printf 'plane-expire: dormant (set PLANE_EXPIRE_ENABLED=1 to arm the attention sweep)\n' >&2
    exit 0
fi

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${CLAUDLOBBY_ROOT:-$(cd "$LIB_DIR/.." && pwd)}"
export CLAUDLOBBY_ROOT="$ROOT"

ARGS=(--root "$ROOT" plane expire "$@")

if [ -x "$ROOT/.venv/bin/claudlobby" ]; then
    exec "$ROOT/.venv/bin/claudlobby" "${ARGS[@]}"
fi
if command -v claudlobby >/dev/null 2>&1; then
    exec claudlobby "${ARGS[@]}"
fi
# python3 existing is not python3 being USABLE (the plane-daemon.sh note):
# probe the import so an unusable interpreter falls through to the honest
# 127 rather than an exit-1 that looks like a prune failure.
if command -v python3 >/dev/null 2>&1 \
    && (cd "$ROOT" && python3 -c "import claudlobby" >/dev/null 2>&1); then
    cd "$ROOT"
    exec python3 -m claudlobby "${ARGS[@]}"
fi
printf 'plane-expire.sh: no claudlobby CLI resolvable from %s\n' "$ROOT" >&2
exit 127
