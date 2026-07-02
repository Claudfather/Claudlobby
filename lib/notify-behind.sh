#!/usr/bin/env bash
# notify-behind.sh — the source-currency nudge (notify-only, never pulls).
#
# Reports how many commits the shared claudlobby install (CLAUDLOBBY_ROOT) is
# behind origin/main. When behind, raises a FLEET NOTICE (fleet event + manager
# tmux nudge + Telegram) telling the operator how to pull; when in sync, stays
# silent. Read-only by contract: the ff-only auto-pull (root-self-update) is a
# separate opt-in follow-up, deliberately NOT part of the default tier — this
# nudge is the only default-on source-currency behavior.
#
# Quiet-failure discipline: an offline host (fetch fails) or a non-git root
# logs + exits 0, leaving a script_error breadcrumb in state/events — a missed
# nudge is low-urgency and must never become alert noise. Re-nudge cadence is
# the timer schedule itself (one notice per run while behind); no debounce
# state to manage.
#
# Usage: notify-behind.sh [<fleet-name>]
#   The optional fleet name scopes signal routing; the composed host unit
#   passes none, and routing falls back across local/<fleet>/runtime/bots.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

FLEET="${1:-${CLAUDLOBBY_FLEET:-}}"
BOTS_DIR="$(resolve_bots_dir "$FLEET")"
LOG_DIR="${CLAUDLOBBY_ROOT}/state"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/notify-behind.log"

ts=$(ts_iso)

if ! git -C "$CLAUDLOBBY_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    echo "$ts SKIP — $CLAUDLOBBY_ROOT is not a git checkout" >> "$LOG"
    exit 0
fi

if ! with_timeout 120 git -C "$CLAUDLOBBY_ROOT" fetch --quiet origin main 2>>"$LOG"; then
    echo "$ts FETCH FAILED — source currency unknown (nudge skipped)" >> "$LOG"
    emit_script_error "" "notify-behind.sh" 1 "git fetch origin main failed — source currency unknown"
    exit 0
fi

behind=$(git -C "$CLAUDLOBBY_ROOT" rev-list --count HEAD..origin/main)

if [ "$behind" -gt 0 ]; then
    echo "$ts BEHIND $behind — notice raised" >> "$LOG"
    emit_fleet_notice "$BOTS_DIR" "source_behind" \
        "claudlobby source on $(hostname) is $behind commit(s) behind origin/main — apply with: git -C $CLAUDLOBBY_ROOT pull --ff-only"
else
    echo "$ts IN SYNC with origin/main" >> "$LOG"
fi
exit 0
