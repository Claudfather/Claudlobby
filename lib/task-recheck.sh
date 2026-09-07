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

# OFF-SWITCH -- an opt-OUT since the defaults flip (chunk N). The re-check is
# the reaction the target workflow is FOR, so it ships ON: unset, or any value
# but an exact 0, runs. A fleet turns it off with TASK_RECHECK_ENABLED=0 in its
# fleet-tier .env, and the composer stamps that 0 onto this unit (a timer runs
# in a CLOSED env and sources no .env -- #1383).
#
# The no-op is LOUD on purpose. A silent skip is indistinguishable from a
# broken timer, and the whole point of the ruling is that a disabled reaction
# must never be invisible: this line is what an operator finds in the journal
# when they ask why nothing was re-checked. `claudlobby doctor`'s switches rung
# and `claudlobby status`'s header say the same thing before they have to ask.
#
# Only an exact 0 disarms. An EMPTY assignment is a win at its tier (#1213) but
# is not a 0, so `export TASK_RECHECK_ENABLED=` leaves the door on -- the same
# rule env_tiers.resolves_to applies everywhere else.
if [ "${TASK_RECHECK_ENABLED:-1}" = "0" ]; then
    printf 'task-recheck: OFF for this fleet (TASK_RECHECK_ENABLED=0) -- no re-check will be sent; unset it, or set 1, to restore the default\n' >&2
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
