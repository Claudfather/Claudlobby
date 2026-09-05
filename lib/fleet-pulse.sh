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
# The fleet is this sweep's carrier for every door it runs (emit_fleet_event
# anchors on it): the timer unit stamps CLAUDLOBBY_FLEET, a hand run does not.
export CLAUDLOBBY_FLEET="${CLAUDLOBBY_FLEET:-$fleet}"

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

# Re-notify window for the debounced pushes below (#831). A changed recipient
# re-fires immediately; this is the second leg, covering what identity cannot
# see — a send that failed silently, a reused pid, or simply an episode long
# enough that one delivery has stopped being a live signal. The 2026-07-27
# outage ran ~360 ticks on a single delivery. Set 0 to disable.
_RENOTIFY_AFTER_S="${FLEET_PULSE_RENOTIFY_AFTER_S:-21600}"  # 6h

# Dispatch watchdog inputs: the manager-written dispatch ledger and the
# worker-written report ledger (overlay path first, root fallback — matches
# report-back.sh). The overdue matcher cross-references them per bot.

# --- Helpers: push to a bot's manager, and identify which manager instance ---
# The manager this bot notifies, as "<socket>|<session>" (empty when none is
# configured). One resolver, because the push and the recipient identity below
# must agree on which session they mean.
_manager_target() {
    local bot_dir="$1" mgr="" mgr_socket=""
    mgr=$(bot_conf_get "$bot_dir" MANAGER_TMUX "")
    [ -n "$mgr" ] || return 0
    # Manager's private socket: prefer the composed field, else reverse-look-up
    # from its session name among the sibling bots. Without targeting the
    # manager's own socket, the check below would hit the default socket and
    # always pass post-migration — silently killing pulse alerts.
    mgr_socket=$(resolve_peer_socket "$(bot_conf_get "$bot_dir" MANAGER_TMUX_SOCKET "")" "$mgr" "$(dirname "$bot_dir")")
    printf '%s|%s' "$mgr_socket" "$mgr"
}

# Identity of the manager session INSTANCE this bot would notify (#831).
# session_created alone is not enough: start-bot.sh runs CLAUDE_CMD as the pane
# command, so pane_pid IS the claude process, and a claude restart inside a
# surviving session loses the message just as surely as a session restart.
# Empty when no manager resolves or the session is gone — itself a distinct
# recipient value, so an alert that fired into the void re-fires once a real
# manager appears.
_mgr_token_key=""; _mgr_token_val=""
# Sets _mgr_token rather than echoing it: a `$( )` capture runs in a subshell,
# which would discard the memo below and silently turn one tmux round-trip per
# sweep back into one per bot per tick -- a cache that never hits.
_resolve_manager_token() {
    local target mgr_socket mgr
    _mgr_token=""
    target=$(_manager_target "$1") || return 0
    [ -n "$target" ] || return 0
    # Memoized on the resolved target: a fleet shares one manager, so the round
    # trip is paid once per sweep, not once per bot on every healthy fleet.
    if [ "$target" != "$_mgr_token_key" ]; then
        _mgr_token_key="$target"
        mgr_socket="${target%%|*}"; mgr="${target##*|}"
        _mgr_token_val=$(bot_tmux "$mgr_socket" display-message -p -t "$mgr" \
            '#{session_created}-#{pane_pid}' 2>/dev/null || true)
    fi
    _mgr_token="$_mgr_token_val"
}

notify_manager() {
    local bot_dir="$1" msg="$2" target="" mgr="" mgr_socket=""
    target=$(_manager_target "$bot_dir") || return 0
    [ -n "$target" ] || return 0
    mgr_socket="${target%%|*}"; mgr="${target##*|}"
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
    reap_event_files "$events_dir" "fleet-*.jsonl" "$reap_days"
}

# --- Pre-sweep: dispatch-overdue scan (once, not per-bot) ---
# Runs dispatch-overdue.py --all to read both ledger files exactly once.
# Output is stored in a temp file for per-bot lookup inside the loop.
# --bots-dir enables respawn detection (#835): a past-deadline row whose worker
# restarted after it was dispatched is split into the orphan set instead of the
# overdue set. The session holding that task id is gone, so it can never be
# echoed and the row would alarm every cycle until it aged out.
_overdue_cache=$(safe_mktemp)
_orphan_cache=$(safe_mktemp)
# The overdue reader's rc and stderr are KEPT (chunk 5): once the reader is
# flipped to the plane, a refusal (rc 3 -- the plane unreachable, or holding no
# bot of this fleet) leaves the cache EMPTY, and an empty cache reads as "nothing
# overdue". _overdue_reader_guard turns that into a page, after the escalation
# chat is resolved below.
_overdue_reader_rc=0
_overdue_reader_err=$(safe_mktemp)
# The matcher reads the plane of THIS fleet (F18 R2a): no ledger paths, no
# file-exists gate — a gate on the retired file once switched the whole
# pre-sweep OFF on a host whose files were gone (found by the R1 harness port).
python3 "$LIB_DIR/dispatch-overdue.py" --all --fleet "$fleet" \
    --bots-dir "$BOTS_DIR" 2>"$_overdue_reader_err" > "$_overdue_cache" || _overdue_reader_rc=$?
python3 "$LIB_DIR/dispatch-overdue.py" --orphans --fleet "$fleet" \
    --bots-dir "$BOTS_DIR" 2>/dev/null > "$_orphan_cache" || true


# --- Cutover overdue-reader guard (chunk 5): a REFUSED --all is not "nothing overdue" ---
# The flipped overdue reader refuses (rc 3) rather than falling back to the
# JSONL when the plane cannot serve; the sweep above keeps its rc and stderr,
# and this guard pages -- debounced, like every other notice here -- so a dark
# watchdog is a paged watchdog. rc 0 clears the marker; any other rc is
# disclosed on stderr and never paged (rc 2 is a call-shape bug, not an outage).
_overdue_page() {
    local _rc=0
    TELEGRAM_GROUP_CHAT_ID="$_ESCALATION_CHAT_ID" TELEGRAM_STATE_DIR="${_ESCALATION_STATE_DIR:-}" \
        "$LIB_DIR/tg-post.sh" "$1" >/dev/null 2>&1 || _rc=$?
    if [ "$_rc" -ne 0 ]; then
        printf '%s ALERT-DELIVERY-FAILED escalation overdue_reader_unreachable: tg-post exit %s -- will retry next pass\n' "$(ts_iso)" "$_rc" >&2
        _OVERDUE_PAGE_FAILED=1
    fi
}

_overdue_reader_guard() {
    case "${_overdue_reader_rc:-0}" in
        0) debounce_clear "$state_dir" fleet overdue_reader_unreachable; return 0 ;;
        3) ;;
        *) echo "fleet-pulse: overdue reader exited ${_overdue_reader_rc}: $(tail -1 "${_overdue_reader_err:-/dev/null}" 2>/dev/null | cut -c1-160)" >&2; return 0 ;;
    esac
    local _why
    _why=$(grep -m1 UNREACHABLE "${_overdue_reader_err:-/dev/null}" 2>/dev/null | cut -c1-200)
    [ -n "$_ESCALATION_CHAT_ID" ] || { echo "fleet-pulse: overdue reader UNREACHABLE and no escalation chat - the watchdog is dark for overdue dispatches: ${_why}" >&2; return 0; }
    _OVERDUE_PAGE_FAILED=0
    debounce_notify "$state_dir" fleet overdue_reader_unreachable _overdue_page \
        "FLEET ALERT: the overdue reader for ${fleet} is UNREACHABLE - the watchdog cannot see overdue dispatches until the plane is restored, or PLANE_READ_OVERDUE is flipped back to 0. (${_why})" \
        "" 600 || true
    [ "${_OVERDUE_PAGE_FAILED:-0}" = "1" ] && debounce_clear "$state_dir" fleet overdue_reader_unreachable
    return 0
}

# _emit_new_orphans <bot_dir> <bot_id>
# Record each orphaned dispatch ONCE, the first sweep it is seen (#835).
#
# An aged-out row (#460) is an abandoned task — nothing to do. An orphan is work
# the fleet lost to its OWN restart, which is actionable: it can be re-dispatched.
# Making it inert for the alarm and otherwise unrecorded would trade this issue's
# noise for the silence of #826/#831/#833, which defeats the operator the same way.
#
# Latched on task-id set membership, not a time window: orphan-ness is monotonic
# (once .spawn is newer than the dispatch it stays newer), so "have I seen this
# id" is the natural once-only test and it needs none of debounce_notify's
# machinery. It also survives the marker moving — a bot dir reset that removes
# .spawn would otherwise let long-dead rows revert to overdue and alarm afresh.
# Recorded to the event ledger only; deliberately no manager push, because the
# whole point is that these are not the operator's emergency.
_emit_new_orphans() {
    local _o_dir="$1" _o_bot="$2" _seen="$state_dir/${2}.orphaned" _tid
    [ -s "$_orphan_cache" ] || return 0
    while read -r _ob _oda _oexp _oel _tid; do
        [ "${_tid:--}" != "-" ] || continue
        grep -qxF "$_tid" "$_seen" 2>/dev/null && continue
        printf '%s\n' "$_tid" >> "$_seen"
        emit_fleet_event "dispatch_orphaned" "pulse" \
            '{"dispatched_at":'"$_oda"',"expected_by":'"$_oexp"',"task_id":"'"$_tid"'","reason":"worker respawned after dispatch"}' \
            "$_o_dir" "$_o_bot"
    done < <(grep "^${_o_bot} " "$_orphan_cache" || true)
}

# --- Check 7 input: the #1024 mirror — reported, never re-tasked ------------
# Scanned lazily and at most once per sweep, on the first bot that has the check
# armed. The scan is fleet-wide but the knobs are per-bot, so a fleet with the
# check off everywhere pays nothing at all, and a fleet whose bots are tuned
# differently still pays for exactly one scan.
#
# The matcher is deliberately asked for NO threshold: it reports every
# unretasked worker with its idle time, and each bot applies its own policy
# below. Same split activity_stuck already uses — the helper owns the join, the
# caller owns the policy.
_unassigned_cache=""
_unassigned_scanned=0
_ensure_unassigned_scan() {
    [ "$_unassigned_scanned" -eq 0 ] || return 0
    _unassigned_scanned=1
    _unassigned_cache=$(safe_mktemp)
    # rc kept (chunk 7a): a flipped reader REFUSES (rc 3) when the plane cannot
    # serve, and an empty cache would read as "no idle workers"; the refusal is
    # disclosed on stderr (the overdue reader pages; the idle check is quieter
    # by design — it is an advisory notice, not an alert).
    _unassigned_rc=0
    python3 "$LIB_DIR/dispatch-overdue.py" --unassigned --fleet "$fleet" \
        2>"$_unassigned_cache.err" > "$_unassigned_cache" || _unassigned_rc=$?
    if [ "$_unassigned_rc" -ne 0 ]; then
        echo "fleet-pulse: the idle-worker reader exited ${_unassigned_rc} — worker_unassigned cannot be judged this pass: $(tail -1 "$_unassigned_cache.err" 2>/dev/null | cut -c1-160)" >&2
        : > "$_unassigned_cache"
    fi
    rm -f "$_unassigned_cache.err"
}

# A bot.conf value that must be an integer. A non-numeric (or empty) setting
# would abort the whole sweep at the `-gt` below under set -e, so it degrades to
# the default rather than taking the fleet's pulse down with it.
_int_or() {  # _int_or <value> <default>
    case "${1:-}" in
        ''|*[!0-9-]*) printf '%s' "$2" ;;
        *)            printf '%s' "$1" ;;
    esac
}

# --- Iterate all bots ---
for bot_dir in "$BOTS_DIR"/*/; do
    [ -d "$bot_dir" ] || continue
    bot_id=$(basename "$bot_dir")
    bot_in_fleet "$bot_id" "$declared_bots" || continue   # skip undeclared (stale/cross-fleet) dirs
    _current_bot_dir="$bot_dir"
    # Who the debounced pushes below would reach, resolved once per bot. A
    # restart mid-episode changes this, which is what re-arms the alert.
    _resolve_manager_token "$bot_dir"

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

    # --- Boot gate: is this bot's supervised start still in flight? ---
    # A bot whose unit is mid-start has no tmux session and no active unit YET,
    # which is indistinguishable from a dead one by state alone — so Checks 1
    # and 2 both fire on a perfectly healthy boot. Evidence (#1002): on the
    # 2026-08-04 boot storm saul emitted session_missing at 17:27:24 and
    # service_down state=activating at 17:27:26, one tick, one healthy boot, two
    # pages. A service_down whose own payload says "activating" is self-proving
    # as a false positive, and it was the only service_down in that day's ledger.
    #
    # Computed once, before both checks, so they cannot disagree about whether
    # this bot is booting. Mid-start is NO VERDICT, not an all-clear: neither
    # emit nor debounce_clear runs, leaving any genuine pre-existing alert state
    # intact to re-fire once the unit settles.
    _svc_starting=0
    if [ -n "$BOT_SERVICE" ] && service_is_starting "$BOT_SERVICE"; then
        _svc_starting=1
    fi

    # --- Check 1: tmux session exists ---
    if [ "$_svc_starting" -eq 1 ]; then
        : # boot in flight — the session is expected to be absent
    elif [ "$_session_alive" -eq 0 ]; then
        emit_fleet_event "session_missing" "pulse" '{"session":"'"$session_name"'"}' "$bot_dir" "$bot_id"
        debounce_notify "$state_dir" "$bot_id" "session_alerted" _notify_current_bot \
            "$bot_id session_missing — tmux session '$session_name' is gone" "$_mgr_token" "$_RENOTIFY_AFTER_S"
    else
        debounce_clear "$state_dir" "$bot_id" "session_alerted"
    fi

    # --- Check 2: supervised service state (systemd on Linux, launchd on macOS) ---
    # Liveness via service_is_active (the OS dispatch lives there). The payload
    # state string stays per-OS: systemd exposes ActiveState; launchd print has no
    # cheap sub-state, so a confirmed-down job is labeled not-loaded.
    if [ -n "$BOT_SERVICE" ] && [ "$_svc_starting" -eq 0 ]; then
        if ! service_is_active "$BOT_SERVICE"; then
            if [ "$_OS" = "Darwin" ]; then
                state="not-loaded"
            else
                state=$(systemctl --user show -p ActiveState --value "$BOT_SERVICE" 2>/dev/null | tr -d '[:cntrl:]' || echo "unknown")
            fi
            emit_fleet_event "service_down" "pulse" '{"unit":"'"$BOT_SERVICE"'","state":"'"$state"'"}' "$bot_dir" "$bot_id"
            debounce_notify "$state_dir" "$bot_id" "service_alerted" _notify_current_bot \
                "$bot_id service_down — unit '$BOT_SERVICE' state=$state" "$_mgr_token" "$_RENOTIFY_AFTER_S"
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
        # `|| true` INSIDE the substitution: bridge_down_state returns 1 on
        # every HEALTHY bot (up, in grace, no handle) and prints only when the
        # bridge is actionably down, and on bash 3.2 a non-zero substitution
        # inside an `if` still fires the inherited ERR trap — one phantom
        # `script_error` per live bot per sweep, ~2,500 a day on a 9-bot fleet
        # (measured 2026-09-03/04; the keepalive reaper carried the same class).
        _bridge_st=$(bridge_down_state "$bot_dir" "$_bridge_grace" || true)
        if [ -n "$_bridge_st" ]; then
            emit_fleet_event "bridge_down" "pulse" '{"state":"'"$_bridge_st"'"}' "$bot_dir" "$bot_id"
            debounce_notify "$state_dir" "$bot_id" "bridge_alerted" _notify_current_bot \
                "$bot_id bridge_down — Telegram bridge '$_bridge_st' (live session, poller not delivering)" "$_mgr_token" "$_RENOTIFY_AFTER_S"
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
                    "$bot_id activity_stuck — no tool calls for ${gap}s while not idle (likely hung mid-task)" "$_mgr_token" "$_RENOTIFY_AFTER_S"
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
            # Name the open ids. The echo instruction is no longer the
            # self-heal it was: report-back.sh resolves the open dispatch
            # itself now (#835), so a row still overdue here means no report
            # arrived at all, or the worker holds several open at once. Name
            # the ids for the manager to act on, not for the worker to echo.
            debounce_notify "$state_dir" "$bot_id" "dispatch_alerted" _notify_current_bot \
                "$bot_id overdue_dispatch — a dispatched task is ${oldest_elapsed}s past its deadline with no report${overdue_ids:+ — no report has closed: $overdue_ids}" "$_mgr_token" "$_RENOTIFY_AFTER_S"
        else
            debounce_clear "$state_dir" "$bot_id" "dispatch_alerted"
        fi
    fi
    _emit_new_orphans "$bot_dir" "$bot_id"

    # --- Check 7: reported, then never re-tasked (#1024) — DEFAULT OFF -------
    # The mirror of Check 6, and the incident it closes ran 16 hours: a worker
    # delivered, reported, and was never dispatched again. Nothing could see it.
    # activity_stuck structurally cannot — a genuinely idle bot IS idle, so
    # keepalive re-stamps `.idle` and that branch never fires. Correct for its
    # purpose, and exactly why this case was invisible.
    #
    # Off unless a fleet arms it, because it is the first check whose subject is
    # the ASSIGNMENT loop rather than a process: it reports that the manager
    # stopped assigning, which a fleet without someone to act on it can only
    # read as noise.
    if [ "$(bot_conf_get "$bot_dir" OBSERVABILITY_UNASSIGNED_CHECK 0)" = "1" ]; then
        # A manager has no assigner, so reported-and-not-re-tasked is its normal
        # resting state, not a strand. Uses bot_is_manager and never a
        # hand-rolled MANAGER_TMUX read: the composed line carries a trailing
        # comment that a naive parse swallows, which once reported three
        # managers as workers across three fleets.
        if bot_is_manager "$bot_dir"; then
            debounce_clear "$state_dir" "$bot_id" "unassigned_alerted"
        else
            _ensure_unassigned_scan
            _ua_thresh=$(_int_or "$(bot_conf_get "$bot_dir" OBSERVABILITY_UNASSIGNED_THRESHOLD 7200)" 7200)
            _ua_maxage=$(_int_or "$(bot_conf_get "$bot_dir" OBSERVABILITY_UNASSIGNED_MAX_AGE 86400)" 86400)
            _ua_line=""
            [ -n "$_unassigned_cache" ] && \
                _ua_line=$(grep "^${bot_id} " "$_unassigned_cache" 2>/dev/null || true)
            _ua_fire=0; _ua_rts=0; _ua_idle=0; _ua_tid="-"; _ua_status="-"
            if [ -n "$_ua_line" ]; then
                read -r _ _ua_rts _ua_idle _ua_tid _ua_status <<< "$_ua_line"
                if [ "$_ua_idle" -gt "$_ua_thresh" ]; then
                    # Past max_age the strand stops being news — it is a known
                    # state, not an event, and re-paging it on every renotify
                    # window is how a real alert becomes wallpaper. Measured:
                    # two bots on this host last reported 66 DAYS ago. <= 0
                    # disables the cap, matching DISPATCH_OVERDUE_MAX_AGE_S.
                    #
                    # AND THE FLIP SIDE, because this check exists to close a
                    # SILENT gap and the cap reopens one: past max_age it stops
                    # firing and the else-branch below clears the debounce
                    # marker, so the emitted signal becomes indistinguishable
                    # from resolved. A strand outliving 24h goes quiet again.
                    # Bounded rather than immediate — roughly 3-4 pushes at the
                    # 6h renotify cadence first — and an exact structural mirror
                    # of overdue_dispatch's own expiry, so it is a deliberate
                    # trade and not an oversight. Set <= 0 to refuse the trade
                    # (found by vera, review of #1121).
                    if [ "$_ua_maxage" -le 0 ] || [ "$_ua_idle" -le "$_ua_maxage" ]; then
                        _ua_fire=1
                    fi
                fi
            fi
            if [ "$_ua_fire" -eq 1 ]; then
                emit_fleet_event "worker_unassigned" "pulse" \
                    '{"reported_at":'"$_ua_rts"',"idle_seconds":'"$_ua_idle"',"task_id":"'"$_ua_tid"'","last_status":"'"$_ua_status"'"}' "$bot_dir" "$bot_id"
                _ua_tail=""
                [ "$_ua_tid" != "-" ] && _ua_tail=" (last task: $_ua_tid)"
                debounce_notify "$state_dir" "$bot_id" "unassigned_alerted" _notify_current_bot \
                    "$bot_id worker_unassigned — reported $_ua_status ${_ua_idle}s ago with no dispatch since${_ua_tail}" "$_mgr_token" "$_RENOTIFY_AFTER_S"
            else
                debounce_clear "$state_dir" "$bot_id" "unassigned_alerted"
            fi
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
# assignment call sites abort the whole pulse via set -e (#610). The `|| true`
# states that same tolerance to the ERR trap, which `return 0` cannot: the return
# masks the status for errexit, but the trap has already fired by then, so under
# errtrace (#844) a normal empty span logged a script_error every pulse — on the
# per-minute path. Suppressing at the statement is what marks a benign non-zero
# as intended; masking it afterwards only hides it from one of the two readers.
# Phase B: the events readers follow the flip — flag AND a recorded declaration
# for the `events` reader, asked ONCE per sweep; a flag alone is disclosed and
# the files keep serving. An UNREACHABLE plane under the flag is a THIRD state,
# never the files: after the retirement they hold nothing, so a fallback would
# read an outage as a quiet fleet (the watchdog gone dark in silence).
_EVENTS_SOURCE=""          # files | plane | unreachable, decided once per sweep
_events_why=""
_events_from_plane() {
    if [ -z "$_EVENTS_SOURCE" ]; then
        _EVENTS_SOURCE=files
        if [ "${PLANE_READ_EVENTS:-0}" = "1" ]; then
            local _decl _rc=0
            _decl=$(python3 -S -E "$LIB_DIR/plane-lookup.py" --root "$CLAUDLOBBY_ROOT" --declared events \
                --fleet "$fleet" 2>"$state_dir/.events-err") || _rc=$?
            if [ "$_rc" -ne 0 ]; then
                _EVENTS_SOURCE=unreachable
                _events_why=$(tail -1 "$state_dir/.events-err" 2>/dev/null | cut -c1-200)
                echo "fleet-pulse: PLANE_READ_EVENTS=1 but the plane is UNREACHABLE (rc=$_rc): ${_events_why} - critical events cannot be judged this pass" >&2
            elif [ -n "$_decl" ]; then
                _EVENTS_SOURCE=plane
            else
                echo "fleet-pulse: PLANE_READ_EVENTS=1 but no cutover_declared for $fleet/events - the events readers keep the files (plane cutover --reader events)" >&2
            fi
            rm -f "$state_dir/.events-err"
        fi
    fi
    [ "$_EVENTS_SOURCE" = plane ]
}
# The plane's CRITICAL set inside a window, read ONCE per window per sweep into
# a cache file (`<bot> <type> <latest>` per row) — the escalation loop asks
# inside its own window, the summary inside the read-back span; a sweep used
# to spawn the lookup once per bot per type. Not a subshell call: a failed read
# flips the source to unreachable for the rest of the sweep.
_plane_critical() {   # $1 = window start (a naive local instant, or ISO), $2 = cache path
    local _rc=0
    python3 -S -E "$LIB_DIR/plane-lookup.py" --root "$CLAUDLOBBY_ROOT" --escalation \
        --since "$1" --fleet "$fleet" >"$2" 2>"$state_dir/.events-err" || _rc=$?
    if [ "$_rc" -ne 0 ]; then
        _EVENTS_SOURCE=unreachable
        _events_why=$(tail -1 "$state_dir/.events-err" 2>/dev/null | cut -c1-200)
        echo "fleet-pulse: critical-events reader UNREACHABLE (rc=$_rc): ${_events_why} - not judged this pass" >&2
        rm -f "$state_dir/.events-err" "$2"; return 1
    fi
    rm -f "$state_dir/.events-err"
    return 0
}
_CRITICAL_ESCALATION_TYPES="service_down session_missing bridge_down rc_timeout"
_CRITICAL_SUMMARY_TYPES="session_missing service_down bridge_down activity_stuck rc_timeout"
_rb_yesterday=$(date -u -v-1d +%Y-%m-%dT00:00:00Z 2>/dev/null || date -u -d "yesterday" +%Y-%m-%dT00:00:00Z 2>/dev/null || echo "")

_readback_efiles() {
    local _bd="$1" _d _f
    for _d in "$today" "$_rb_today"; do
        _f="$_bd/data/events/fleet-${_d}.jsonl"
        [ -f "$_f" ] && printf '%s\n' "$_f"
    done | sort -u || true
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

# Phase B twin of _overdue_reader_guard: the events reader that could not be
# reached under a declared flip has left the watchdog dark for critical fleet
# events — paged (debounced), cleared once it reads again. Runs AFTER the
# escalation and summary reads, which are what discover the outage.
_events_page() {
    local _rc=0
    TELEGRAM_GROUP_CHAT_ID="$_ESCALATION_CHAT_ID" TELEGRAM_STATE_DIR="${_ESCALATION_STATE_DIR:-}" \
        "$LIB_DIR/tg-post.sh" "$1" >/dev/null 2>&1 || _rc=$?
    if [ "$_rc" -ne 0 ]; then
        printf '%s ALERT-DELIVERY-FAILED escalation events_reader_unreachable: tg-post exit %s -- will retry next pass\n' "$(ts_iso)" "$_rc" >&2
        _EVENTS_PAGE_FAILED=1
    fi
}
_events_reader_guard() {
    if [ "${_EVENTS_SOURCE:-}" != unreachable ]; then
        debounce_clear "$state_dir" fleet events_reader_unreachable
        return 0
    fi
    [ -n "$_ESCALATION_CHAT_ID" ] || { echo "fleet-pulse: events reader UNREACHABLE and no escalation chat - the watchdog is dark for critical fleet events: ${_events_why}" >&2; return 0; }
    _EVENTS_PAGE_FAILED=0
    debounce_notify "$state_dir" fleet events_reader_unreachable _events_page \
        "FLEET ALERT: the events reader for ${fleet} is UNREACHABLE - critical fleet events cannot be judged until the plane is restored, or PLANE_READ_EVENTS is flipped back to 0. (${_events_why})" \
        "" 600 || true
    [ "${_EVENTS_PAGE_FAILED:-0}" = "1" ] && debounce_clear "$state_dir" fleet events_reader_unreachable
    return 0
}

_overdue_reader_guard || true

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
        _esc_cache="$state_dir/.critical-window"
        _esc_ok=0
        if _events_from_plane; then
            _plane_critical "$_window_start" "$_esc_cache" && _esc_ok=1
        fi
        for _crit_type in $_CRITICAL_ESCALATION_TYPES; do
            _affected_bots=""
            _affected_count=0
            if [ "$_EVENTS_SOURCE" = files ]; then
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
            elif [ "$_esc_ok" -eq 1 ]; then
                # Phase B: the same question from the plane's one read — which
                # declared bots carry this critical type inside the window
                while read -r _bid _btype _latest_ts; do
                    [ "$_btype" = "$_crit_type" ] || continue
                    bot_in_fleet "$_bid" "$declared_bots" || continue
                    _affected_bots="$_affected_bots $_bid"
                    _affected_count=$((_affected_count + 1))
                done < "$_esc_cache"
            else
                continue                  # unreachable under the flag: not judged this pass (disclosed once, paged below)
            fi
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
                    _esc_rc=0
                    _esc_err=$(TELEGRAM_GROUP_CHAT_ID="$_ESCALATION_CHAT_ID" \
                    TELEGRAM_STATE_DIR="${_ESCALATION_STATE_DIR:-}" \
                        "$LIB_DIR/tg-post.sh" "$_msg" 2>&1) || _esc_rc=$?
                    if [ "$_esc_rc" -eq 0 ]; then
                        touch "$_esc_marker"
                    else
                        # DO NOT touch the marker. It is what suppresses re-firing
                        # for the whole debounce window, so touching it on failure
                        # means a send that reached nobody buys itself silence --
                        # and the condition is never raised again while it lasts.
                        # Leaving it absent makes the next pass retry, which is the
                        # only rung here that repairs itself once a token is fixed.
                        printf '%s ALERT-DELIVERY-FAILED escalation %s: tg-post exit %s (%s) -- debounce marker NOT set, will retry next pass\n' \
                            "$(ts_iso)" "$_crit_type" "$_esc_rc" "$(printf '%s' "$_esc_err" | tr '\n' ' ' | cut -c1-200)" >&2
                        emit_fleet_event "alert_delivery_failed" "pulse" \
                            "$(printf '{"for_event":"%s","channel":"telegram","exit":%s,"debounced":false}' \
                                "$(json_escape "$_crit_type")" "$_esc_rc")" "" fleet
                    fi
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
        # the service column via service_is_active — a BOT_SERVICE-less bot must not
        # be probed as a unit.
        _s_svc=$(bot_conf_get "$_s_bot_dir" BOT_SERVICE "")
        _s_svc_status="ok"
        if [ -n "$_s_svc" ] && ! service_is_active "$_s_svc"; then
            _s_svc_status="DOWN"
        fi
        # Same boot gate as the main loop, for the same reason and by the same
        # predicate. Without it this run reports two verdicts about one bot: the
        # loop correctly stays silent for a mid-boot bot while this block prints
        # SESSION DOWN / SERVICE DOWN to the journal and pulse-summary.txt — the
        # pre-fix answer, from the file that fixed it. "starting" is its own
        # column value, never folded into "up": a boot is not health.
        if [ -n "$_s_svc" ] && service_is_starting "$_s_svc"; then
            [ "$_s_session_status" = "DOWN" ] && _s_session_status="starting"
            [ "$_s_svc_status" = "DOWN" ] && _s_svc_status="starting"
        fi

        _s_alerts=""
        if _events_from_plane; then
            # ONE read for the whole summary (the read-back span), on the first bot
            if [ -z "${_rb_read:-}" ]; then
                _rb_read=1; _rb_ok=0
                _plane_critical "$_rb_yesterday" "$state_dir/.critical-readback" && _rb_ok=1
            fi
            if [ "${_rb_ok:-0}" -eq 1 ]; then
                for _s_ct in $_CRITICAL_SUMMARY_TYPES; do
                    grep -q "^${_s_bid} ${_s_ct} " "$state_dir/.critical-readback" 2>/dev/null && _s_alerts="$_s_alerts $_s_ct"
                done
            fi
        fi
        if [ "$_EVENTS_SOURCE" = files ]; then
            _s_efiles=$(_readback_efiles "$_s_bot_dir")
            if [ -n "$_s_efiles" ]; then
                for _s_ct in $_CRITICAL_SUMMARY_TYPES; do
                    # shellcheck disable=SC2086  # _s_efiles: newline list of ledger paths, intentional split
                    grep -q "\"type\":\"$_s_ct\"" $_s_efiles 2>/dev/null && _s_alerts="$_s_alerts $_s_ct"
                done
            fi
        elif [ "$_EVENTS_SOURCE" = unreachable ]; then
            _s_alerts=" unknown (events reader unreachable)"    # never "none": an outage is not a quiet bot
        fi
        _s_alerts="${_s_alerts:- none}"
        printf "%-12s %-8s %-18s %s\n" "$_s_bid" "$_s_session_status" "$_s_svc_status" "$_s_alerts"
    done
} > "$_summary_tmp" && mv "$_summary_tmp" "$_summary_file"
_events_reader_guard || true
rm -f "$state_dir/.critical-window" "$state_dir/.critical-readback"

cat "$_summary_file"
