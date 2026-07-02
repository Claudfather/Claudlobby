#!/usr/bin/env bash
# bot-vitals.sh — Claude Code hook script for fleet observability.
#
# Reads the hook JSON payload from stdin and writes a JSONL event to
# the bot's data/events/fleet-YYYY-MM-DD.jsonl. Works as both
# PreToolUse and PostToolUse hook.
#
# Captures: tool_call, session events.
# NOTE: context_warning and rate_limit are NOT available via the Claude Code
# hook payload (PreToolUse/PostToolUse). Managers must use live checks for those.
# Reaps event files older than 7 days on each invocation.
#
#
# Usage in fleet.yaml:
#   hooks:
#     PreToolUse:
#       - command: "$CLAUDLOBBY_ROOT/lib/bot-vitals.sh"
#     PostToolUse:
#       - command: "$CLAUDLOBBY_ROOT/lib/bot-vitals.sh"
#
# Event schema (one JSON object per line):
#   {"ts":"...","bot":"...","type":"...","source":"vitals","data":{...}}

# Non-blocking hook: trap ALL errors and always exit 0.
# A vitals failure must never block tool execution.
trap 'exit 0' ERR

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

# --- Read hook payload from stdin (Claude Code sends JSON) ---
payload="$(cat)"

# --- Resolve output directory ---
events_dir="${BOT_DIR:-${PWD}}/data/events"
mkdir -p "$events_dir"

ts=$(ts_iso)
bot="${BOT_ID:-unknown}"
today=$(date +%Y-%m-%d)
outfile="$events_dir/fleet-${today}.jsonl"

# --- Parse payload and emit event(s) ---
# Single python3 call: parse payload, classify event type, emit JSONL.
# Values passed via env to avoid shell injection.
BOT_ID_VAL="$bot" \
TS_VAL="$ts" \
python3 -c "
import json, os, sys

ts = os.environ['TS_VAL']
bot = os.environ['BOT_ID_VAL']

try:
    p = json.loads(sys.stdin.read())
except (json.JSONDecodeError, ValueError):
    p = {}

hook_event = p.get('hook_event_name', '')
tool = p.get('tool_name', '')
session = p.get('session_id', '')

events = []

def evt(etype, data):
    events.append(json.dumps(
        {'ts': ts, 'bot': bot, 'type': etype, 'source': 'vitals', 'data': data},
        separators=(',', ':'),
    ))

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

# Session events (start, stop, etc.)
session_event = p.get('session_event')
if session_event:
    evt('session_event', {'event': session_event, 'session': session})

# Print all events, one per line
for e in events:
    print(e)
" <<< "$payload" >> "$outfile" 2>/dev/null || true

# --- Activity marker ---
# Touch a marker file on every tool-call hook invocation. fleet-pulse.sh reads
# its mtime to detect an "activity_stuck" bot — one whose pane is animating but
# has made no tool call for a long time (the case pane_stuck can't see). Cheaper
# and more portable than parsing the last tool_call timestamp out of the JSONL.
touch "${BOT_DIR:-${PWD}}/data/.last-tool-call" 2>/dev/null || true

# --- Reap old event files ---
_reap_days="${OBSERVABILITY_REAP_DAYS:-7}"
find "$events_dir" -name "fleet-*.jsonl" -mtime +"$_reap_days" -delete 2>/dev/null || true

# Non-blocking hook — always exit 0
exit 0
