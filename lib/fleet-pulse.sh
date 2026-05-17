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

# --- Helper: emit a single event to a bot's event log ---
emit_event() {
    local bot_dir="$1" bot_id="$2" event_type="$3" data_json="$4"
    local events_dir="$bot_dir/data/events"
    mkdir -p "$events_dir"
    local outfile="$events_dir/fleet-${today}.jsonl"
    printf '{"ts":"%s","bot":"%s","type":"%s","source":"pulse","data":%s}\n' \
        "$ts" "$bot_id" "$event_type" "$data_json" >> "$outfile"
}

# --- Helper: reap old event files for a bot ---
reap_events() {
    local bot_dir="$1"
    local events_dir="$bot_dir/data/events"
    if [ -d "$events_dir" ]; then
        find "$events_dir" -name "fleet-*.jsonl" -mtime +7 -delete 2>/dev/null || true
    fi
}

# --- Iterate all bots ---
for bot_dir in "$BOTS_DIR"/*/; do
    [ -d "$bot_dir" ] || continue
    bot_id=$(basename "$bot_dir")

    # Load bot.conf if it exists (for BOT_SERVICE, etc.)
    BOT_SERVICE=""
    if [ -f "$bot_dir/bot.conf" ]; then
        # Extract BOT_SERVICE without full load_bot_conf (avoid side effects)
        BOT_SERVICE=$(grep '^BOT_SERVICE=' "$bot_dir/bot.conf" | head -1 | sed 's/^BOT_SERVICE=//' | tr -d '"')
    fi

    session_name=$(tmux_session_name "$bot_dir")

    # --- Check 1: tmux session exists ---
    if ! check_tmux_session "$session_name"; then
        emit_event "$bot_dir" "$bot_id" "session_missing" '{"session":"'"$session_name"'"}'
    fi

    # --- Check 2: systemd service state ---
    if [ -n "$BOT_SERVICE" ] && [ "$_OS" = "Linux" ]; then
        if ! systemctl --user is-active "$BOT_SERVICE" >/dev/null 2>&1; then
            state=$(systemctl --user is-active "$BOT_SERVICE" 2>/dev/null | tr -d '[:cntrl:]' || echo "unknown")
            emit_event "$bot_dir" "$bot_id" "service_down" '{"unit":"'"$BOT_SERVICE"'","state":"'"$state"'"}'
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

    # Reap old event files for this bot
    reap_events "$bot_dir"
done
