#!/bin/bash
# Inter-bot communication — worker bots report back to manager
# Usage: report-back.sh <bot-name> <status> <summary> [pr:<url>] [issues:<urls>]
#
# The manager bot's tmux session receives a structured message it can parse.
# Format: [BOTREPORT] <bot> | <status> | <summary> [| pr:<url>] [| issues:<urls>]
#
# Example:
#   report-back.sh "work-eng" "DONE" "Fixed auth test" "pr:https://github.com/org/repo/pull/42"

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

MANAGER_SESSION="${MANAGER_TMUX:-claude-bot}"
BOT="$1"
STATUS="$2"
SUMMARY="$3"
shift 3

# Validate status against allowed set
case "$STATUS" in
    completed|progress|blocked|failed) ;;
    *)
        echo "report-back: invalid status '$STATUS' (must be: completed|progress|blocked|failed)" >&2
        exit 1
        ;;
esac

# Sanitize summary for tmux safety
SUMMARY="$(sanitize_tmux_input "$SUMMARY")"

EXTRAS=""
for arg in "$@"; do
    EXTRAS="$EXTRAS | $arg"
done

MESSAGE="[BOTREPORT] $BOT | $STATUS | $SUMMARY$EXTRAS"

"$_TMUX_BIN" send-keys -t "$MANAGER_SESSION" "$MESSAGE" || true
sleep 0.3
"$_TMUX_BIN" send-keys -t "$MANAGER_SESSION" Enter || true

# Append structured JSONL event to the fleet-level report-back ledger.
# Path follows overlay convention: local/<fleet>/runtime/ or root runtime/.
_emit_ledger_event() {
    local ledger_dir
    if [ -n "${FLEET_NAME:-}" ]; then
        ledger_dir="$CLAUDLOBBY_ROOT/local/$FLEET_NAME/runtime"
    else
        ledger_dir="$CLAUDLOBBY_ROOT/runtime/fleet"
    fi
    mkdir -p "$ledger_dir"
    local ledger="$ledger_dir/report-back.jsonl"
    local ts
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    # Extract optional fields from extras
    local pr_url="" issues="" skill=""
    for _ex in "$@"; do
        case "$_ex" in
            pr:*)     pr_url="${_ex#pr:}" ;;
            issues:*) issues="${_ex#issues:}" ;;
            skill:*)  skill="${_ex#skill:}" ;;
        esac
    done

    # Build JSON with printf — no jq dependency for the hot path.
    # Escape double quotes and backslashes in summary for valid JSON.
    local safe_summary
    safe_summary=$(printf '%s' "$SUMMARY" | sed 's/\\/\\\\/g; s/"/\\"/g')

    printf '{"ts":"%s","bot":"%s","status":"%s","summary":"%s","pr_url":"%s","issues":"%s","skill":"%s"}\n' \
        "$ts" "$BOT" "$STATUS" "$safe_summary" "$pr_url" "$issues" "$skill" >> "$ledger"
}
_emit_ledger_event "$@"

# Mirror to fleet-state if helper is present
_FS=$(dirname "$0")/fleet-state-update.sh
if [ -x "$_FS" ]; then
  case "$STATUS" in
    completed) FS=idle ;;
    blocked)   FS=blocked ;;
    failed)    FS=idle ;;
    progress)  FS=working ;;
    *)         FS=idle ;;
  esac
  "$_FS" "$BOT" "$FS" "" "" "$SUMMARY" || true
fi
