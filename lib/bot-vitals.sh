#!/usr/bin/env bash
# bot-vitals.sh — Claude Code hook script for fleet observability.
#
# Reads the hook JSON payload from stdin and writes a JSONL event to
# the bot's data/events/fleet-YYYY-MM-DD.jsonl. Works as both
# PreToolUse and PostToolUse hook.
#
# Captures: tool_call, mcp_error, context_warning, rate_limit, session events.
# Reaps event files older than 7 days on each invocation.
#
# Supersedes the older log-tool-call.sh reference hook.
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
error = p.get('error', '')

events = []

def evt(etype, data):
    events.append(json.dumps(
        {'ts': ts, 'bot': bot, 'type': etype, 'source': 'vitals', 'data': data},
        separators=(',', ':'),
    ))

# Always emit a tool_call event for any hook invocation with a tool name
if tool:
    evt('tool_call', {'tool': tool, 'event': hook_event, 'session': session})

# Detect MCP errors (tool_name contains mcp prefix or error field present)
if error and 'mcp' in tool.lower():
    evt('mcp_error', {'server': tool, 'error': str(error)[:500]})

# Context warnings — look for context_window_percent in payload
ctx_pct = p.get('context_window_percent')
if ctx_pct is not None:
    try:
        pct = int(ctx_pct)
        if pct >= 50:
            evt('context_warning', {'pct': pct})
    except (ValueError, TypeError):
        pass

# Rate limit detection
if 'rate_limit' in str(error).lower() or p.get('rate_limited'):
    evt('rate_limit', {'tool': tool, 'error': str(error)[:500]})

# Session events (start, stop, etc.)
session_event = p.get('session_event')
if session_event:
    evt('session_event', {'event': session_event, 'session': session})

# Print all events, one per line
for e in events:
    print(e)
" <<< "$payload" >> "$outfile" 2>/dev/null || true

# --- Reap old event files (>7 days) ---
find "$events_dir" -name "fleet-*.jsonl" -mtime +7 -delete 2>/dev/null || true

# Non-blocking hook — always exit 0
exit 0
