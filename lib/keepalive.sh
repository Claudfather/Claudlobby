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
    "$_TMUX_BIN" send-keys -t "$TMUX_SESSION" "$cmd"
    sleep 0.3
    "$_TMUX_BIN" send-keys -t "$TMUX_SESSION" Enter
    sleep 0.3
    # If the command text is still sitting unsubmitted at the prompt, the TUI
    # swallowed the Enter during a render — resend it once. Scope the match to the
    # bottom of the pane (the input line), not the whole pane: after a clean submit
    # the command scrolls up into the transcript and stays visible there, so a
    # full-pane match would re-fire Enter on every successful submit and inject an
    # empty message at the now-idle prompt.
    if "$_TMUX_BIN" capture-pane -t "$TMUX_SESSION" -p 2>/dev/null | tail -3 | grep -qF "$cmd"; then
        "$_TMUX_BIN" send-keys -t "$TMUX_SESSION" Enter 2>/dev/null || true
    fi
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
if ! check_tmux_session "$TMUX_SESSION"; then
    # Reduce (not eliminate) race with start-bot.sh
    sleep 1
    if check_tmux_session "$TMUX_SESSION"; then
        echo "$(ts_iso) SKIP — session reappeared (start-bot.sh likely won the race)" >> "$LOG"
        emit_keepalive_event "SKIP" "session reappeared (start-bot.sh likely won the race)"
        exit 0
    fi
    if [ "$_OS" = "Linux" ] && [ -n "${BOT_SERVICE:-}" ] && [ -f "$HOME/.config/systemd/user/$BOT_SERVICE.service" ]; then
        echo "$(ts_iso) RESTART — session dead, systemctl --user restart $BOT_SERVICE" >> "$LOG"
        emit_keepalive_event "RESTART" "session dead, systemctl --user restart $BOT_SERVICE"
        systemctl --user restart "$BOT_SERVICE.service" >>"$LOG" 2>&1
    elif [ "$_OS" = "Linux" ] && [ -f "$HOME/.config/systemd/user/$BOT_NAME.service" ]; then
        # Pre-rename unit still installed (fleet not regenerated yet).
        echo "$(ts_iso) RESTART — session dead, systemctl --user restart $BOT_NAME (pre-rename)" >> "$LOG"
        emit_keepalive_event "RESTART" "session dead, systemctl --user restart $BOT_NAME (pre-rename)"
        systemctl --user restart "$BOT_NAME.service" >>"$LOG" 2>&1
    elif [ "$_OS" = "Darwin" ] && [ -n "${BOT_SERVICE:-}" ] && [ -f "$HOME/Library/LaunchAgents/$BOT_SERVICE.plist" ]; then
        echo "$(ts_iso) RESTART — session dead, launchctl kickstart $BOT_SERVICE" >> "$LOG"
        emit_keepalive_event "RESTART" "session dead, launchctl kickstart $BOT_SERVICE"
        launchctl kickstart -k "gui/$(id -u)/$BOT_SERVICE" >>"$LOG" 2>&1
    else
        echo "$(ts_iso) RESTART — session dead, falling back to start-bot.sh $BOT_DIR" >> "$LOG"
        emit_keepalive_event "RESTART" "session dead, falling back to start-bot.sh"
        "$LIB_DIR/start-bot.sh" "$BOT_DIR" >>"$LOG" 2>&1
    fi
    exit 0
fi

pane_content=$("$_TMUX_BIN" capture-pane -t "$TMUX_SESSION" -p 2>/dev/null) || true
last_lines=$(echo "$pane_content" | tail -10)

# ---------------------------------------------------------------------------
# Pane-state classification
# ---------------------------------------------------------------------------
# Detection strategy (ordered by reliability):
#
#   BUSY  — Spinner characters (braille: ⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏) present in the last
#           10 lines. These are the most version-stable signal — Claude Code
#           shows a braille spinner whenever it's processing, regardless of
#           which verb label it uses.  Fallback: a configurable verb pattern
#           catches labelled activity lines.
#
#   IDLE  — Last non-blank line ends with a prompt glyph (>, ❯) or contains
#           known waiting-for-input markers, AND no spinner is visible.
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
# Prints BUSY, IDLE, or UNKNOWN to stdout.  Sourced by tests — keep this as
# the single source of truth for pattern definitions.
classify_pane() {
    local text="$1"

    # --- BUSY: spinner characters first, then verb pattern ---
    local _busy_spinner='[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]'
    local _busy_verbs='(Running|Thinking|Reading|Writing|Editing|Searching|Generating|Pondering)'
    local _busy_pattern="$_busy_spinner|$_busy_verbs"
    if [ -n "${KEEPALIVE_BUSY_PATTERNS:-}" ]; then
        _busy_pattern="$_busy_pattern|$KEEPALIVE_BUSY_PATTERNS"
    fi

    # --- IDLE: prompt glyph or waiting-for-input marker ---
    # Uses _IDLE_PATTERN_BASE from lib-common.sh (single source of truth).
    local _idle_pattern="$_IDLE_PATTERN_BASE"
    if [ -n "${KEEPALIVE_IDLE_PATTERNS:-}" ]; then
        _idle_pattern="$_idle_pattern|$KEEPALIVE_IDLE_PATTERNS"
    fi

    if echo "$text" | grep -qE "$_busy_pattern"; then
        echo "BUSY"
    elif echo "$text" | grep -qE "$_idle_pattern"; then
        echo "IDLE"
    else
        echo "UNKNOWN"
    fi
}

state=$(classify_pane "$last_lines")
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
