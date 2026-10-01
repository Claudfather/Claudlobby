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

switch_is_on PLANE_PRUNE_ENABLED plane-prune "metric samples will accumulate without bound" || exit 0

_claudlobby_require_cli
exec "$CLAUDLOBBY_CLI" --root "$CLAUDLOBBY_ROOT" plane prune "$@"
