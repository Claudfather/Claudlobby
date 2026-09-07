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

# OFF-SWITCH — an opt-OUT since the defaults flip (chunk N). Aging the
# attention queue is the half of the target workflow that keeps the queue
# meaning something, so it ships ON: unset, or any value but an exact 0, runs.
# A host turns it off with PLANE_EXPIRE_ENABLED=0 in its host or root .env,
# which the composer stamps onto this unit (a host timer runs in a CLOSED env
# and sources no .env — the #1383 carrier).
#
# The no-op is LOUD: a silent skip reads as a broken timer, and a disabled
# reaction must never be invisible. Only an exact 0 disarms — an empty
# assignment is a win at its tier (#1213) but is not a 0.
if [ "${PLANE_EXPIRE_ENABLED:-1}" = "0" ]; then
    printf 'plane-expire: OFF on this host (PLANE_EXPIRE_ENABLED=0) -- no assignment will be expired; unset it, or set 1, to restore the default\n' >&2
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
