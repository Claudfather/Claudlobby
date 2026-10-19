#!/usr/bin/env bash
# bot-vitals.sh — Claude Code hook script for fleet observability.
#
# Reads the hook JSON payload from stdin and records each event on the plane
# through the fleet-event door (emit_fleet_event). Works as both PreToolUse and
# PostToolUse hook.
#
# Captures: tool_call. A Pre/PostToolUse payload carries no session lifecycle
# field, so no session event can be derived here.
# NOTE: context_warning and rate_limit are NOT available via the Claude Code
# hook payload (PreToolUse/PostToolUse). Managers must use live checks for those.
#
# Composed into every bot by the release's defaults (claudlobby/system.yaml) as
# "$CLAUDLOBBY_NATIVE_DIR/bot-vitals.sh" for PreToolUse and PostToolUse. A fleet
# manifest must not declare it again: a second entry, such as the retired
# "$CLAUDLOBBY_ROOT/lib/bot-vitals.sh", runs the hook twice and records every
# tool call twice (#2062).
#
# It needs BOT_DIR, an existing absolute dir (bot.conf exports it). Without one it
# does nothing (#874): a bot's working directory is its project checkout, so
# neither the marker nor a plane row may fall back to it.
#
# Each event: <type>\t<data-json>, handed to emit_fleet_event (source "vitals").

# Non-blocking hook: trap ALL errors and always exit 0.
# A vitals failure must never block tool execution.
trap 'exit 0' ERR

set -euo pipefail

# #874 (see header). Checked before lib-common is sourced: the no-op path is one fork.
if [[ "${BOT_DIR:-}" != /* || ! -d "$BOT_DIR" ]]; then
    cat >/dev/null 2>&1 || true   # drain the payload; the writer must not see a closed pipe
    echo "bot-vitals: BOT_DIR unset or not an absolute directory — recording nothing, touching no marker (#874)" >&2
    exit 0
fi

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLANE_EMIT_CLASS=hook   # a live Claude Code turn waits on its plane record: its socket deadline (#1693, claudlobby/_runtime_scripts/plane-emit.sh)
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

# --- Read hook payload from stdin (Claude Code sends JSON) ---
payload="$(cat)"

bot="${BOT_ID:-unknown}"

# --- Parse payload and emit event(s) ---
# Single python3 call: parse the payload and print one
# `<type>\t<data-json>` line per event; every line then goes through the ONE
# fleet-event door (cutover B2: emit_fleet_event lands it on the plane with
# provenance, alias-anchored, and the JSONL append retires with the family —
# this script printed straight into fleet-<day>.jsonl before, so its rows were
# invisible to `claudlobby event list` from the flip on). The payload reaches
# python on stdin, never inside its source, so payload text cannot inject code.
python3 -c "
import json, sys

try:
    p = json.loads(sys.stdin.read())
except (json.JSONDecodeError, ValueError):
    p = {}

hook_event = p.get('hook_event_name', '')
tool = p.get('tool_name', '')
session = p.get('session_id', '')

events = []

def evt(etype, data):
    events.append(etype + '\t' + json.dumps(data, separators=(',', ':')))

# Always emit a tool_call event for any hook invocation with a tool name
if tool:
    evt('tool_call', {'tool': tool, 'event': hook_event, 'session': session})

# MCP tool errors are not observable from this hook. A failing tool call —
# including an MCP server returning isError — fires the PostToolUseFailure
# hook event, not PostToolUse, and only that event carries an error field.
# This script is wired to Pre/PostToolUse, whose payload exposes no error or
# tool-failure field, so an mcp_error signal cannot be derived here.

# NOTE: context_warning and rate_limit are not available in the hook payload.
# The PreToolUse/PostToolUse schema does not include context_window_percent
# or rate_limited fields. Managers must use live checks for these signals.

# Print all events, one per line
for e in events:
    print(e)
" <<< "$payload" 2>/dev/null | while IFS=$'\t' read -r _etype _edata; do
    [ -n "$_etype" ] || continue
    emit_fleet_event "$_etype" vitals "$_edata" "$BOT_DIR" "$bot" || true
done

# --- Activity marker ---
# Touch a marker file on every tool-call hook invocation. fleet-pulse.sh reads
# its mtime to detect an "activity_stuck" bot — one whose pane is animating but
# has made no tool call for a long time (the case pane_stuck can't see). Cheaper
# and more portable than parsing the last tool_call timestamp out of the JSONL.
touch "$BOT_DIR/data/.last-tool-call" 2>/dev/null || true

# Non-blocking hook — always exit 0
exit 0
