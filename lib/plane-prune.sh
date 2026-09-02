#!/bin/bash
# plane-prune.sh — the composed `claudlobby-plane-prune` host-job timer
# execs this (chunk 3a; spec §F20). Thin by rule: env resolution here,
# the family-scoped DELETE in `claudlobby plane prune`.
#
# NOT the ingest daemon (INGEST ONLY by scope tripwire) — a separate door
# on its own timer, writing through its own WAL connection while the daemon
# ingests. Ages out raw metric_samples past the 30-day window; the ledger
# is never touched. `--root` is a GLOBAL flag and precedes the subcommand.
# Extra args pass through (the timer's script line may append --days N).

set -euo pipefail

# SELF-GATE — arming is an env flag, not the timer's mere existence. The
# `enroll: false` manifest is NOT enforced for host timers (setup-system
# enrolls every composed claudlobby-* unit with no dormancy gate — a
# pre-existing gap, gauntlet-probed), so for a DELETE door that alone
# cannot honor "must not arrive switched on via a root pull." A host arms
# retention by setting PLANE_PRUNE_ENABLED=1 (the SESSION_DIGEST_ENABLED
# pattern); unarmed, the timer fires and no-ops loudly. Defense in depth:
# the manifest still marks it dormant, this makes dormancy TRUE.
if [ "${PLANE_PRUNE_ENABLED:-0}" != "1" ]; then
    printf 'plane-prune: dormant (set PLANE_PRUNE_ENABLED=1 to arm retention)\n' >&2
    exit 0
fi

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${CLAUDLOBBY_ROOT:-$(cd "$LIB_DIR/.." && pwd)}"
export CLAUDLOBBY_ROOT="$ROOT"

ARGS=(--root "$ROOT" plane prune "$@")

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
printf 'plane-prune.sh: no claudlobby CLI resolvable from %s\n' "$ROOT" >&2
exit 127
