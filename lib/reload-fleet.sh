#!/bin/bash
# reload-fleet.sh — Mechanism 1 of the fleet update lifecycle: refresh plugins
# and composed skills LIVE, with no restart and no context loss.
#
# Steps (the download + generate run under one fleet-wide lock so a daily-timer
# run and an on-demand run can never relink .claude/skills symlinks concurrently):
#   1. claude plugin update <each FLEET_PLUGINS_REQUIRED> — refresh the shared
#      host plugin cache (~/.claude/plugins/cache, shared fleet-wide).
#   2. claudlobby generate — re-link composed skills.
#   3. drop data/.reload-pending on every RUNNING bot. keepalive.sh performs the
#      actual /reload-plugins + /reload-skills at each bot's next idle tick — a
#      single, idle-gated activation path (fork F2(b) in the update-lifecycle plan).
#
# Triggered daily by the reload-fleet systemd timer, and runnable on demand to
# push a release immediately (activation still lands at each bot's next idle
# keepalive tick, <=60s).
#
# A failed download or generate is LOUD, never silent: it emits a reload_failed
# fleet-observability event AND alerts the manager (tmux nudge + Telegram
# escalation), and marks NO bot — there is no half-reload.
#
# Needs `claude` and `claudlobby` on PATH (same as update-claude-code.sh needs npm).
#
# Usage: reload-fleet.sh [<fleet-name>]
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$(cd "$LIB_DIR/.." && pwd)}"
export CLAUDLOBBY_ROOT
FLEET="${1:-${CLAUDLOBBY_FLEET:-}}"

BOTS_DIR="$(resolve_bots_dir "$FLEET")"
mkdir -p "${CLAUDLOBBY_ROOT}/state"
LOG="${CLAUDLOBBY_ROOT}/state/reload-fleet.log"

# LOUD failure — emit the reload_failed event + alert the manager via the shared
# lib-common primitive (emit_failure_alert is also used by Mechanism 2's
# update-claude-code.sh, so the two mechanisms never fork the alert path).
loud_fail() {
    local reason="$1"
    printf '%s reload_failed: %s\n' "$(ts_iso)" "$reason" >> "$LOG"
    emit_failure_alert "$BOTS_DIR" "reload_failed" "$reason"
}

# --- 1 + 2. download + generate, serialized under a fleet-wide lock ---
PLUGINS=""
_plugin_bot=$(first_bot_with_conf "$BOTS_DIR" FLEET_PLUGINS_REQUIRED || true)
[ -n "$_plugin_bot" ] && PLUGINS=$(bot_conf_get "$_plugin_bot" FLEET_PLUGINS_REQUIRED "")
_reason_file=$(safe_mktemp)

_reload_critical() {
    local _p
    for _p in $PLUGINS; do
        if ! claude plugin update "$_p" >> "$LOG" 2>&1; then
            printf 'claude plugin update failed: %s' "$_p" > "$_reason_file"
            return 1
        fi
    done
    if [ -n "$FLEET" ]; then
        claudlobby --fleet "$FLEET" generate >> "$LOG" 2>&1 || { printf 'claudlobby generate failed' > "$_reason_file"; return 1; }
    else
        claudlobby generate >> "$LOG" 2>&1 || { printf 'claudlobby generate failed' > "$_reason_file"; return 1; }
    fi
}

if ! with_lock "${CLAUDLOBBY_ROOT}/state/reload-fleet.lock" _reload_critical; then
    reason=$(cat "$_reason_file" 2>/dev/null || echo "reload download/generate failed")
    loud_fail "$reason"
    exit 1
fi
# $_reason_file lives under lib-common's _LC_TMPDIR, reaped by its EXIT trap.

# --- 3. mark every RUNNING bot for a keepalive-driven live reload ---
marked=0
if [ -d "$BOTS_DIR" ]; then
    for bot_dir in "$BOTS_DIR"/*/; do
        [ -d "$bot_dir" ] || continue
        # "Running" = the bot's session is alive on its OWN per-bot server.
        if check_tmux_session "$(tmux_session_name "$bot_dir")" "$(tmux_socket_for_bot "$bot_dir" 2>/dev/null || true)"; then
            mkdir -p "$bot_dir/data"
            touch "$bot_dir/data/.reload-pending"
            marked=$((marked + 1))
        fi
    done
fi
printf '%s reload-fleet: download + generate OK, marked %d running bot(s) for live reload\n' \
    "$(ts_iso)" "$marked" >> "$LOG"
