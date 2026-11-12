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
# The comparison is switch_is_on (lib-common) — polarity in one place.
case "${BASH_SOURCE[0]}" in
    */*) LIB_DIR="${BASH_SOURCE[0]%/*}" ;;
    *) LIB_DIR="." ;;
esac
# shellcheck source=cli-context.sh
. "$LIB_DIR/cli-context.sh"
_claudlobby_require_root
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

switch_is_on PLANE_EXPIRE_ENABLED plane-expire "no assignment will be expired" || exit 0

_claudlobby_require_cli
exec "$CLAUDLOBBY_CLI" --root "$CLAUDLOBBY_ROOT" plane expire "$@"
