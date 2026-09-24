#!/usr/bin/env bash
# update-claude-code.sh — Download the latest Claude Code binary daily.
#
# Download-only. Intended to run daily via systemd timer. Idempotent: if already
# on latest, does nothing. It does NOT restart any bot — the binary cannot
# hot-reload, so it is applied on the next restart instead: any natural restart,
# or the weekly worker-only bounce (weekly-worker-restart.sh). Retiring the old
# daily fleet-bounce here is what removes the daily-reset context loss.
#
# SUCCESS IS A MEASUREMENT OF THE STAGED BINARY, NOT OF npm: the binary the
# fleet launches must RUN (exit 0) and print a parseable version. npm can exit 0
# while omitting the platform-native optional dependency, leaving a stub that
# exits 1 — every bot that starts or restarts afterwards fails to launch, while
# the bots already running hide it. So an UNRUNNABLE binary is a different
# signal from a STALE one (stale is bounded to <=1 week by the weekly worker
# restart): it is raised at both ends of a run — binary_unrunnable before the
# install, binary_update_failed after it — and binary_repaired says when a
# reinstall fixed it.
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

# Stamped at write time: how long an install ran is evidence when one goes wrong.
log() { printf '%s %s\n' "$(ts_iso)" "$*" >> "$LOG"; }

# Loud failure: raise it through the shared emit_failure_alert primitive (fleet
# event + manager tmux nudge + Telegram escalation) — the same alert path
# Mechanism 1's reload-fleet.sh uses, so neither mechanism forks it — then exit
# non-zero so the timer run is marked failed.
update_failed() {
    local rc="$1" msg="$2"
    log "UPDATE FAILED — $msg"
    emit_failure_alert "$BOTS_DIR" "binary_update_failed" "$msg"
    exit "$rc"
}

# --- Resolve the binary the FLEET launches (not this script's own PATH) ------
# The update must target the SAME claude that start-bot.sh runs. This script's
# PATH (above) prepends the user prefixes so npm/node resolve under a bare timer
# env — but start-bot.sh exports its launch PATH with the SYSTEM dirs FIRST, so
# the fleet runs e.g. /usr/bin/claude even when ~/.npm-global holds a newer copy.
# Detecting via this script's PATH updated that shadow user copy and left the
# fleet's binary stale (#635). Mirror start-bot's ordering for detection +
# version + the sudo choice; the PATH above still finds npm/node to RUN the
# install. (A hand copy of start-bot's launch order — keep the two in sync;
# one shared resolver is #1772.)
# CLAUDE_BIN is the same override start-bot.sh launches with — when the fleet
# pins its binary explicitly, the updater targets THAT one (SSOT). Absent it,
# mirror start-bot's launch ordering. CLAUDE_UPDATE_FLEET_PATH lets a test / an
# unusual host substitute the resolution order.
_FLEET_PATH="${CLAUDE_UPDATE_FLEET_PATH:-/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$HOME/.bun/bin:$HOME/.npm-global/bin${_HOMEBREW:+:$_HOMEBREW/bin}}"
fleet_claude() {
    if [ -n "${CLAUDE_BIN:-}" ]; then printf '%s' "$CLAUDE_BIN"; return; fi
    PATH="$_FLEET_PATH" command -v claude 2>/dev/null || true
}

# --- The one predicate: does the fleet's binary work? ------------------------
# It RAN (exit 0) and the first line of its stdout carries a parseable X.Y.Z.
# Sets CLAUDE_VERSION and returns 0; otherwise leaves CLAUDE_VERSION empty, sets
# CLAUDE_VERSION_WHY and returns 1. There is deliberately NO sentinel value: a
# could-not-measure rendered as a string gets logged, compared and diffed like a
# version. Callers branch on the return code, never on the text. This is the
# rule claudlobby/source_state.py decides for read doors (unreachable is not a
# value); that module answers whether a PATH can be opened, and a stub that
# cannot run opens fine. Local until a second consumer needs it (#1772).
measure_claude_version() {
    CLAUDE_VERSION=""
    CLAUDE_VERSION_WHY=""
    local p out first said rc=0 re='[0-9]+\.[0-9]+\.[0-9]+'
    p="$(fleet_claude)"
    if [ -z "$p" ]; then
        CLAUDE_VERSION_WHY="no claude binary resolved"
        return 1
    fi
    # Settled inside the substitution (|| exit) so install_error_trap never sees
    # the failing binary. Unneeded on bash 5.2 (measured: callers are all `if`s,
    # whose ERR suppression reaches in); kept for bash 3.2, unmeasured.
    out="$("$p" --version 2>/dev/null || exit $?)" || rc=$?
    first="${out%%$'\n'*}"
    if [ "$rc" -eq 0 ] && [[ $first =~ $re ]]; then
        CLAUDE_VERSION="${BASH_REMATCH[0]}"
        return 0
    fi
    # Could not measure: say why in the binary's own words. stderr is where a
    # binary that cannot run explains itself; the read above discards it so a
    # warning can never be parsed as the version.
    said="$("$p" --version 2>&1 || true)"
    said="${said%%$'\n'*}"
    if [ "$rc" -ne 0 ]; then
        CLAUDE_VERSION_WHY="$p --version exited $rc"
    else
        CLAUDE_VERSION_WHY="$p --version printed no parseable version"
    fi
    CLAUDE_VERSION_WHY="$CLAUDE_VERSION_WHY${said:+: ${said:0:200}}"
    return 1
}

# --- Measure the fleet's binary BEFORE the install ----------------------------
_claude_path="$(fleet_claude)"
old_version=""
old_why=""
_current="not installed"
if [ -n "$_claude_path" ]; then
    if measure_claude_version; then
        old_version="$CLAUDE_VERSION"
        _current="$old_version"
    else
        old_why="$CLAUDE_VERSION_WHY"
        _current="CANNOT RUN ($old_why)"
    fi
fi
log "UPDATE starting (current: $_current, target: ${_claude_path:-none}, fleet: ${FLEET:-none})"

# A binary that cannot run is an outage already in progress: every bot that
# starts or restarts on this host launches it. Say so BEFORE the install, which
# can run for many minutes and may not repair it.
if [ -n "$old_why" ]; then
    log "UPDATE ALERT — the fleet's binary cannot run before the update: $old_why"
    emit_failure_alert "$BOTS_DIR" "binary_unrunnable" \
        "the fleet's claude binary cannot run ($old_why) — a bot that starts or restarts on this host will not launch until it is repaired; reinstalling now"
fi

# --- Install: elevate only when the fleet's binary is a root-owned system install
_npm=(npm install -g @anthropic-ai/claude-code@latest)
if [ -n "$_claude_path" ] && [[ "$_claude_path" == /usr/* ]]; then
    _npm=(sudo "${_npm[@]}")
fi
# The repair an unrunnable result needs: the install again. The stub's own advice
# (the package's install.cjs) exits 0 and repairs nothing when the platform
# package is absent.
_repair="to repair, re-run: ${_npm[*]} (not the package's install.cjs, which cannot restore a missing platform package), then check --version"
log "UPDATE running: ${_npm[*]}"
npm_rc=0
"${_npm[@]}" >> "$LOG" 2>&1 || npm_rc=$?
log "UPDATE install finished (npm exit $npm_rc)"

# --- Verify the STAGED binary: this, not npm's exit status, is the verdict ----
if measure_claude_version; then
    new_version="$CLAUDE_VERSION"
    [ "$npm_rc" -eq 0 ] || update_failed 1 "npm install returned $npm_rc — the fleet's binary runs $new_version"
elif [ "$npm_rc" -ne 0 ]; then
    update_failed 1 "npm install returned $npm_rc and the fleet's binary cannot run ($CLAUDE_VERSION_WHY); $_repair"
else
    update_failed 1 "npm install returned 0 but the staged binary cannot run ($CLAUDE_VERSION_WHY) — a bot that starts or restarts on this host will not launch until it is repaired; $_repair"
fi
log "UPDATE verified: the staged binary ran and reported $new_version"

if [ -n "$old_why" ]; then
    log "UPDATE repaired: the binary could not run before the update and now runs $new_version (staged; applied on next restart)"
    emit_fleet_notice "$BOTS_DIR" "binary_repaired" \
        "the fleet's claude binary runs again: $new_version (before this update it could not run: $old_why)"
elif [ "$old_version" = "$new_version" ]; then
    log "UPDATE no-op: already on $new_version"
else
    # Download-only: the new binary is staged in place. Bots pick it up on their
    # next restart — any natural restart, or the weekly worker-only bounce
    # (weekly-worker-restart.sh). No fleet bounce here: that daily forced
    # restart was the daily-reset context loss this role shift removes.
    log "UPDATE version changed: ${old_version:-not installed} → $new_version (staged; applied on next restart)"
fi
exit 0
