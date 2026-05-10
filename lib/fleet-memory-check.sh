#!/bin/bash
# fleet-memory-check.sh — Monitor per-bot RSS memory usage for the fleet.
#
# Sums RSS of all Claude + Node/MCP processes visible to this user,
# compares against available system RAM, and fires a Telegram alert via
# tg-post.sh when usage exceeds the threshold (default 80%).
#
# Usage:
#   fleet-memory-check.sh [--threshold N] [--fleet <name>]
#   fleet-memory-check.sh --threshold 85
#   fleet-memory-check.sh --fleet crog-eng-team
#
# Reads from environment:
#   CLAUDLOBBY_ROOT         — repo root (default: ~/claudlobby)
#   TELEGRAM_GROUP_CHAT_ID  — chat ID for Telegram alerts
#   TELEGRAM_STATE_DIR      — dir containing .env with TELEGRAM_BOT_TOKEN
#                             (default: ~/.claude/channels/telegram)
#
# Exits 0 always — monitoring scripts must never abort fleet operations.
# Output goes to $CLAUDLOBBY_ROOT/lib/fleet-memory-check.log and stdout.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

# --- Defaults -----------------------------------------------------------------

THRESHOLD=80
FLEET_NAME=""

while [ $# -gt 0 ]; do
    case "$1" in
        --threshold) THRESHOLD="$2"; shift 2 ;;
        --fleet)     FLEET_NAME="$2"; shift 2 ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            echo "fleet-memory-check: unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$HOME/claudlobby}"
LOG="$CLAUDLOBBY_ROOT/lib/fleet-memory-check.log"
setup_log_dir "$LOG"
TS=$(ts_iso)

# --- Available RAM (MB) -------------------------------------------------------
# /proc/meminfo is Linux-only; fall back to `vm_stat` on macOS.

avail_ram_mb() {
    if [ -f /proc/meminfo ]; then
        # MemAvailable is the kernel's own "this much is usable" figure.
        # Fall back to MemFree if not present (very old kernels).
        awk '/^MemAvailable:/ { printf "%d", $2/1024; found=1 }
             /^MemFree:/      { if (!found) printf "%d", $2/1024 }' \
            /proc/meminfo
    else
        # macOS: vm_stat reports pages; multiply by page size (usually 4096).
        local page_size
        page_size=$(pagesize 2>/dev/null || sysctl -n hw.pagesize 2>/dev/null || echo 4096)
        vm_stat | awk -v ps="$page_size" '
            /Pages free/               { free = $3+0 }
            /Pages inactive/           { inactive = $3+0 }
            /Pages speculative/        { spec = $3+0 }
            END { printf "%d", (free + inactive + spec) * ps / 1048576 }'
    fi
}

total_ram_mb() {
    if [ -f /proc/meminfo ]; then
        awk '/^MemTotal:/ { printf "%d", $2/1024 }' /proc/meminfo
    else
        sysctl -n hw.memsize 2>/dev/null | awk '{ printf "%d", $1/1048576 }'
    fi
}

AVAIL_MB=$(avail_ram_mb)
TOTAL_MB=$(total_ram_mb)

if [ -z "$AVAIL_MB" ] || [ "$AVAIL_MB" -eq 0 ]; then
    echo "$TS ERROR — could not determine available RAM" | tee -a "$LOG"
    exit 0
fi

# --- Fleet RSS (MB) -----------------------------------------------------------
# Sum RSS of Claude Code (node) + MCP server (node) processes owned by this
# user.  We match on patterns that capture:
#   - claude itself (node process running @anthropic-ai/claude-code)
#   - MCP servers (node processes forked by claude)
# ps aux columns: USER PID %CPU %MEM VSZ RSS TTY STAT START TIME COMMAND

fleet_rss_mb() {
    ps aux 2>/dev/null \
      | awk -v user="$(id -un)" '
          $1 == user && ($0 ~ /claude|node.*mcp|python.*mcp|uvx/) {
              rss += $6
          }
          END { printf "%d", rss/1024 }'
}

# Per-bot breakdown via tmux session names (best-effort)
per_bot_summary() {
    local bot_root
    if [ -n "$FLEET_NAME" ]; then
        bot_root="$CLAUDLOBBY_ROOT/local/$FLEET_NAME/runtime/bots"
    else
        bot_root="$CLAUDLOBBY_ROOT/runtime/bots"
    fi

    if [ ! -d "$bot_root" ]; then
        return
    fi

    echo "  Per-bot RSS estimates (by session PPID chain, approximate):"
    for bot_dir in "$bot_root"/*/; do
        [ -d "$bot_dir" ] || continue
        local bot_name
        bot_name=$(basename "$bot_dir")
        # Find the tmux pane PID for this session, then sum child tree RSS
        local pane_pid rss_kb=0
        pane_pid=$("$_TMUX_BIN" list-panes -t "$bot_name" -F '#{pane_pid}' 2>/dev/null | head -1) || true
        if [ -n "$pane_pid" ]; then
            # Walk the process tree rooted at pane_pid
            rss_kb=$(ps --ppid "$pane_pid" -p "$pane_pid" -o rss= 2>/dev/null \
                    | awk '{s+=$1} END{print s+0}') || true
        fi
        local rss_mb
        rss_mb=$(( (rss_kb + 512) / 1024 ))
        printf '    %-20s %4d MB' "$bot_name" "$rss_mb"
        if check_tmux_session "$bot_name" 2>/dev/null; then
            printf ' [running]'
        else
            printf ' [stopped]'
        fi
        echo
    done
}

FLEET_RSS_MB=$(fleet_rss_mb)
USED_MB=$(( TOTAL_MB - AVAIL_MB ))

# Percentage of available RAM consumed by fleet-related processes.
# Available RAM (MemAvailable) is the kernel's estimate of usable memory
# without swapping — the correct denominator for swap-pressure detection.
if [ "$AVAIL_MB" -gt 0 ]; then
    FLEET_PCT=$(( FLEET_RSS_MB * 100 / AVAIL_MB ))
else
    FLEET_PCT=0
fi

# --- Report -------------------------------------------------------------------

SUMMARY="fleet RSS ${FLEET_RSS_MB} MB / avail ${AVAIL_MB} MB (${FLEET_PCT}%), total ${TOTAL_MB} MB, threshold ${THRESHOLD}%"

echo "$TS $SUMMARY" | tee -a "$LOG"
per_bot_summary | tee -a "$LOG" || true

# --- Alert --------------------------------------------------------------------

if [ "$FLEET_PCT" -ge "$THRESHOLD" ]; then
    ALERT_MSG="fleet-memory-check: RAM at ${FLEET_PCT}% (${FLEET_RSS_MB} MB fleet RSS / ${AVAIL_MB} MB available). Threshold ${THRESHOLD}%. Consider stopping idle bots."
    echo "$TS ALERT — $ALERT_MSG" | tee -a "$LOG"

    TG_POST="$CLAUDLOBBY_ROOT/lib/tg-post.sh"
    if [ -x "$TG_POST" ] && [ -n "${TELEGRAM_GROUP_CHAT_ID:-}" ]; then
        "$TG_POST" "$ALERT_MSG" >> "$LOG" 2>&1 || \
            echo "$TS WARN — tg-post failed for memory alert" >> "$LOG"
    else
        echo "$TS NOTE — TELEGRAM_GROUP_CHAT_ID not set; skipping Telegram alert" >> "$LOG"
    fi
else
    echo "$TS OK — fleet RAM usage within threshold" | tee -a "$LOG"
fi

# Monitoring scripts must always exit 0 so cron/watchdog chains continue.
exit 0
