#!/bin/bash
# task-recheck.sh — the composed `<prefix>.task-recheck` FLEET timer execs this
# (the task loop's re-check trigger, chunk M-B of #1481). Thin by rule: env
# resolution here, the sweep itself in `claudlobby task recheck`.
#
# The fleet arrives as $1 (a fleet timer's ExecStart is `<script> <fleet>`), and
# is passed on as `--fleet`, so a hand run reads exactly like the unit does.
# `--root` is a GLOBAL flag and precedes the subcommand. Extra args pass through
# (the timer's script line may append --max-age-h N / --repeat-h N).
#
# What it does: hands each manager of the fleet ONE id-less re-check listing
# their open rows past deadline or older than the max age, with the four verbs
# and their commands, and asks for a report naming what it did per row. Id-less
# because a re-check is a COMMUNICATION, never a task -- an id'd one would open
# a row nobody closes, the defect the chunk exists to remove.

set -euo pipefail

# SELF-GATE -- arming is an env flag, not the timer's mere existence. The
# `enroll: false` manifest keeps the setup backbone from enrolling the unit, and
# this makes dormancy TRUE for the case the manifest cannot cover: a unit
# enrolled by hand, or a fleet that armed the job before reading what it does.
# This door DISPATCHES INTO A LIVE MANAGER SESSION, which is as far from
# read-only as a timer gets, so it must not arrive switched on via a root pull
# (the PLANE_EXPIRE_ENABLED / SESSION_DIGEST_ENABLED pattern). A fleet arms it
# with TASK_RECHECK_ENABLED=1 in its fleet-tier .env; unarmed, the timer fires
# and no-ops LOUDLY.
if [ "${TASK_RECHECK_ENABLED:-0}" != "1" ]; then
    printf 'task-recheck: dormant (set TASK_RECHECK_ENABLED=1 in the fleet .env to arm the re-check)\n' >&2
    exit 0
fi

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${CLAUDLOBBY_ROOT:-$(cd "$LIB_DIR/.." && pwd)}"
export CLAUDLOBBY_ROOT="$ROOT"

FLEET="${1:-${CLAUDLOBBY_FLEET:-}}"
[ $# -gt 0 ] && shift
if [ -z "$FLEET" ]; then
    printf 'task-recheck.sh: no fleet named (usage: task-recheck.sh <fleet> [flags]) -- the plane rows are per fleet\n' >&2
    exit 2
fi

ARGS=(--root "$ROOT" task recheck --fleet "$FLEET" "$@")

if [ -x "$ROOT/.venv/bin/claudlobby" ]; then
    exec "$ROOT/.venv/bin/claudlobby" "${ARGS[@]}"
fi
if command -v claudlobby >/dev/null 2>&1; then
    exec claudlobby "${ARGS[@]}"
fi
# python3 existing is not python3 being USABLE (the plane-daemon.sh note): probe
# the import so an unusable interpreter falls through to the honest 127 rather
# than an exit 1 that looks like a re-check failure.
if command -v python3 >/dev/null 2>&1 \
    && (cd "$ROOT" && python3 -c "import claudlobby" >/dev/null 2>&1); then
    cd "$ROOT"
    exec python3 -m claudlobby "${ARGS[@]}"
fi
printf 'task-recheck.sh: no claudlobby CLI resolvable from %s\n' "$ROOT" >&2
exit 127
