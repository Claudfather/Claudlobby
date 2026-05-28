#!/usr/bin/env bash
# fleet-pulse.sh — External liveness checks for all bots in a fleet.
#
# Runs as a cron job (no LLM). Iterates all bot directories in a fleet
# and checks: tmux session alive, systemd service state, pane freshness,
# uncommitted git WIP.
#
# Writes events to each bot's data/events/fleet-YYYY-MM-DD.jsonl with
# source: "pulse". Same schema as bot-vitals.sh.
#
# Usage: lib/fleet-pulse.sh <fleet-name>

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

fleet="${1:?Usage: fleet-pulse.sh <fleet-name>}"

BOTS_DIR=$(resolve_bots_dir "$fleet")
if [ ! -d "$BOTS_DIR" ]; then
    echo "fleet-pulse: bots directory not found: $BOTS_DIR" >&2
    exit 1
fi

today=$(date +%Y-%m-%d)
ts=$(ts_iso)

# State directory for pane hashes (persistent across runs)
state_dir="${CLAUDLOBBY_ROOT}/state/pulse"
mkdir -p "$state_dir"

# Dispatch watchdog inputs: the manager-written dispatch ledger and the
# worker-written report ledger (overlay path first, root fallback — matches
# report-back.sh). The overdue matcher cross-references them per bot.
dispatch_log="${CLAUDLOBBY_ROOT}/state/dispatch-log.jsonl"
if [ -d "${CLAUDLOBBY_ROOT}/local/${fleet}/runtime" ]; then
    report_ledger="${CLAUDLOBBY_ROOT}/local/${fleet}/runtime/report-back.jsonl"
else
    report_ledger="${CLAUDLOBBY_ROOT}/runtime/fleet/report-back.jsonl"
fi

# --- Helper: emit a single event to a bot's event log ---
emit_event() {
    local bot_dir="$1" bot_id="$2" event_type="$3" data_json="$4"
    local events_dir="$bot_dir/data/events"
    mkdir -p "$events_dir"
    local outfile="$events_dir/fleet-${today}.jsonl"
    printf '{"ts":"%s","bot":"%s","type":"%s","source":"pulse","data":%s}\n' \
        "$ts" "$bot_id" "$event_type" "$data_json" >> "$outfile"
}

# --- Helper: read a value from a bot's bot.conf (no side effects) ---
bot_conf_get() {
    local bot_dir="$1" key="$2" default="$3" val=""
    if [ -f "$bot_dir/bot.conf" ]; then
        val=$(grep "^\(export \)\?$key=" "$bot_dir/bot.conf" | head -1 \
            | sed -E "s/^(export )?$key=//" | tr -d '"' || true)
    fi
    printf '%s' "${val:-$default}"
}

# --- Helper: actively notify a bot's manager via its tmux session ---
# The system is pull-based by design, but silent stalls (the reason this exists)
# mean the manager can't rely on polling. We push a one-line [FLEET-PULSE] note
# into the manager's session — the same channel report-back.sh uses — leaving
# the human-facing escalation as the manager's decision (see fleet-observability).
notify_manager() {
    local bot_dir="$1" msg="$2" mgr=""
    mgr=$(bot_conf_get "$bot_dir" MANAGER_TMUX "")
    [ -n "$mgr" ] || return 0
    check_tmux_session "$mgr" || return 0
    "$_TMUX_BIN" send-keys -t "$mgr" "[FLEET-PULSE] $(sanitize_tmux_input "$msg")" Enter 2>/dev/null || true
}

# --- Helper: reap old event files for a bot (honors OBSERVABILITY_REAP_DAYS) ---
reap_events() {
    local bot_dir="$1"
    local events_dir="$bot_dir/data/events"
    local reap_days
    reap_days=$(bot_conf_get "$bot_dir" OBSERVABILITY_REAP_DAYS 7)
    if [ -d "$events_dir" ]; then
        find "$events_dir" -name "fleet-*.jsonl" -mtime +"$reap_days" -delete 2>/dev/null || true
    fi
}

# --- Iterate all bots ---
for bot_dir in "$BOTS_DIR"/*/; do
    [ -d "$bot_dir" ] || continue
    bot_id=$(basename "$bot_dir")

    # Load BOT_SERVICE via the helper (handles `export` prefix + no-match safely).
    BOT_SERVICE=$(bot_conf_get "$bot_dir" BOT_SERVICE "")

    session_name=$(tmux_session_name "$bot_dir")

    # --- Check 1: tmux session exists ---
    if ! check_tmux_session "$session_name"; then
        emit_event "$bot_dir" "$bot_id" "session_missing" '{"session":"'"$session_name"'"}'
        if [ ! -f "$state_dir/${bot_id}.session_alerted" ]; then
            notify_manager "$bot_dir" "$bot_id session_missing — tmux session '$session_name' is gone"
            touch "$state_dir/${bot_id}.session_alerted"
        fi
    else
        rm -f "$state_dir/${bot_id}.session_alerted"
    fi

    # --- Check 2: systemd service state ---
    if [ -n "$BOT_SERVICE" ] && [ "$_OS" = "Linux" ]; then
        if ! systemctl --user is-active "$BOT_SERVICE" >/dev/null 2>&1; then
            state=$(systemctl --user show -p ActiveState --value "$BOT_SERVICE" 2>/dev/null | tr -d '[:cntrl:]' || echo "unknown")
            emit_event "$bot_dir" "$bot_id" "service_down" '{"unit":"'"$BOT_SERVICE"'","state":"'"$state"'"}'
            if [ ! -f "$state_dir/${bot_id}.service_alerted" ]; then
                notify_manager "$bot_dir" "$bot_id service_down — unit '$BOT_SERVICE' state=$state"
                touch "$state_dir/${bot_id}.service_alerted"
            fi
        else
            rm -f "$state_dir/${bot_id}.service_alerted"
        fi
    fi

    # --- Check 3: pane stuck (>5 min unchanged) ---
    if check_tmux_session "$session_name"; then
        pane_content=$("$_TMUX_BIN" capture-pane -t "$session_name" -p 2>/dev/null | tail -5 || true)
        if [ -n "$pane_content" ]; then
            current_hash=$(printf '%s' "$pane_content" | md5sum 2>/dev/null | cut -d' ' -f1 || printf '%s' "$pane_content" | md5 2>/dev/null || echo "nohash")
            hash_file="$state_dir/${bot_id}.pane_hash"
            ts_file="$state_dir/${bot_id}.pane_ts"

            if [ -f "$hash_file" ]; then
                prev_hash=$(cat "$hash_file")
                if [ "$current_hash" = "$prev_hash" ] && [ -f "$ts_file" ]; then
                    prev_ts=$(cat "$ts_file")
                    now_epoch=$(date +%s)
                    elapsed=$(( now_epoch - prev_ts ))
                    if [ "$elapsed" -ge 300 ]; then
                        emit_event "$bot_dir" "$bot_id" "pane_stuck" '{"unchanged_since_epoch":'"$prev_ts"',"elapsed_seconds":'"$elapsed"'}'
                    fi
                else
                    # Hash changed — update timestamp
                    printf '%s' "$(date +%s)" > "$ts_file"
                fi
            else
                # First run — seed the hash and timestamp
                printf '%s' "$(date +%s)" > "$ts_file"
            fi
            printf '%s' "$current_hash" > "$hash_file"
        fi
    fi

    # --- Check 4: git WIP (uncommitted changes in projects/) ---
    if [ -d "$bot_dir/projects" ]; then
        for repo_dir in "$bot_dir/projects"/*/; do
            [ -d "$repo_dir/.git" ] || continue
            wip=$(git -C "$repo_dir" status --porcelain 2>/dev/null | head -5 || true)
            if [ -n "$wip" ]; then
                repo_name=$(basename "$repo_dir")
                file_count=$(git -C "$repo_dir" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
                emit_event "$bot_dir" "$bot_id" "wip_uncommitted" '{"repo":"'"$repo_name"'","dirty_files":'"$file_count"'}'
            fi
        done
    fi

    # --- Check 5: activity stuck (animating but no tool calls) ---
    # pane_stuck (Check 3) is fooled by the braille spinner, which animates even
    # during a hang — the pane hash keeps changing, so it never fires. This check
    # uses the .last-tool-call marker bot-vitals.sh touches on every tool call:
    # session alive + not idle + no tool call for > threshold ⇒ activity_stuck.
    marker="$bot_dir/data/.last-tool-call"
    alerted="$state_dir/${bot_id}.activity_alerted"
    if check_tmux_session "$session_name" && [ -f "$marker" ]; then
        threshold=$(bot_conf_get "$bot_dir" OBSERVABILITY_ACTIVITY_STUCK_THRESHOLD 1800)
        now_epoch=$(date +%s)
        last_epoch=$(stat_mtime "$marker" 2>/dev/null || echo "$now_epoch")
        gap=$(( now_epoch - last_epoch ))
        pane_tail=$("$_TMUX_BIN" capture-pane -t "$session_name" -p 2>/dev/null | tail -10 || true)
        if [ "$gap" -ge "$threshold" ] && ! pane_is_idle "$pane_tail"; then
            emit_event "$bot_dir" "$bot_id" "activity_stuck" \
                '{"last_tool_call_epoch":'"$last_epoch"',"elapsed_seconds":'"$gap"'}'
            if [ ! -f "$alerted" ]; then
                notify_manager "$bot_dir" "$bot_id activity_stuck — no tool calls for ${gap}s while not idle (likely hung mid-task)"
                touch "$alerted"
            fi
        else
            rm -f "$alerted"
        fi
    fi

    # --- Check 6: overdue dispatch (manager-side watchdog) ---
    # A task this bot was dispatched is overdue if its deadline passed with no
    # terminal [BOTREPORT]. The matcher (dispatch-overdue.py) cross-references
    # the dispatch ledger against the report ledger. Notify the manager, debounced.
    if [ -f "$dispatch_log" ]; then
        overdue_out=$(python3 "$LIB_DIR/dispatch-overdue.py" "$bot_id" "$dispatch_log" "$report_ledger" 2>/dev/null || true)
        if [ -n "$overdue_out" ]; then
            oldest_elapsed=0
            while read -r _da _exp _elapsed; do
                [ -n "${_elapsed:-}" ] || continue
                emit_event "$bot_dir" "$bot_id" "overdue_dispatch" \
                    '{"dispatched_at":'"$_da"',"expected_by":'"$_exp"',"elapsed_seconds":'"$_elapsed"'}'
                [ "$_elapsed" -gt "$oldest_elapsed" ] && oldest_elapsed="$_elapsed"
            done <<< "$overdue_out"
            if [ ! -f "$state_dir/${bot_id}.dispatch_alerted" ]; then
                notify_manager "$bot_dir" "$bot_id overdue_dispatch — a dispatched task is ${oldest_elapsed}s past its deadline with no report"
                touch "$state_dir/${bot_id}.dispatch_alerted"
            fi
        else
            rm -f "$state_dir/${bot_id}.dispatch_alerted"
        fi
    fi

    # Reap old event files for this bot
    reap_events "$bot_dir"
done
