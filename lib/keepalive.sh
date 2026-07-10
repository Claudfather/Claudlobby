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

# Emit structured JSONL event for fleet-pulse / claudlobby uptime consumption.
emit_keepalive_event() {
    local ev_state="$1"
    local ev_detail="${2:-}"
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
    find "$events_dir" -name 'keepalive-*.jsonl' -type f -mtime +"$KEEPALIVE_REAP_DAYS" -delete 2>/dev/null || true
}

# send_reload_command <slash-command>
# Slash-safe send: a slash command must be the FIRST text in the input — the
# 'set +H; ' prefix dispatch.sh uses would break Claude Code's slash-command
# recognition. Two-step send (text, pause, Enter) plus a verify-retry on the
# Enter, mirroring start-bot.sh's STARTUP_PROMPT pattern. Caller guarantees the
# pane is IDLE (see the IDLE branch).
send_reload_command() {
    local cmd="$1"
    bot_tmux "$TMUX_SOCKET" send-keys -t "$TMUX_SESSION" "$cmd"
    sleep 0.3
    bot_tmux "$TMUX_SOCKET" send-keys -t "$TMUX_SESSION" Enter
    sleep 0.3
    # If the command text is still sitting unsubmitted at the prompt, the TUI
    # swallowed the Enter during a render — resend it once. Scope the match to the
    # bottom of the pane (the input line), not the whole pane: after a clean submit
    # the command scrolls up into the transcript and stays visible there, so a
    # full-pane match would re-fire Enter on every successful submit and inject an
    # empty message at the now-idle prompt.
    if bot_tmux "$TMUX_SOCKET" capture-pane -t "$TMUX_SESSION" -p 2>/dev/null | tail -3 | grep -qF "$cmd"; then
        bot_tmux "$TMUX_SOCKET" send-keys -t "$TMUX_SESSION" Enter 2>/dev/null || true
    fi
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
