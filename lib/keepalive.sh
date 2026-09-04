#!/bin/bash
# Bot keepalive — restart dead sessions, log idle state.
# Usage: keepalive.sh /path/to/bot/dir
#
# IMPORTANT: this script does NOT press Enter on idle panes EXCEPT for the one
# gated reload path below. Pressing Enter at an empty prompt submits any "ghost"
# text — including Claude Code's greyed-out auto-completion suggestions — which
# causes the bot to act on input the user never typed. The reload path is safe
# because it types a FIXED slash command as the first input, then Enter (so Enter
# submits that command, never ghost text), and fires ONLY when a fleet reload is
# pending (data/.reload-pending) and the pane is confirmed IDLE. This is the
# single activation point for Mechanism 1 of the fleet update lifecycle:
# reload-fleet.sh marks bots; keepalive performs the /reload at the next idle tick.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

BOT_DIR="${1:?Usage: keepalive.sh /path/to/bot/dir}"
load_bot_conf "$BOT_DIR"
install_error_trap "$BOT_DIR"
TMUX_SESSION="$(tmux_session_name "$BOT_DIR")"
# Per-bot tmux server socket (see start-bot.sh) — same SSOT resolver so the
# watchdog checks the bot on its OWN server, not the shared default socket.
# Fail fast (like start-bot.sh) on an unresolvable socket rather than aborting
# silently on errexit.
TMUX_SOCKET="$(tmux_socket_for_bot "$BOT_DIR")" || {
    echo "keepalive.sh: cannot resolve tmux socket for $BOT_DIR (check BOT_SERVICE in bot.conf)" >&2
    exit 1
}

LOG="$BOT_DIR/keepalive.log"

# JSONL retention — delete keepalive event files older than this many days.
# Honors the one fleet-wide retention window (observability.reap_days, composed as
# OBSERVABILITY_REAP_DAYS into bot.conf, loaded above) so every event writer
# (keepalive, fleet-pulse, bot-vitals) reaps on the same horizon. An explicit
# KEEPALIVE_REAP_DAYS still overrides; both fall back to 7.
KEEPALIVE_REAP_DAYS="${KEEPALIVE_REAP_DAYS:-${OBSERVABILITY_REAP_DAYS:-7}}"

# Emit a keepalive event. Cutover B2: a TRANSITION (RESTART, BRIDGE_HEAL, SKIP,
# RELOAD) is a FLEET EVENT on the plane through the one door (emit_fleet_event:
# provenance, alias-anchored, retired with the family), so `claudlobby events`
# and `uptime` see it; the per-tick verdicts BUSY / IDLE / UNKNOWN ride the
# heartbeat sample the same tick emits (plane_presence_samples) and are not
# fleet events. The keepalive-<day>.jsonl file has NO reader in the estate
# (measured: 867 rows/bot/day, only the validate harness ever opened it): it
# keeps being written under dual-write and stops the day the events write is
# retired — the flag alone gates it, because the four facts protect a RECORD
# and a file nothing reads is not one.
emit_keepalive_event() {
    local ev_state="$1"
    local ev_detail="${2:-}"
    case "$ev_state" in
        RESTART)     emit_fleet_event keepalive_restart keepalive "{\"detail\":\"$(json_escape "$ev_detail")\"}" "$BOT_DIR" "$BOT_NAME" || true ;;
        BRIDGE_HEAL) emit_fleet_event bridge_heal keepalive "{\"detail\":\"$(json_escape "$ev_detail")\"}" "$BOT_DIR" "$BOT_NAME" || true ;;
        SKIP)        emit_fleet_event keepalive_skip keepalive "{\"detail\":\"$(json_escape "$ev_detail")\"}" "$BOT_DIR" "$BOT_NAME" || true ;;
        RELOAD)      emit_fleet_event keepalive_reload keepalive "{\"detail\":\"$(json_escape "$ev_detail")\"}" "$BOT_DIR" "$BOT_NAME" || true ;;
    esac
    local _wflag
    if [ -n "${PLANE_LEGACY_WRITE_EVENTS+x}" ]; then
        _wflag="${PLANE_LEGACY_WRITE_EVENTS:-1}"
    else
        _wflag=$(plane_fleet_tier_value "${FLEET_NAME:-${CLAUDLOBBY_FLEET:-}}" PLANE_LEGACY_WRITE_EVENTS 1)
    fi
    [ "$_wflag" = "0" ] && return 0
    local events_dir="$BOT_DIR/data/events"
    mkdir -p "$events_dir"
    local events_file="$events_dir/keepalive-$(date +%Y-%m-%d).jsonl"
    local ts
    ts=$(ts_iso)
    local detail_json=""
    if [ -n "$ev_detail" ]; then
        detail_json=',"detail":"'"$(json_escape "$ev_detail")"'"'
    fi
    printf '{"ts":"%s","bot":"%s","type":"keepalive","source":"keepalive","data":{"state":"%s"%s}}\n' \
        "$ts" "$BOT_NAME" "$ev_state" "$detail_json" >> "$events_file"

    # Reap old keepalive JSONL files beyond retention window.
    reap_event_files "$events_dir" 'keepalive-*.jsonl' "$KEEPALIVE_REAP_DAYS"
}

# ---- plane door: presence's RECORDED half (#1361, harvest item 1) ----------
# The verdict this tick ALREADY computed, emitted as ONE metric_sample per
# tick through the shim — the table, contract and metric names shipped in
# migration 0006 with no emitter until now. Subject is the INSTANCE alias
# (bot:<fleet>/<name>): identity resolution at ingest lands on the SAME uid
# the registry keyframes use, so heartbeat samples join equipment/history
# with no glue. Arming reaches this script as an Environment= line stamped
# on the keepalive job unit from the fleet tier cascade (the #1383
# mechanism — a scheduler env is closed, so the fleet-tier .env alone
# never arrives here). NON-BLOCKING rc-wise AND clock-wise (background
# emit, pid-guarded); the view sampler keeps rendering pixels and
# classifies NOTHING — the recorded half lives here, the sibling's
# two-truths split stays forsworn. The live tick emits bot.heartbeat only
# (session-up-ness is derivable from heartbeat presence); the dead-session
# path emits the one fact heartbeat cannot carry, bot.session_up=false,
# and NO heartbeat — no pane was classified, and a fabricated verdict is
# the lie this lane exists to kill. The SKIP paths (boot in flight,
# restart race) deliberately emit nothing — transitional, the next tick
# records (unpinned, disclosed).
plane_presence_samples() {
    local verdict="$1"   # BUSY|IDLE|UNKNOWN, or DOWN (the dead-session fact)
    # THE arming predicate (lib-common) — a hand-rolled copy here re-forked
    # what #1384 consolidated, with silent identity skips the ruling calls
    # drift (r2 gauntlet). plane_armed is if-safe under set -e.
    if ! plane_armed keepalive --require-fleet --require-bot; then
        return 0
    fi
    # No pileup on a wedged rung (r2 gauntlet, probed): the cold-CLI rung
    # has no wall-clock bound and keepalive-all sweeps ticks SEQUENTIALLY —
    # one D-state stall must never wedge the whole fleet's watchdog. The
    # emit runs in a BACKGROUND subshell, and a tick whose previous emit is
    # still in flight SKIPS: presence tolerates a gap, the reader types
    # staleness. rc-wise the tick never depends on the record; disclosures
    # land in keepalive.log, not the journal.
    # The claim is honored only while FRESH (2 ticks): kill -0 alone let a
    # RE-USED pid block a bot indefinitely — proven live within minutes of
    # deploy (takahashi: pidfile held 1543, occupied by an unrelated
    # long-lived process; zero heartbeats across every sweep while eight
    # siblings recorded). A stale claim means wedge-or-reuse and both want
    # one new emit; pileup stays bounded at ~one background proc per 120s.
    local pidf="$BOT_DIR/data/.plane-presence.pid" prev
    prev="$(cat "$pidf" 2>/dev/null || true)"
    if [ -n "$prev" ] && kill -0 "$prev" 2>/dev/null \
       && marker_age_within "$pidf" $(( ${KEEPALIVE_EMIT_TIMEOUT_S:-110} + 10 )); then
        return 0
    fi
    local fleet_esc subj payload
    fleet_esc="$(json_escape "$FLEET_NAME")"
    subj="$(json_escape "bot:$FLEET_NAME/$BOT_NAME")"
    if [ "$verdict" = "DOWN" ]; then
        payload='{"subject_kind":"bot_instance","subject":"'"$subj"'","metric":"bot.session_up","value":false}'
    else
        # Session-up-ness is DERIVABLE from heartbeat presence (r2 volume
        # fold: a per-tick session_up=true row doubled the lane for a fact
        # the heartbeat already carries; only the dead path keeps the
        # explicit false). marker_age_s clamps at 0: an RTC-skewed future
        # mtime (this estate boots with a stale clock) otherwise records a
        # huge negative age readers would mis-sort — age has floor
        # semantics, not signed-delta semantics.
        local agefrag="" m_epoch age
        # `|| true` INSIDE the substitution (the #1460 rule): stat_mtime returns
        # 1 for a bot that has made no tool call yet, and on bash 3.2 that fires
        # the inherited ERR trap even under an `if` — a phantom script_error per
        # tick, now a fleet event on the plane (B2 made it visible).
        m_epoch=$(stat_mtime "$BOT_DIR/data/.last-tool-call" 2>/dev/null || true)
        if [ -n "$m_epoch" ]; then
            age=$(( $(date +%s) - m_epoch ))
            if [ "$age" -lt 0 ]; then age=0; fi
            agefrag=',"marker_age_s":'"$age"
        fi
        payload='{"subject_kind":"bot_instance","subject":"'"$subj"'","metric":"bot.heartbeat","value":{"state":"'"$verdict"'"'"$agefrag"'}}'
    fi
    # The background emit is WALL-CLOCK BOUNDED (retro round): under a
    # permanently wedged rung (the estate's documented D-state SD stall)
    # an unbounded emit made "bounded pileup" a RATE, not a ceiling —
    # ~720 stuck processes/day. The outer subshell reaps its emit at
    # KEEPALIVE_EMIT_TIMEOUT_S (portable — macOS has no timeout(1)), so
    # the in-flight claim self-expires and concurrency ceilings at ~1.
    # The staleness window derives from the SAME knob (+10s), keeping the
    # guard and the reaper coupled by construction rather than by twin
    # constants.
    local _eto="${KEEPALIVE_EMIT_TIMEOUT_S:-110}"
    (
        printf '%s' '{"events":[{"event_type":"metric_sample","emitter":"keepalive","fleet":"'"$fleet_esc"'","payload":'"$payload"'}]}' \
            | plane_emit_events keepalive >>"$LOG" 2>&1 &
        _w=$!
        _i=0
        while kill -0 "$_w" 2>/dev/null && [ "$_i" -lt "$_eto" ]; do
            sleep 1
            _i=$((_i + 1))
        done
        # kill the TREE, not the pipeline leader: $_w is the backgrounded
        # FUNCTION subshell, whose real work is grandchildren (plane-emit.sh
        # -> the cold CLI). A bare kill -9 "$_w" reaped the leader and
        # ORPHANED the wedged CLI alive — the whole point defeated (found by
        # observing five survivors after the pin "passed"; the recursive
        # form is portable where macOS bash 3.2 has no pkill -g / setsid).
        _kill_tree() {
            local _p="$1" _c
            # `|| true` INSIDE the substitution: pgrep exits 1 at every leaf of
            # every reap (a process with no children is the terminating case,
            # not a failure), and on bash 3.2 that rc fires the inherited ERR
            # trap from a for-word substitution even though set -e does NOT
            # exit there — so every tick of every armed bot logged a phantom
            # `script_error` ("non-zero exit at line 155", the funcdef line
            # bash 3.2 reports for in-function failures) while nothing failed.
            # 87/day fleet-wide, measured 2026-09-02; the alphabetical skew
            # (damodaran 77) was launchd killing later bots' reapers at sweep
            # teardown before they could log — suppression, not health.
            for _c in $(pgrep -P "$_p" 2>/dev/null || true); do _kill_tree "$_c"; done
            kill -9 "$_p" 2>/dev/null || true
        }
        _kill_tree "$_w"
    ) >/dev/null 2>&1 &
    printf '%d' $! > "$pidf" 2>/dev/null || true
}

# send_reload_command <slash-command>
# Verbatim send via pane_send_verified — NOT bot_tmux_send, which sanitizes (see
# that helper for why a slash command must reach the pane untouched). Caller
# guarantees the pane is IDLE (see the IDLE branch).
send_reload_command() {
    pane_send_verified "$TMUX_SOCKET" "$TMUX_SESSION" "$1"
}

# restart_bot_service <reason>
# The ONE restart ladder — reused by the dead-session watchdog below AND the
# bridge-heal path (Fork F1=b: consolidate, do not fork a second ladder). Picks the
# platform control plane, logs + emits a RESTART event tagged with <reason>, and
# falls back to start-bot.sh. Does NOT exit — callers own their control flow.
# NOTE: every branch re-runs start-bot.sh (directly, or as the systemd/launchd
# ExecStart), which re-touches data/.spawn. The bridge-heal path leans on that —
# bridge_down_state graces from .spawn, so the touch is what SPACES heal retries.
# Keep it true for any new restart branch, or retries collapse to once-per-tick.
restart_bot_service() {
    local reason="$1" desc
    if [ "$_OS" = "Linux" ] && [ -n "${BOT_SERVICE:-}" ] && [ -f "$HOME/.config/systemd/user/$BOT_SERVICE.service" ]; then
        desc="systemctl --user restart $BOT_SERVICE"
        echo "$(ts_iso) RESTART — $reason, $desc" >> "$LOG"
        emit_keepalive_event "RESTART" "$reason, $desc"
        systemctl --user restart "$BOT_SERVICE.service" >>"$LOG" 2>&1
    elif [ "$_OS" = "Linux" ] && [ -f "$HOME/.config/systemd/user/$BOT_NAME.service" ]; then
        # Pre-rename unit still installed (fleet not regenerated yet).
        desc="systemctl --user restart $BOT_NAME (pre-rename)"
        echo "$(ts_iso) RESTART — $reason, $desc" >> "$LOG"
        emit_keepalive_event "RESTART" "$reason, $desc"
        systemctl --user restart "$BOT_NAME.service" >>"$LOG" 2>&1
    elif [ "$_OS" = "Darwin" ] && [ -n "${BOT_SERVICE:-}" ] && [ -f "$HOME/Library/LaunchAgents/$BOT_SERVICE.plist" ]; then
        desc="launchctl kickstart $BOT_SERVICE"
        echo "$(ts_iso) RESTART — $reason, $desc" >> "$LOG"
        emit_keepalive_event "RESTART" "$reason, $desc"
        launchctl kickstart -k "gui/$(id -u)/$BOT_SERVICE" >>"$LOG" 2>&1
    else
        echo "$(ts_iso) RESTART — $reason, falling back to start-bot.sh $BOT_DIR" >> "$LOG"
        emit_keepalive_event "RESTART" "$reason, falling back to start-bot.sh"
        "$LIB_DIR/start-bot.sh" "$BOT_DIR" >>"$LOG" 2>&1
    fi
}

# _bridge_heal
# Tier-2 auto-heal for a dark Telegram inbound bridge (Fork F6b). GATED OFF unless
# OBSERVABILITY_BRIDGE_HEAL=1 — enabling the bounce fleet-wide waits on the
# production bounce-to-recovery telemetry that clears the F6b gate. The bun
# server.ts poller is an MCP stdio CHILD of claude, so nothing but a claude restart
# respawns it: the heal action is a full bot bounce (there is no claude-mcp-restart,
# and a standalone bun would 409 on Telegram's single-consumer slot). Called ONLY
# from the IDLE branch, so the BUSY-gate is implicit — a working bot is never
# bounced for an inbound-only outage it may not need this minute.
_bridge_heal() {
    [ "${OBSERVABILITY_BRIDGE_HEAL:-0}" = "1" ] || return 0

    local grace down
    grace="${OBSERVABILITY_BRIDGE_DOWN_GRACE:-300}"
    # bridge_down_state graces from data/.spawn, so a freshly (re)started bot —
    # including one we just bounced — is not re-bounced while its poller spins up.
    # That grace is what SPACES retries (the Fork F4 backoff intent); the persisted
    # attempt counter is what CAPS them. Reuses the SAME grace fleet-pulse alerts on,
    # so detection and heal never disagree about whether a bot is down.
    down="$(bridge_down_state "$BOT_DIR" "$grace" 2>/dev/null || true)"
    case "$down" in
        no_bridge)
            # Actionably dark past grace. Bounce under a per-bot lock so two
            # overlapping ticks cannot double-fire (M3) — belt-and-suspenders atop
            # the persisted counter + spawn grace.
            with_lock "$BOT_DIR/data/.bridge-heal.lock" _bridge_heal_bounce
            ;;
        no_token)
            : # A bounce cannot conjure a missing token — never heal (Fork F2/5e);
              # bring-up verify + fleet-pulse already escalate the misconfig.
            ;;
        *)
            # up / within-grace / no_handle / unknown — nothing to bounce. If a heal
            # ladder was in flight and the poller is genuinely back, reset it so a
            # future outage starts clean (unconditional marker clear on up, m3).
            if [ -f "$BOT_DIR/data/.bridge-heal" ] && [ "$(bridge_state "$BOT_DIR" 2>/dev/null || true)" = "up" ]; then
                rm -f "$BOT_DIR/data/.bridge-heal" "$BOT_DIR/data/.bridge-heal-escalated" "$BOT_DIR/data/.bridge-down" 2>/dev/null || true
                echo "$(ts_iso) BRIDGE_HEAL — poller recovered, ladder reset" >> "$LOG"
                emit_keepalive_event "BRIDGE_HEAL" "poller recovered, ladder reset"
            fi
            ;;
    esac
}

# _bridge_heal_bounce
# The capped bounce, run under the per-bot heal lock. keepalive is stateless across
# ticks, so the attempt budget is persisted on disk (data/.bridge-heal) — a plain
# in-memory counter would reset every tick and bounce forever.
_bridge_heal_bounce() {
    local statef="$BOT_DIR/data/.bridge-heal" max attempt
    max="${BRIDGE_HEAL_MAX_ATTEMPTS:-3}"
    attempt=0
    [ -f "$statef" ] && attempt="$(tr -cd '0-9' < "$statef" 2>/dev/null)"
    [ -n "$attempt" ] || attempt=0

    if [ "$attempt" -ge "$max" ]; then
        # Budget exhausted — escalate ONCE (Fork F3 escalate-only) and stop churning.
        # The bot still serves tmux dispatch; a human/manager takes it from here.
        if [ ! -f "$BOT_DIR/data/.bridge-heal-escalated" ]; then
            emit_failure_alert "$(dirname "$BOT_DIR")" "bridge_down" \
                "$BOT_NAME Telegram bridge still dark after $attempt heal bounces — escalate-only; manual attention needed" || true
            : > "$BOT_DIR/data/.bridge-heal-escalated" 2>/dev/null || true
            echo "$(ts_iso) BRIDGE_HEAL — budget exhausted ($attempt/$max), escalated" >> "$LOG"
            emit_keepalive_event "BRIDGE_HEAL" "budget exhausted ($attempt/$max), escalated"
        fi
        return 0
    fi

    # Record BEFORE the bounce so a crash mid-restart still advances the cap — never
    # an unbounded bounce loop.
    attempt=$((attempt + 1))
    printf '%s' "$attempt" > "$statef" 2>/dev/null || true
    echo "$(ts_iso) BRIDGE_HEAL — poller dark, bounce attempt $attempt/$max" >> "$LOG"
    emit_keepalive_event "BRIDGE_HEAL" "poller dark, bounce attempt $attempt/$max"
    restart_bot_service "bridge poller dark (heal $attempt/$max)"
}

# Cron runs with a minimal env. `systemctl --user` needs XDG_RUNTIME_DIR to
# reach the user bus, otherwise it fails silently with "Failed to connect to
# bus" while we still log a "RESTART" line — so the bot looks supervised
# but isn't. Set it here if missing. Requires `loginctl enable-linger $USER`
# (recommended by install-bot-systemd.sh) so /run/user/<uid> exists at boot.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# If session is dead, restart the bot's service.
# Pick the right control plane:
#   Linux  → systemctl --user restart $BOT_NAME.service (user units, no sudo)
#   macOS  → launchctl kickstart -k gui/<uid>/<label>   (LaunchAgent)
#   else   → fall back to start-bot.sh directly (cron+tmux pattern)
if ! check_tmux_session "$TMUX_SESSION" "$TMUX_SOCKET"; then
    # Reduce (not eliminate) race with start-bot.sh
    sleep 1
    if check_tmux_session "$TMUX_SESSION" "$TMUX_SOCKET"; then
        echo "$(ts_iso) SKIP — session reappeared (start-bot.sh likely won the race)" >> "$LOG"
        emit_keepalive_event "SKIP" "session reappeared (start-bot.sh likely won the race)"
        exit 0
    fi
    # A boot in flight has no session YET — restarting it kills the very start
    # that would have created one, and the replacement boot loses the same race.
    # The sleep 1 above is no defence: it is a fixed second against a window
    # measured at 35-178s under load. Evidence (#1002): rajan restarted 3x at
    # 17:24:34 / 17:25:32 / 17:26:19 on 2026-08-04, each landing on a start-bot.sh
    # that was still installing plugins, each resetting that work. The loop broke
    # only when host load fell enough for a boot to finish inside 60s.
    #
    # service_is_starting is the unit's own state, so unlike a session-age test
    # (there is no session to age) or data/.spawn (touched AFTER session creation
    # at start-bot.sh:273, so stale through exactly this window) it cannot be
    # fooled by the absence it is guarding. It is bounded by KEEPALIVE_BOOT_GRACE_S
    # so a wedged start-bot eventually gets restarted rather than suppressing the
    # watchdog forever.
    if [ -n "${BOT_SERVICE:-}" ] && service_is_starting "$BOT_SERVICE"; then
        echo "$(ts_iso) SKIP — boot in flight (unit mid-start), not restarting" >> "$LOG"
        emit_keepalive_event "SKIP" "boot in flight (unit mid-start), not restarting"
        exit 0
    fi
    plane_presence_samples DOWN
    restart_bot_service "session dead"
    exit 0
fi

# ---------------------------------------------------------------------------
# Pane-state classification
# ---------------------------------------------------------------------------
# Detection strategy (ordered by reliability):
#
# Liveness errs toward BUSY — a false idle reloads a working bot, far worse than
# a skipped reload — so a bot is BUSY if EITHER signal fires:
#
#   BUSY (primary) — A recent data/.last-tool-call marker (bot-vitals.sh touches
#           it on every tool call). A fresh marker means the bot is active —
#           rendering-immune, so it survives Claude Code verb/spinner churn and
#           prefersReducedMotion, which pane parsing cannot. Window:
#           KEEPALIVE_ACTIVE_WINDOW_S (default 180s ≈ 3 keepalive cycles).
#           Same marker fleet-pulse trusts (lib-common.sh).
#
#   BUSY (fallback) — classify_pane matches the "esc to interrupt" active-turn
#           affordance, catching a long think or long single tool call that
#           emits no marker for minutes (stable across releases, unlike the
#           spinner/verb list).
#
#   IDLE  — No recent tool-call marker, AND classify_pane matches a prompt glyph
#           (>, ❯) or a known waiting-for-input marker (no active affordance).
#
#   UNKNOWN — Neither signal matched.  Consecutive UNKNOWNs are tracked in
#             a counter file; crossing a threshold logs a warning so fleet
#             dashboards can surface stuck bots.
#
# Operators can extend patterns without editing this script:
#   KEEPALIVE_BUSY_PATTERNS  — extra ERE appended to the spinner check
#   KEEPALIVE_IDLE_PATTERNS  — extra ERE appended to the idle check
# ---------------------------------------------------------------------------

# classify_pane <pane_text>
# Prints BUSY, IDLE, or UNKNOWN to stdout. Pattern definitions and operator
# extension (KEEPALIVE_*_PATTERNS) live in lib-common.sh — pane_is_busy /
# pane_is_idle are the single source of truth; this adds only the three-way
# verdict keepalive needs (IDLE drives reload activation, UNKNOWN drives the
# consecutive-counter warning).
classify_pane() {
    local text="$1"
    if pane_is_busy "$text"; then
        echo "BUSY"
    elif pane_is_idle "$text"; then
        echo "IDLE"
    else
        echo "UNKNOWN"
    fi
}

# Liveness = active recently OR active now (err toward BUSY; see header). Primary:
# a data/.last-tool-call marker within the recency window — rendering-immune, and
# a short-circuit so a busy bot skips the pane capture entirely. Fallback: the
# pane's "esc to interrupt" affordance, for a long think/long single call that
# left no recent marker. The pane is only captured on the fallback path.
if marker_age_within "$BOT_DIR/data/.last-tool-call" "${KEEPALIVE_ACTIVE_WINDOW_S:-$_ACTIVE_WINDOW_DEFAULT}"; then
    state=BUSY
else
    pane_content=$(bot_tmux "$TMUX_SOCKET" capture-pane -t "$TMUX_SESSION" -p 2>/dev/null) || true
    last_lines=$(echo "$pane_content" | tail -10)
    state=$(classify_pane "$last_lines")
fi
UNKNOWN_COUNTER="$BOT_DIR/.keepalive-unknown-count"
UNKNOWN_THRESHOLD="${KEEPALIVE_UNKNOWN_THRESHOLD:-3}"

case "$state" in
    BUSY)
        echo "$(ts_iso) BUSY — active processing" >> "$LOG"
        emit_keepalive_event "BUSY" "active processing"
        rm -f "$UNKNOWN_COUNTER"
        # Clear idle marker — bot is actively working
        rm -f "$BOT_DIR/data/.idle"
        ;;
    IDLE)
        echo "$(ts_iso) IDLE — at prompt" >> "$LOG"
        emit_keepalive_event "IDLE" "at prompt"
        rm -f "$UNKNOWN_COUNTER"
        # Touch idle marker — fleet-pulse reads this instead of parsing panes
        touch "$BOT_DIR/data/.idle"
        # F2(b) consolidated reload activation: if reload-fleet.sh marked a live
        # plugin/skill update pending, perform it now that the pane is IDLE, then
        # clear the marker. This is the one place keepalive presses Enter on an
        # idle pane — safe because it sends fixed slash commands, not ghost text.
        if [ -f "$BOT_DIR/data/.reload-pending" ]; then
            send_reload_command "/reload-plugins"
            send_reload_command "/reload-skills"
            rm -f "$BOT_DIR/data/.reload-pending"
            echo "$(ts_iso) RELOAD — sent /reload-plugins + /reload-skills (live update)" >> "$LOG"
            emit_keepalive_event "RELOAD" "sent /reload-plugins + /reload-skills"
        fi
        # Tier-2 Telegram-bridge self-heal — gated OFF by default (F6b). Runs here, on
        # a confirmed-IDLE pane, so a heal bounce never interrupts in-flight work.
        _bridge_heal
        ;;
    *)
        # Track consecutive UNKNOWN runs
        prev=0
        [ -f "$UNKNOWN_COUNTER" ] && prev=$(cat "$UNKNOWN_COUNTER" 2>/dev/null) || true
        count=$((prev + 1))
        _tmp_counter="$(safe_mktemp)"
        printf '%d' "$count" > "$_tmp_counter" && mv "$_tmp_counter" "$UNKNOWN_COUNTER"
        if [ "$count" -ge "$UNKNOWN_THRESHOLD" ]; then
            echo "$(ts_iso) UNKNOWN — unrecognized pane state ($count consecutive, threshold $UNKNOWN_THRESHOLD) — investigate" >> "$LOG"
            emit_keepalive_event "UNKNOWN" "unrecognized pane state ($count consecutive, threshold $UNKNOWN_THRESHOLD)"
        else
            echo "$(ts_iso) UNKNOWN — pane state did not match known patterns ($count consecutive)" >> "$LOG"
            emit_keepalive_event "UNKNOWN" "pane state did not match known patterns ($count consecutive)"
        fi
        ;;
esac
plane_presence_samples "$state"
