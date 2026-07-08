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
#   fleet-memory-check.sh --fleet my-fleet
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
install_error_trap ""

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

LOG="$CLAUDLOBBY_ROOT/lib/fleet-memory-check.log"
setup_log_dir "$LOG"
TS=$(ts_iso)

# --- Available RAM (MB) -------------------------------------------------------
# /proc/meminfo is Linux-only; fall back to `vm_stat` on macOS.

avail_ram_mb() {
    if [ -f /proc/meminfo ]; then
        # MemAvailable is the kernel's own "this much is usable" figure.
        # Fall back to MemFree if not present (very old kernels). Decide in
        # END — MemFree precedes MemAvailable in /proc/meminfo, so per-line
        # printing would emit BOTH numbers concatenated.
        awk '/^MemAvailable:/ { avail=$2 } /^MemFree:/ { free=$2 }
             END { v = (avail ? avail : free); printf "%d", v/1024 }' \
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

# Per-bot breakdown via tmux session names (best-effort). Fleet-scoped when a
# fleet is set; the host job runs fleet-less and reports every fleet's bots.
per_bot_summary() {
    local bot_roots=() _d
    if [ -n "$FLEET_NAME" ]; then
        bot_roots=("$(resolve_bots_dir "$FLEET_NAME")")
    else
        # while-read, not mapfile: macOS ships bash 3.2.
        while IFS= read -r _d; do bot_roots+=("$_d"); done < <(host_bots_dirs)
    fi
    [ "${#bot_roots[@]}" -gt 0 ] || return 0
    local bot_root
    for bot_root in "${bot_roots[@]}"; do
    [ -d "$bot_root" ] || continue

    echo "  Per-bot RSS estimates (by session PPID chain, approximate):"
    for bot_dir in "$bot_root"/*/; do
        [ -d "$bot_dir" ] || continue
        local bot_name
        bot_name=$(basename "$bot_dir")
        # Each bot runs its own tmux server — resolve its socket from its dir
        # (tolerant: a bad/un-regenerated bot is skipped, not fatal to the sweep).
        local bot_socket
        bot_socket=$(tmux_socket_for_bot "$bot_dir" 2>/dev/null || true)
        # Find the tmux pane PID for this session, then sum child tree RSS
        local pane_pid rss_kb=0
        pane_pid=$(bot_tmux "$bot_socket" list-panes -t "$bot_name" -F '#{pane_pid}' 2>/dev/null | head -1) || true
        if [ -n "$pane_pid" ]; then
            # Walk the process tree rooted at pane_pid
            rss_kb=$(ps --ppid "$pane_pid" -p "$pane_pid" -o rss= 2>/dev/null \
                    | awk '{s+=$1} END{print s+0}') || true
        fi
        local rss_mb
        rss_mb=$(( (rss_kb + 512) / 1024 ))
        printf '    %-20s %4d MB' "$bot_name" "$rss_mb"
        if check_tmux_session "$bot_name" "$bot_socket" 2>/dev/null; then
            printf ' [running]'
        else
            printf ' [stopped]'
        fi
        echo
    done
    done
}

FLEET_RSS_MB=$(fleet_rss_mb)

# Alert when available RAM drops below the reserve floor.
# Threshold=80 means "alert when less than 20% of total RAM remains available."
# This avoids the >100% nonsense that fleet_rss/avail produces when RSS > avail.
RESERVE_PCT=$(( 100 - THRESHOLD ))
RESERVE_MB=$(( TOTAL_MB * RESERVE_PCT / 100 ))

if [ "$TOTAL_MB" -gt 0 ]; then
    AVAIL_PCT=$(( AVAIL_MB * 100 / TOTAL_MB ))
else
    AVAIL_PCT=0
fi

# --- Report -------------------------------------------------------------------

SUMMARY="fleet RSS ${FLEET_RSS_MB} MB, avail ${AVAIL_MB} MB (${AVAIL_PCT}% of ${TOTAL_MB} MB), reserve floor ${RESERVE_MB} MB (${RESERVE_PCT}%)"

echo "$TS $SUMMARY" | tee -a "$LOG"
per_bot_summary | tee -a "$LOG" || true

# --- Alert --------------------------------------------------------------------

if [ "$AVAIL_MB" -lt "$RESERVE_MB" ]; then
    ALERT_MSG="available RAM ${AVAIL_MB} MB (${AVAIL_PCT}% of total) on $(hostname) is below reserve floor ${RESERVE_MB} MB (${RESERVE_PCT}%). Fleet RSS ${FLEET_RSS_MB} MB. Consider stopping idle bots."
    echo "$TS ALERT — $ALERT_MSG" | tee -a "$LOG"
    # Shared signal path (fleet event + manager tmux nudge + Telegram):
    # delivery works fleet-less (host job) via the cross-fleet fallback.
    emit_failure_alert "$(resolve_bots_dir "$FLEET_NAME")" "memory_high" "$ALERT_MSG"
else
    echo "$TS OK — fleet RAM usage within threshold" | tee -a "$LOG"
fi

# Monitoring scripts must always exit 0 so cron/watchdog chains continue.
exit 0
