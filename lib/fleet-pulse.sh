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
# Fleet overlay dir (flat local/<fleet> byte-identically, or nested
# local/<system>/<fleet>) — the home for fleet.yaml + the report ledger below.
fleet_dir=$(resolve_fleet_dir "$fleet") || fleet_dir="$CLAUDLOBBY_ROOT/local/$fleet"

# fleet.yaml is authoritative for which bots this fleet owns. Filter the
# runtime-dir glob through it so stale/cross-fleet residue dirs (e.g. a bot
# moved to another fleet, leaving its old runtime dir behind) are never
# health-checked — that mismatch produced false service_down + pane_stuck.
# Empty list (no/unreadable fleet.yaml, e.g. root-mode) → bot_in_fleet returns
# "declared" for every dir, so the sweep falls back to scanning all of them.
declared_bots=$(parse_fleet_bots "$fleet_dir/fleet.yaml")

install_error_trap ""

today=$(date +%Y-%m-%d)
ts=$(ts_iso)

# State directory for pane hashes (persistent across runs)
state_dir="${CLAUDLOBBY_ROOT}/state/pulse"
mkdir -p "$state_dir"

# Dispatch watchdog inputs: the manager-written dispatch ledger and the
# worker-written report ledger (overlay path first, root fallback — matches
# report-back.sh). The overdue matcher cross-references them per bot.
dispatch_log="${CLAUDLOBBY_ROOT}/state/dispatch-log.jsonl"
if [ -d "$fleet_dir/runtime" ]; then
    report_ledger="$fleet_dir/runtime/report-back.jsonl"
else
    report_ledger="${CLAUDLOBBY_ROOT}/runtime/fleet/report-back.jsonl"
fi

# --- Helper: actively notify a bot's manager via its tmux session ---
# The system is pull-based by design, but silent stalls (the reason this exists)
# mean the manager can't rely on polling. We push a one-line [FLEET-PULSE] note
# into the manager's session — the same channel report-back.sh uses — leaving
# the human-facing escalation as the manager's decision (see fleet-observability).
notify_manager() {
    local bot_dir="$1" msg="$2" mgr="" mgr_socket=""
    mgr=$(bot_conf_get "$bot_dir" MANAGER_TMUX "")
    [ -n "$mgr" ] || return 0
    # Manager's private socket: prefer the composed field, else reverse-look-up
    # from its session name among the sibling bots. Without targeting the
    # manager's own socket, the check below would hit the default socket and
    # always pass post-migration — silently killing pulse alerts.
    mgr_socket=$(resolve_peer_socket "$(bot_conf_get "$bot_dir" MANAGER_TMUX_SOCKET "")" "$mgr" "$(dirname "$bot_dir")")
    check_tmux_session "$mgr" "$mgr_socket" || return 0
    # Attribute any send_miss to THIS bot's ledger — it is the one whose manager
    # could not be reached. bot_tmux_send sanitizes + two-step sends.
    BOT_DIR="$bot_dir" BOT_ID="$(basename "$bot_dir")" \
        bot_tmux_send "$mgr_socket" "$mgr" "[FLEET-PULSE] $msg" || true
}

# Wrapper: notify_manager needs bot_dir, but debounce_notify passes only
# the message. We close over _current_bot_dir for each iteration.
_notify_current_bot() {
    notify_manager "$_current_bot_dir" "$1"
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

# --- Pre-sweep: dispatch-overdue scan (once, not per-bot) ---
# Runs dispatch-overdue.py --all to read both ledger files exactly once.
# Output is stored in a temp file for per-bot lookup inside the loop.
_overdue_cache=$(safe_mktemp)
if [ -f "$dispatch_log" ]; then
    python3 "$LIB_DIR/dispatch-overdue.py" --all "$dispatch_log" "$report_ledger" 2>/dev/null > "$_overdue_cache" || true
fi

# --- Iterate all bots ---
for bot_dir in "$BOTS_DIR"/*/; do
    [ -d "$bot_dir" ] || continue
    bot_id=$(basename "$bot_dir")
    bot_in_fleet "$bot_id" "$declared_bots" || continue   # skip undeclared (stale/cross-fleet) dirs
    _current_bot_dir="$bot_dir"

    # Load BOT_SERVICE via the helper (handles `export` prefix + no-match safely).
    BOT_SERVICE=$(bot_conf_get "$bot_dir" BOT_SERVICE "")
    # Resolve the bot's private tmux socket the same way keepalive/start-bot do
    # (TMUX_SOCKET, else BOT_SERVICE) so session liveness is checked on the right
    # server. In production this equals BOT_SERVICE; the indirection also lets the
    # validate-bot-change harness (empty BOT_SERVICE) resolve its fallback socket.
    _bot_socket=$(tmux_socket_for_bot "$bot_dir" 2>/dev/null || true)

    session_name=$(tmux_session_name "$bot_dir")

    # --- Capture pane once per bot (reused by Check 3 + Check 5) ---
    _pane_buf=""
    _session_alive=0
    if check_tmux_session "$session_name" "$_bot_socket"; then
        _session_alive=1
        _pane_buf=$(bot_tmux "$_bot_socket" capture-pane -t "$session_name" -p 2>/dev/null || true)
    fi

    # --- Check 1: tmux session exists ---
    if [ "$_session_alive" -eq 0 ]; then
        emit_fleet_event "session_missing" "pulse" '{"session":"'"$session_name"'"}' "$bot_dir" "$bot_id"
        debounce_notify "$state_dir" "$bot_id" "session_alerted" _notify_current_bot \
            "$bot_id session_missing — tmux session '$session_name' is gone"
    else
        debounce_clear "$state_dir" "$bot_id" "session_alerted"
    fi

    # --- Check 2: systemd service state ---
    if [ -n "$BOT_SERVICE" ] && [ "$_OS" = "Linux" ]; then
        if ! systemctl --user is-active "$BOT_SERVICE" >/dev/null 2>&1; then
            state=$(systemctl --user show -p ActiveState --value "$BOT_SERVICE" 2>/dev/null | tr -d '[:cntrl:]' || echo "unknown")
            emit_fleet_event "service_down" "pulse" '{"unit":"'"$BOT_SERVICE"'","state":"'"$state"'"}' "$bot_dir" "$bot_id"
            debounce_notify "$state_dir" "$bot_id" "service_alerted" _notify_current_bot \
                "$bot_id service_down — unit '$BOT_SERVICE' state=$state"
        else
            debounce_clear "$state_dir" "$bot_id" "service_alerted"
        fi
    fi

    # --- Check 2b: Telegram bridge down (channel up-bots only) ---
    # A live tmux session whose Telegram poller isn't delivering is invisible to
    # the human — surface it. bridge_down_state only counts a bot as down past a
    # post-(re)start grace, so a fleet-wide restart doesn't trip a spurious
    # escalation while pollers respawn. Non-channel bots (no_handle) and
    # indeterminate ownership (unknown) are not actionable and never fire.
    if [ "$_session_alive" -eq 1 ]; then
        # Grace is env-overridable now; fleet.yaml exposure + composer emission
        # are deferred to the observability-config (system-defaults) tier.
        _bridge_grace=$(bot_conf_get "$bot_dir" OBSERVABILITY_BRIDGE_DOWN_GRACE 300)
        if _bridge_st=$(bridge_down_state "$bot_dir" "$_bridge_grace"); then
            emit_fleet_event "bridge_down" "pulse" '{"state":"'"$_bridge_st"'"}' "$bot_dir" "$bot_id"
            debounce_notify "$state_dir" "$bot_id" "bridge_alerted" _notify_current_bot \
                "$bot_id bridge_down — Telegram bridge '$_bridge_st' (live session, poller not delivering)"
        else
            debounce_clear "$state_dir" "$bot_id" "bridge_alerted"
        fi
    fi

    # --- Check 3: pane stuck (>5 min unchanged) ---
    if [ -n "$_pane_buf" ]; then
        pane_content=$(echo "$_pane_buf" | tail -5)
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
                    pane_stuck_threshold=$(bot_conf_get "$bot_dir" OBSERVABILITY_PANE_STUCK_THRESHOLD 300)
                    # Fire only when the pane has been static past the threshold AND
                    # the bot shows NO sign of life by any liveness signal (mirrors
                    # Check 5 activity_stuck + bot_is_busy, on the already-captured
                    # pane). A working-but-static or idle-waiting bot trips one of:
                    #   - idle: data/.idle not older than .last-tool-call (keepalive
                    #     touches .idle when idle; bot-vitals touches .last-tool-call
                    #     on each tool call);
                    #   - recently active: a tool call within the active window; or
                    #   - active turn: an "esc to interrupt" affordance in the pane
                    #     (e.g. a long tool call or waiting on a subagent).
                    # Any of these means busy/idle, NOT stuck.
                    if [ "$elapsed" -ge "$pane_stuck_threshold" ] \
                        && ! marker_is_newer "$bot_dir/data/.idle" "$bot_dir/data/.last-tool-call" \
                        && ! marker_age_within "$bot_dir/data/.last-tool-call" "${KEEPALIVE_ACTIVE_WINDOW_S:-$_ACTIVE_WINDOW_DEFAULT}" \
                        && ! pane_is_busy "$_pane_buf"; then
                        emit_fleet_event "pane_stuck" "pulse" '{"unchanged_since_epoch":'"$prev_ts"',"elapsed_seconds":'"$elapsed"'}' "$bot_dir" "$bot_id"
                    fi
                else
                    # Hash changed — update timestamp
                    printf '%s' "$(date +%s)" > "$ts_file"
                fi
            else
                # First run — seed the hash and timestamp
                printf '%s' "$(date +%s)" > "$ts_file"
            fi
            _tmp_hash="$(safe_mktemp)"
            printf '%s' "$current_hash" > "$_tmp_hash" && mv "$_tmp_hash" "$hash_file"
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
                emit_fleet_event "wip_uncommitted" "pulse" '{"repo":"'"$repo_name"'","dirty_files":'"$file_count"'}' "$bot_dir" "$bot_id"
            fi
        done
    fi

    # --- Check 5: activity stuck (no tool calls for extended period) ---
    # Uses marker-file comparison instead of pane-regex idle detection.
    # keepalive.sh touches data/.idle when it sees an idle pane; bot-vitals.sh
    # touches data/.last-tool-call on every tool call. Comparing mtimes is
    # deterministic and harness-agnostic (works for Claude Code, Codex, Cortex).
    marker="$bot_dir/data/.last-tool-call"
    idle_marker="$bot_dir/data/.idle"
    if [ -f "$marker" ]; then
        # If idle marker is newer than tool-call marker, bot is idle — skip
        if ! marker_is_newer "$idle_marker" "$marker"; then
            threshold=$(bot_conf_get "$bot_dir" OBSERVABILITY_ACTIVITY_STUCK_THRESHOLD 1800)
            now_epoch=$(date +%s)
            last_epoch=$(stat_mtime "$marker" 2>/dev/null || echo "$now_epoch")
            gap=$(( now_epoch - last_epoch ))
            if [ "$gap" -ge "$threshold" ]; then
                emit_fleet_event "activity_stuck" "pulse" \
                    '{"last_tool_call_epoch":'"$last_epoch"',"elapsed_seconds":'"$gap"'}' "$bot_dir" "$bot_id"
                debounce_notify "$state_dir" "$bot_id" "activity_alerted" _notify_current_bot \
                    "$bot_id activity_stuck — no tool calls for ${gap}s while not idle (likely hung mid-task)"
            else
                debounce_clear "$state_dir" "$bot_id" "activity_alerted"
            fi
        else
            debounce_clear "$state_dir" "$bot_id" "activity_alerted"
        fi
    fi

    # --- Check 6: overdue dispatch (from pre-sweep cache) ---
    if [ -s "$_overdue_cache" ]; then
        overdue_lines=$(grep "^${bot_id} " "$_overdue_cache" || true)
        if [ -n "$overdue_lines" ]; then
            oldest_elapsed=0
            overdue_ids=""
            while read -r _bot _da _exp _elapsed _tid; do
                [ -n "${_elapsed:-}" ] || continue
                emit_fleet_event "overdue_dispatch" "pulse" \
                    '{"dispatched_at":'"$_da"',"expected_by":'"$_exp"',"elapsed_seconds":'"$_elapsed"',"task_id":"'"${_tid:--}"'"}' "$bot_dir" "$bot_id"
                [ "$_elapsed" -gt "$oldest_elapsed" ] && oldest_elapsed="$_elapsed"
                if [ "${_tid:--}" != "-" ]; then
                    overdue_ids="${overdue_ids:+$overdue_ids }$_tid"
                fi
            done <<< "$overdue_lines"
            # Self-heal: name the open ids — an id-less re-report cannot
            # close an id'd dispatch.
            debounce_notify "$state_dir" "$bot_id" "dispatch_alerted" _notify_current_bot \
                "$bot_id overdue_dispatch — a dispatched task is ${oldest_elapsed}s past its deadline with no report${overdue_ids:+ — worker must report-back --task <id> for: $overdue_ids}"
        else
            debounce_clear "$state_dir" "$bot_id" "dispatch_alerted"
        fi
    fi

    # Reap old event files for this bot
    reap_events "$bot_dir"
done

# Read-back date span for the escalation + summary below. emit_fleet_event
# stamps each event with a per-call date, so a sweep that straddles midnight
# lands late events in the NEXT day's ledger — past the single script-start
# $today this read-back would otherwise scan. Covering the script-start day plus
# the read-back day (identical unless the sweep crossed midnight; a sub-24h
# sweep spans at most these two) closes that gap. The span tracks the sweep's
# own run, not the escalation window: the summary below has no time filter and
# leans on this span alone for "recent", while the escalation ADDITIONALLY
# filters by _window_start — so a narrower span there can only under-count
# (miss), never over-escalate.
_rb_today=$(date +%Y-%m-%d)
# Echo a bot's existing ledger file(s) across that span, oldest first so a
# downstream `tail -1` still yields the chronologically latest event. An empty
# result (bot emitted nothing in the span) is a normal state, not an error:
# without the explicit return, a missing file on the span's last date makes the
# failed `[ -f ]` the pipeline's exit status under pipefail, and the `$(...)`
# assignment call sites abort the whole pulse via set -e (#610).
_readback_efiles() {
    local _bd="$1" _d _f
    for _d in "$today" "$_rb_today"; do
        _f="$_bd/data/events/fleet-${_d}.jsonl"
        [ -f "$_f" ] && printf '%s\n' "$_f"
    done | sort -u
    return 0
}

# --- Fleet-wide escalation: persistent critical events → Telegram -----------
_ESCALATION_THRESHOLD="${FLEET_PULSE_ESCALATION_THRESHOLD:-2}"
_ESCALATION_WINDOW="${FLEET_PULSE_ESCALATION_WINDOW:-10}"

# Resolve the escalation Telegram target via the shared fleet-alert resolver, so
# this path, lib-common _emit_fleet_signal, and creds-check all resolve the same
# chat-id (override → composed fleet env → bot.conf scan). Fleet-scoped: a fleet's
# escalation must not page a peer fleet's channel, and an empty result keeps the
# loud no-receiver warning below.
resolve_alert_target "$BOTS_DIR" fleet   # sets _alert_chat_id / _alert_state_dir (sourced lib-common)
# shellcheck disable=SC2154
_ESCALATION_CHAT_ID="$_alert_chat_id"
# shellcheck disable=SC2154
_ESCALATION_STATE_DIR="$_alert_state_dir"

# No chat ID anywhere means the critical-alert safety net is mute. Say so
# loudly rather than no-op silently.
if [ -z "$_ESCALATION_CHAT_ID" ]; then
    echo "fleet-pulse: WARNING — no escalation Telegram chat ID resolved; critical fleet alerts will NOT be delivered. Set FLEET_PULSE_ESCALATION_CHAT_ID, or ensure at least one bot's bot.conf defines TELEGRAM_GROUP_CHAT_ID." >&2
fi

if [ -n "$_ESCALATION_CHAT_ID" ]; then
    # Compute window start (portable: GNU date then BSD date fallback)
    _window_start=$(date -d "-${_ESCALATION_WINDOW} minutes" +%Y-%m-%dT%H:%M 2>/dev/null || \
                    date -v-"${_ESCALATION_WINDOW}"M +%Y-%m-%dT%H:%M 2>/dev/null || echo "")

    if [ -n "$_window_start" ]; then
        # rc_timeout is startup-sourced: start-bot.sh emits it once on readiness
        # TIMEOUT and never re-emits it — unlike service_down / bridge_down,
        # which fleet-pulse re-emits from current state each run. Through this
        # window loop it is therefore a BURST detector: it pages when the
        # threshold of bots TIMEOUT within one escalation window — the #533
        # fleet-wide-rollout signature (a mass restart clusters every bot's
        # TIMEOUT). A single or slowly-staggered RC-dark bot spread beyond the
        # window is NOT caught here, and nothing re-checks a live-but-RC-dark
        # session (keepalive only heals DEAD ones); the durable-marker parity
        # fix (mirror bridge_down's startup+pulse legs) is the deferred follow-up.
        for _crit_type in service_down session_missing bridge_down rc_timeout; do
            _affected_bots=""
            _affected_count=0
            for bot_dir in "$BOTS_DIR"/*/; do
                [ -d "$bot_dir" ] || continue
                _bid=$(basename "$bot_dir")
                bot_in_fleet "$_bid" "$declared_bots" || continue
                _efiles=$(_readback_efiles "$bot_dir")
                [ -n "$_efiles" ] || continue
                # Check if this bot has this critical event type within the window
                # shellcheck disable=SC2086  # _efiles: newline list of ledger paths, intentional split
                if grep -q "\"type\":\"$_crit_type\"" $_efiles 2>/dev/null; then
                    # shellcheck disable=SC2086
                    _latest_ts=$(grep -h "\"type\":\"$_crit_type\"" $_efiles | tail -1 | \
                        python3 -c "import sys,json; print(json.loads(sys.stdin.readline())['ts'])" 2>/dev/null || echo "")
                    if [ -n "$_latest_ts" ] && [[ "$_latest_ts" > "$_window_start" ]]; then
                        _affected_bots="$_affected_bots $_bid"
                        _affected_count=$((_affected_count + 1))
                    fi
                fi
            done

            if [ "$_affected_count" -ge "$_ESCALATION_THRESHOLD" ]; then
                _esc_marker="$state_dir/escalation_${_crit_type}"
                # Debounce: only fire once per 10 minutes
                _should_fire=1
                if [ -f "$_esc_marker" ]; then
                    _marker_age=$(( $(date +%s) - $(stat_mtime "$_esc_marker" 2>/dev/null || echo 0) ))
                    [ "$_marker_age" -lt 600 ] && _should_fire=0
                fi
                if [ "$_should_fire" -eq 1 ]; then
                    _msg="FLEET ALERT: $_crit_type on ${_affected_count} bots (${_affected_bots# }). Check fleet health immediately."
                    TELEGRAM_GROUP_CHAT_ID="$_ESCALATION_CHAT_ID" \
                    TELEGRAM_STATE_DIR="${_ESCALATION_STATE_DIR:-}" \
                        "$LIB_DIR/tg-post.sh" "$_msg" 2>/dev/null || true
                    touch "$_esc_marker"
                fi
            else
                # Condition cleared — remove debounce marker
                rm -f "$state_dir/escalation_${_crit_type}" 2>/dev/null || true
            fi
        done
    fi
fi

# --- Human-readable summary ---------------------------------------------------
_summary_file="$state_dir/pulse-summary.txt"
_summary_tmp=$(safe_mktemp)
{
    printf "Fleet pulse: %s — %s\n" "$fleet" "$ts"
    printf "%-12s %-8s %-18s %s\n" "BOT" "SESSION" "SERVICE" "ALERTS"
    printf "%-12s %-8s %-18s %s\n" "---" "-------" "-------" "------"
    for _s_bot_dir in "$BOTS_DIR"/*/; do
        [ -d "$_s_bot_dir" ] || continue
        _s_bid=$(basename "$_s_bot_dir")
        bot_in_fleet "$_s_bid" "$declared_bots" || continue

        _s_session_status="up"
        _s_session_name=$(tmux_session_name "$_s_bot_dir")
        # Resolve the socket the SAME way the main loop does (single source of
        # truth) — a bot whose TMUX_SOCKET differs from BOT_SERVICE must not show a
        # false SESSION DOWN in the summary while the main loop sees it alive.
        _s_socket=$(tmux_socket_for_bot "$_s_bot_dir" 2>/dev/null || true)
        check_tmux_session "$_s_session_name" "$_s_socket" 2>/dev/null || _s_session_status="DOWN"
        # BOT_SERVICE (empty default + [ -n ] guard, mirroring the main loop) drives
        # only the systemd column — a BOT_SERVICE-less bot must not be probed as a unit.
        _s_svc=$(bot_conf_get "$_s_bot_dir" BOT_SERVICE "")
        _s_svc_status="ok"
        if [ "$_OS" = "Linux" ] && [ -n "$_s_svc" ]; then
            systemctl --user is-active "$_s_svc" >/dev/null 2>&1 || _s_svc_status="DOWN"
        fi

        _s_alerts=""
        _s_efiles=$(_readback_efiles "$_s_bot_dir")
        if [ -n "$_s_efiles" ]; then
            for _s_ct in session_missing service_down bridge_down activity_stuck rc_timeout; do
                # shellcheck disable=SC2086  # _s_efiles: newline list of ledger paths, intentional split
                grep -q "\"type\":\"$_s_ct\"" $_s_efiles 2>/dev/null && _s_alerts="$_s_alerts $_s_ct"
            done
        fi
        _s_alerts="${_s_alerts:- none}"
        printf "%-12s %-8s %-18s %s\n" "$_s_bid" "$_s_session_status" "$_s_svc_status" "$_s_alerts"
    done
} > "$_summary_tmp" && mv "$_summary_tmp" "$_summary_file"

cat "$_summary_file"
