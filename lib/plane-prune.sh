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
