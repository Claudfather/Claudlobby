#!/bin/bash
# heavy-slot-guard.sh — PreToolUse: a heavy job takes the host's heavy-job slot
# (#1686).
#
# Heavy jobs stacked across fleets have stormed the primary host (load 25-57,
# iowait up to 66%, swap full), and a prose rule saying "one at a time" cannot
# hold across a score of bots in four fleets. This hook is the enforcement
# point: for each Bash tool call it puts claudlobby/_runtime_scripts/heavy-slot.py's wrapper in front of
# every heavy command (a whole pytest or vitest suite, an npm/pnpm/yarn install,
# a test or build script, next build, Playwright, Chromium) and leaves every
# other byte alone; when every slot is taken, or a free slot is another
# caller's turn in the queue, it refuses the call before anything runs, naming
# the holder or the turn. The decision lives in heavy-slot.py (stdlib,
# unit-tested); this file is the cheap front door.
#
# COMPOSED ONLY FOR A BOT THAT OPTED IN (`heavy_slot: true` in fleet.yaml): a
# bot that did not runs no process at all for it, rather than a hook that
# checks a switch and exits. A composed hook is live on every bot the next
# generate composes it for (#1310), so the manifest key is the only canary.
#
# THE COST ON A BOT THAT OPTED IN: one bash and one `cat` per Bash call; python
# only when the payload names a heavy tool (the prefilter below).
#
# FAILS OPEN, LOUDLY: no python3 or a decider that cannot answer lets the call
# through and leaves a breadcrumb. Blocking every Bash call on a bot is a worse
# outage than one unslotted heavy job.
#
# THE OFF SWITCH, host-wide and instant: $CLAUDLOBBY_ROOT/state/heavy-slot/disabled.
set -uo pipefail

case "${BASH_SOURCE[0]}" in
*/*) LIB_DIR="${BASH_SOURCE[0]%/*}" ;;
*) LIB_DIR=. ;;
esac
# The slot is host state under the data root, never beside this release's code.
SLOT_DIR="${HEAVY_SLOT_DIR:-}"
if [ -z "$SLOT_DIR" ]; then
    case "${CLAUDLOBBY_ROOT:-}" in
    /*) SLOT_DIR="$CLAUDLOBBY_ROOT/state/heavy-slot" ;;
    esac
fi
PLANE_EMIT_CLASS=hook   # a live Claude Code turn waits on this hook (#1693)

[ -n "$SLOT_DIR" ] && [ -e "$SLOT_DIR/disabled" ] && exit 0

# --- the prefilter: no python for a call that names no heavy tool ------------
# It over-matches on purpose (`npm` in a description, `next build` in a
# heredoc, `pip`/`uv` as a substring): a false positive costs one python start,
# a false negative is a stacked heavy job. flock/xargs/sh -c wrappers need no
# entry of their own: the heavy tool they wrap still names itself in the payload.
payload="$(cat)"
case "$payload" in
*pytest* | *py.test* | *vitest* | *npm* | *yarn* | *npx* | *"next build"* | *playwright* | *chrom* | *pip* | *uv*) ;;
*) exit 0 ;;
esac
case "$payload" in
*Bash*) ;;
*) exit 0 ;;
esac

_bail() { # <reason> — fail open, but leave a breadcrumb
    # lib-common is sourced HERE, not at the top: every Bash call whose payload
    # merely names a heavy tool would otherwise pay to parse it. And in a
    # subshell: an `exit` inside a sourced file ends the shell whatever `|| true`
    # surrounds it, and a fail-open hook must not fail because its breadcrumb did.
    (
        # shellcheck source=lib-common.sh
        . "$LIB_DIR/lib-common.sh" &&
            emit_script_error "" "heavy-slot-guard.sh" 1 \
                "$1 — heavy-job slot INACTIVE for this call"
    ) >/dev/null 2>&1 || true
    exit 0
}

[ -n "$SLOT_DIR" ] || _bail "no absolute CLAUDLOBBY_ROOT data directory"
PY_BIN="$(command -v python3 || true)"
[ -n "$PY_BIN" ] || _bail "python3 not available"
out="$("$PY_BIN" "$LIB_DIR/heavy-slot.py" hook <<<"$payload")" || _bail "decider failed"
[ -z "$out" ] || printf '%s\n' "$out"
exit 0
