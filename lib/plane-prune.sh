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

# OFF-SWITCH — an opt-OUT since the defaults flip (chunk N). Retention of raw
# metric SAMPLES past the 30-day incident-join window is hygiene an operator
# already assumes is happening, and it is what makes the per-minute host probe
# safe to ship on; the DELETE is family-scoped to metric_samples and can never
# reach the ledger. So it ships ON: unset, or any value but an exact 0, runs.
# A host turns it off with PLANE_PRUNE_ENABLED=0 in its host or root .env,
# stamped onto this unit by the composer (a host timer runs in a CLOSED env).
# THE WINDOW IS THE OTHER KNOB — the unit's script line may append --days N.
#
# The no-op is LOUD: a plane growing without bound because a flag was set two
# months ago and forgotten is the failure this line prevents. Only an exact 0
# disarms — an empty assignment is a win at its tier (#1213) but is not a 0.
if [ "${PLANE_PRUNE_ENABLED:-1}" = "0" ]; then
    printf 'plane-prune: OFF on this host (PLANE_PRUNE_ENABLED=0) -- metric samples will accumulate without bound; unset it, or set 1, to restore the default\n' >&2
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
