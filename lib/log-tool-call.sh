#!/usr/bin/env bash
# log-tool-call.sh — Reference Claude Code hook script for fleet observability.
#
# Reads the hook JSON payload from stdin and writes a JSONL entry to
# the bot's logs/activity.jsonl. Works as both PreToolUse and PostToolUse hook.
#
# Usage in fleet.yaml:
#   hooks:
#     PreToolUse:
#       - command: "$CLAUDLOBBY_ROOT/lib/log-tool-call.sh"
#     PostToolUse:
#       - command: "$CLAUDLOBBY_ROOT/lib/log-tool-call.sh"
#
# Output format (one JSON object per line):
#   {"ts":"2026-05-09T14:30:00Z","event":"PreToolUse","tool":"Bash","bot":"eng-1","session":"abc123"}

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

# Read the hook payload from stdin (Claude Code sends JSON)
payload="$(cat)"

# Determine log directory from BOT_DIR env var (set in bot.conf)
log_dir="${BOT_DIR:-${PWD}}/logs"
mkdir -p "$log_dir"

ts=$(ts_iso)
bot="${BOT_ID:-unknown}"

# Single python3 call: parse payload + build output in one shot.
# Values are passed via env vars to avoid shell injection.
entry=$(
  BOT_ID_VAL="$bot" \
  TS_VAL="$ts" \
  python3 -c "
import json, os, sys
try:
    p = json.loads(sys.stdin.read())
except (json.JSONDecodeError, ValueError):
    p = {}
print(json.dumps({
    'ts': os.environ['TS_VAL'],
    'event': p.get('hook_event_name', 'unknown'),
    'tool': p.get('tool_name', ''),
    'bot': os.environ['BOT_ID_VAL'],
    'session': p.get('session_id', ''),
}, separators=(',', ':')))
" <<< "$payload"
)

# Append to activity log
printf '%s\n' "$entry" >> "$log_dir/activity.jsonl"

# Exit 0 — non-blocking hook, never interfere with tool execution
exit 0
