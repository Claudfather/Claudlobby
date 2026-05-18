#!/bin/bash
# Bot keepalive — restart dead sessions, log idle state.
# Usage: keepalive.sh /path/to/bot/dir
#
# IMPORTANT: this script intentionally does NOT press Enter on idle panes.
# Pressing Enter at the prompt submits any "ghost" text — including Claude
# Code's greyed-out auto-completion suggestions — which causes the bot to
# act on input the user never typed. If you want a nudge mechanism, build
# one in your local overlay with eyes open about the input-injection risk.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

BOT_DIR="${1:?Usage: keepalive.sh /path/to/bot/dir}"
load_bot_conf "$BOT_DIR"
TMUX_SESSION="$(tmux_session_name "$BOT_DIR")"

LOG="$BOT_DIR/keepalive.log"

# Emit structured JSONL event for fleet-pulse / claudlobby uptime consumption.
emit_keepalive_event() {
    local ev_state="$1"
    local events_dir="$BOT_DIR/data/events"
    mkdir -p "$events_dir"
    local events_file="$events_dir/keepalive-$(date +%Y-%m-%d).jsonl"
    local ts
    ts=$(ts_iso)
    printf '{"ts":"%s","bot":"%s","type":"keepalive","source":"keepalive","data":{"state":"%s"}}\n' \
        "$ts" "$BOT_NAME" "$ev_state" >> "$events_file"
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
        exit 0
    fi
    emit_keepalive_event "RESTART"
    if [ "$_OS" = "Linux" ] && [ -f "$HOME/.config/systemd/user/$BOT_NAME.service" ]; then
        echo "$(ts_iso) RESTART — session dead, systemctl --user restart $BOT_NAME" >> "$LOG"
        systemctl --user restart "$BOT_NAME.service" >>"$LOG" 2>&1
    elif [ "$_OS" = "Darwin" ] && [ -n "$BOT_SERVICE" ] && [ -f "$HOME/Library/LaunchAgents/$BOT_SERVICE.plist" ]; then
        echo "$(ts_iso) RESTART — session dead, launchctl kickstart $BOT_SERVICE" >> "$LOG"
        launchctl kickstart -k "gui/$(id -u)/$BOT_SERVICE" >>"$LOG" 2>&1
    else
        echo "$(ts_iso) RESTART — session dead, falling back to start-bot.sh $BOT_DIR" >> "$LOG"
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
    # Note: \b is a GNU grep extension (word boundary). It works on Linux
    # (GNU grep) and macOS 14+ (which ships GNU-compatible grep). On older
    # macOS, replace with [^a-z] or drop the boundary check.
    local _idle_pattern='(^\s*[>❯]\s*$|Remote Control active|Enter\/Esc to close|Yes\/No|Allow|Deny|y\/n\b)'
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
        emit_keepalive_event "BUSY"
        rm -f "$UNKNOWN_COUNTER"
        ;;
    IDLE)
        echo "$(ts_iso) IDLE — at prompt" >> "$LOG"
        emit_keepalive_event "IDLE"
        rm -f "$UNKNOWN_COUNTER"
        ;;
    *)
        # Track consecutive UNKNOWN runs
        prev=0
        [ -f "$UNKNOWN_COUNTER" ] && prev=$(cat "$UNKNOWN_COUNTER" 2>/dev/null) || true
        count=$((prev + 1))
        printf '%d' "$count" > "$UNKNOWN_COUNTER"
        emit_keepalive_event "UNKNOWN"
        if [ "$count" -ge "$UNKNOWN_THRESHOLD" ]; then
            echo "$(ts_iso) UNKNOWN — unrecognized pane state ($count consecutive, threshold $UNKNOWN_THRESHOLD) — investigate" >> "$LOG"
        else
            echo "$(ts_iso) UNKNOWN — pane state did not match known patterns ($count consecutive)" >> "$LOG"
        fi
        ;;
esac
