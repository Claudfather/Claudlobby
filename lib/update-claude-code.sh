#!/usr/bin/env bash
# update-claude-code.sh — Download the latest Claude Code binary daily.
#
# Download-only. Intended to run daily via systemd timer. Idempotent: if already
# on latest, does nothing. It does NOT restart any bot — the binary cannot
# hot-reload, so it is applied on the next restart instead: any natural restart,
# or the weekly worker-only bounce (weekly-worker-restart.sh). Retiring the old
# daily fleet-bounce here is what removes the daily-reset context loss.
#
# A failed download emits a durable script_error event (queryable via the events
# CLI / `claudlobby report-back`). A stale binary is low-urgency — bounded to
# <=1 week by the weekly worker restart — so this is a heads-up, not an emergency.
#
# Usage: update-claude-code.sh [<fleet-name>]
#   The optional fleet name is recorded with the run; this script restarts no bot.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

# Timer environments carry a minimal PATH; own tool resolution here so npm /
# claude resolve identically under systemd, launchd, cron, or a shell.
# _HOMEBREW (lib-common) covers brew-installed node on macOS.
# PREPENDS (unlike own_tool_path, which appends): must run the NEWEST npm to do
# the install, while detecting the binary to UPDATE via _FLEET_PATH below (#635).
PATH="$HOME/.local/bin:$HOME/.npm-global/bin${_HOMEBREW:+:$_HOMEBREW/bin}:$PATH"
export PATH

FLEET="${1:-${CLAUDLOBBY_FLEET:-}}"
BOTS_DIR="$(resolve_bots_dir "$FLEET")"
LOG_DIR="${CLAUDLOBBY_ROOT}/state"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/claude-update.log"

ts=$(ts_iso)

# Loud failure: raise it through the shared emit_failure_alert primitive (fleet
# event + manager tmux nudge + Telegram escalation) — the same alert path
# Mechanism 1's reload-fleet.sh uses, so neither mechanism forks it — then exit
# non-zero so the timer run is marked failed. A stale binary is low-urgency
# (bounded to <=1 week by the weekly worker restart), so this is a heads-up.
update_failed() {
    local rc="$1" msg="$2"
    echo "$ts UPDATE FAILED — $msg" >> "$LOG"
    emit_failure_alert "$BOTS_DIR" "binary_update_failed" "$msg"
    exit "$rc"
}

# --- Resolve the binary the FLEET launches (not this script's own PATH) ------
# The update must target the SAME claude that start-bot.sh runs. This script's
# PATH (above) prepends the user prefixes so npm/node resolve under a bare timer
# env — but start-bot.sh:49 launches with the SYSTEM dirs FIRST, so the fleet
# runs e.g. /usr/bin/claude even when ~/.npm-global holds a newer copy.
# Detecting via this script's PATH updated that shadow user copy and left the
# fleet's binary stale (#635). Mirror start-bot's ordering for detection +
# version + the sudo choice; the PATH above still finds npm/node to RUN the
# install. (Ordering mirrors start-bot.sh:49 — keep the two in sync.)
# CLAUDE_BIN is the same override start-bot.sh:176 launches with — when the
# fleet pins its binary explicitly, the updater targets THAT one (SSOT). Absent
# it, mirror start-bot.sh:49's launch ordering. CLAUDE_UPDATE_FLEET_PATH lets a
# test / an unusual host substitute the resolution order.
_FLEET_PATH="${CLAUDE_UPDATE_FLEET_PATH:-/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$HOME/.bun/bin:$HOME/.npm-global/bin${_HOMEBREW:+:$_HOMEBREW/bin}}"
fleet_claude() {
    if [ -n "${CLAUDE_BIN:-}" ]; then printf '%s' "$CLAUDE_BIN"; return; fi
    PATH="$_FLEET_PATH" command -v claude 2>/dev/null || true
}
fleet_claude_version() {
    local p; p="$(fleet_claude)"
    [ -n "$p" ] || { echo "unknown"; return; }
    "$p" --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || echo "unknown"
}

# --- Capture current version (of the fleet's binary) ---
_claude_path="$(fleet_claude)"
old_version=""
if [ -n "$_claude_path" ]; then
    old_version="$(fleet_claude_version)"
fi

echo "$ts UPDATE starting (current: ${old_version:-not installed}, target: ${_claude_path:-none}, fleet: ${FLEET:-none})" >> "$LOG"

# --- Elevate only when the fleet's binary is a root-owned system install ------
_use_sudo=0
if [ -n "$_claude_path" ] && [[ "$_claude_path" == /usr/* ]]; then
    _use_sudo=1
fi

if [ "$_use_sudo" -eq 1 ]; then
    echo "$ts UPDATE running: sudo npm install -g @anthropic-ai/claude-code@latest" >> "$LOG"
    if sudo npm install -g @anthropic-ai/claude-code@latest >> "$LOG" 2>&1; then
        new_version="$(fleet_claude_version)"
        echo "$ts UPDATE success: $old_version → $new_version" >> "$LOG"
    else
        update_failed 1 "npm install (sudo) returned non-zero — fleet stays on ${old_version:-unknown}"
    fi
else
    echo "$ts UPDATE running: npm install -g @anthropic-ai/claude-code@latest" >> "$LOG"
    if npm install -g @anthropic-ai/claude-code@latest >> "$LOG" 2>&1; then
        new_version="$(fleet_claude_version)"
        echo "$ts UPDATE success: $old_version → $new_version" >> "$LOG"
    else
        update_failed 1 "npm install returned non-zero — fleet stays on ${old_version:-unknown}"
    fi
fi

# --- Check if version actually changed ---
if [ "$old_version" = "$new_version" ]; then
    echo "$ts UPDATE no-op: already on $new_version" >> "$LOG"
    exit 0
fi

# Download-only: the new binary is staged in place. Bots pick it up on their
# next restart — any natural restart, or the weekly worker-only bounce
# (weekly-worker-restart.sh). No fleet bounce here: that daily forced restart
# was the daily-reset context loss this role shift removes.
echo "$ts UPDATE version changed: $old_version → $new_version (staged; applied on next restart)" >> "$LOG"
exit 0
