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

# --- fleet-observability event (mirrors emit_script_error's fleet-level path) ---
emit_reload_event() {
    local event_type="$1" reason="$2"
    local events_dir="${CLAUDLOBBY_ROOT}/state/events"
    mkdir -p "$events_dir"
    local ts today escaped
    ts=$(ts_iso); today=$(date +%Y-%m-%d); escaped=$(json_escape "$reason")
    printf '{"ts":"%s","bot":"fleet","type":"%s","source":"reload","data":{"reason":"%s"}}\n' \
        "$ts" "$event_type" "$escaped" >> "$events_dir/fleet-${today}.jsonl"
}

# --- manager alert: tmux nudge + Telegram escalation (the fleet-pulse path) ---
alert_manager() {
    local msg="$1" mgr_bot mgr chat_bot chat_id state_dir
    # tmux nudge to the fleet manager (resolve from whichever bot declares it).
    mgr_bot=$(first_bot_with_conf "$BOTS_DIR" MANAGER_TMUX || true)
    mgr=$(bot_conf_get "$mgr_bot" MANAGER_TMUX "")  # "" when no bot declares one
    if [ -n "$mgr" ] && check_tmux_session "$mgr"; then
        "$_TMUX_BIN" send-keys -t "$mgr" "[RELOAD-FAIL] $(sanitize_tmux_input "$msg")" Enter || true
    fi
    # Telegram escalation (loudest channel) — mirror fleet-pulse chat-id resolution.
    chat_id="${FLEET_PULSE_ESCALATION_CHAT_ID:-}"
    if [ -z "$chat_id" ]; then
        chat_bot=$(first_bot_with_conf "$BOTS_DIR" TELEGRAM_GROUP_CHAT_ID || true)
        if [ -n "$chat_bot" ]; then
            chat_id=$(bot_conf_get "$chat_bot" TELEGRAM_GROUP_CHAT_ID "")
            state_dir=$(bot_conf_get "$chat_bot" TELEGRAM_STATE_DIR "")
        fi
    fi
    if [ -n "$chat_id" ]; then
        TELEGRAM_GROUP_CHAT_ID="$chat_id" TELEGRAM_STATE_DIR="${state_dir:-}" \
            "$LIB_DIR/tg-post.sh" "FLEET RELOAD FAILED: $msg" >/dev/null 2>&1 || true
    fi
}

loud_fail() {
    local reason="$1"
    printf '%s reload_failed: %s\n' "$(ts_iso)" "$reason" >> "$LOG"
    emit_reload_event "reload_failed" "$reason"
    alert_manager "$reason"
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
        if check_tmux_session "$(tmux_session_name "$bot_dir")"; then
            mkdir -p "$bot_dir/data"
            touch "$bot_dir/data/.reload-pending"
            marked=$((marked + 1))
        fi
    done
fi
printf '%s reload-fleet: download + generate OK, marked %d running bot(s) for live reload\n' \
    "$(ts_iso)" "$marked" >> "$LOG"
