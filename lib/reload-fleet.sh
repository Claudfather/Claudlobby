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
# Needs `claude` and the claudlobby CLI. A timer environment supplies neither on
# its minimal PATH, so this script owns tool resolution itself (own_tool_path +
# claudlobby_cli, lib-common) rather than assuming an interactive PATH.
#
# Usage: reload-fleet.sh [<fleet-name>]
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

own_tool_path   # timer PATH is minimal, and stale units predate #802 (#805)

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$(cd "$LIB_DIR/.." && pwd)}"
export CLAUDLOBBY_ROOT
FLEET="${1:-${CLAUDLOBBY_FLEET:-}}"
# the doors this script runs anchor their fleet events on it (F18 R1)
[ -z "$FLEET" ] || export CLAUDLOBBY_FLEET="${CLAUDLOBBY_FLEET:-$FLEET}"

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
_step_out=$(safe_mktemp)    # reused by every _run_step; one temp, not one per step

_warm_npx() {
    printf '%s npx cache degraded — warming (best-effort, once per episode)\n' "$(ts_iso)" >> "$LOG"
    claudlobby_cli ${FLEET:+--fleet "$FLEET"} warm-cache >> "$LOG" 2>&1 || true
}

# _run_step <label> <command...>
# Run one critical step, appending its output to $LOG, and on failure record a
# reason that says what ACTUALLY went wrong — exit status plus the command's own
# last line of output. The previous code discarded both and reported only the
# last command name, so a PATH failure (exit 127, "claude: command not found")
# surfaced as "claude plugin update failed: <plugin>" and read as a broken
# plugin. That misdirection cost the triage, not the outage (#805). Both the
# plugin loop and generate route through here so the two cannot drift.
_run_step() {
    local label="$1"; shift
    local cmd="$1" rc=0 detail
    "$@" >"$_step_out" 2>&1 || rc=$?
    cat "$_step_out" >> "$LOG"
    [ "$rc" -eq 0 ] && return 0
    # Last non-blank line of the command's own output — the real error text.
    detail=$(grep -v '^[[:space:]]*$' "$_step_out" | tail -n 1)
    # 127 is the unresolvable-tool signature: name the command and the PATH it
    # was not found on, so the alert points at the install rather than the work.
    [ "$rc" -eq 127 ] && detail="$cmd not found on PATH=$PATH"
    printf '%s failed (exit %d)%s' "$label" "$rc" "${detail:+: $detail}" > "$_reason_file"
    return 1
}

_reload_critical() {
    # step 0: npx cache preflight. A cold cache turns MCP startup into an IO
    # storm on SD-card hardware, so verify before touching plugins — inside
    # the lock, because warm-cache mutates the host-shared ~/.npm/_npx.
    # Cache health never aborts the reload, and a degraded cache warms at
    # most once per episode (debounced): a permanently-missing package must
    # not become a daily warm loop.
    if "$LIB_DIR/check-npx-cache.sh" ${FLEET:+--fleet "$FLEET"} >> "$LOG" 2>&1; then
        debounce_clear "${CLAUDLOBBY_ROOT}/state" "npx" "warm-attempted"
    else
        debounce_notify "${CLAUDLOBBY_ROOT}/state" "npx" "warm-attempted" _warm_npx ""
    fi
    # Tool preflight: fail on the MISSING TOOL rather than on whichever command
    # happened to run first. A timer env resolves neither by default, and an
    # unresolvable tool is an install/PATH fault — naming it is the whole
    # difference between a 5-minute fix and a two-day silent outage.
    if [ -n "$PLUGINS" ] && ! command -v claude >/dev/null 2>&1; then
        printf 'claude not found on PATH=%s — install Claude Code or set CLAUDE_BIN' "$PATH" > "$_reason_file"
        return 1
    fi
    local _p
    for _p in $PLUGINS; do
        _run_step "claude plugin update $_p" claude plugin update "$_p" || return 1
    done
    _run_step "claudlobby generate" claudlobby_cli ${FLEET:+--fleet "$FLEET"} generate || return 1
}

if ! with_lock "${CLAUDLOBBY_ROOT}/state/reload-fleet.lock" _reload_critical; then
    reason=$(cat "$_reason_file" 2>/dev/null || echo "reload download/generate failed")
    loud_fail "$reason"
    exit 1
fi
# $_reason_file lives under lib-common's _LC_TMPDIR, reaped by its EXIT trap.

# --- 3. mark every RUNNING bot for a keepalive-driven live reload ---
# fleet.yaml is authoritative for which bots this fleet owns. Filter the runtime
# glob through it so stale/cross-fleet residue dirs are never marked for reload.
# BOTS_DIR is resolve_bots_dir's local/<fleet>/runtime/bots (or runtime/bots in
# root mode), so its grandparent holds fleet.yaml for both. Empty list (no/
# unreadable fleet.yaml) → bot_in_fleet treats every dir as declared.
# #1146: over-inclusive, not a no-op — a drifted manifest marks .reload-pending
# inside every bot dir on the host, other fleets included.
declared_bots=$(parse_fleet_bots "$(dirname "$(dirname "$BOTS_DIR")")/fleet.yaml")
marked=0
if [ -d "$BOTS_DIR" ]; then
    for bot_dir in "$BOTS_DIR"/*/; do
        [ -d "$bot_dir" ] || continue
        bot_in_fleet "$(basename "$bot_dir")" "$declared_bots" || continue   # departed/cross-fleet residue → skip
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
