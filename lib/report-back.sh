#!/bin/bash
# Inter-bot communication — worker bots report back to manager
# Usage: report-back.sh <bot-name> <status> <summary> [options] [pr:<url>] [issues:<urls>]
#
# The manager bot's tmux session receives a structured message it can parse.
# Format: [BOTREPORT] <bot> | <status> | <summary> [| progress:<N>] [| pr:<url>] [| artifact:<url>]
#
# Options:
#   --progress N      Progress percentage (0-100), added to BOTREPORT and ledger
#   --artifact URL    Source artifact URL for findings provenance (repeatable)
#
# Example:
#   report-back.sh "work-eng" completed "Fixed auth test" --pr https://github.com/org/repo/pull/42
#   report-back.sh "work-eng" progress "Refactoring auth" --progress 40
#   report-back.sh "work-eng" completed "Audit done" --artifact https://github.com/org/repo/blob/main/REPORT.md

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

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

# Parse optional flags and positional extras
PROGRESS=""
ARTIFACTS=""
POSITIONAL_EXTRAS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --progress)  PROGRESS="$2"; shift 2 ;;
        --artifact)  ARTIFACTS="${ARTIFACTS:+$ARTIFACTS,}$2"; shift 2 ;;
        --pr)        POSITIONAL_EXTRAS+=("pr:$2"); shift 2 ;;
        --issues)    POSITIONAL_EXTRAS+=("issues:$2"); shift 2 ;;
        --skill)     POSITIONAL_EXTRAS+=("skill:$2"); shift 2 ;;
        *)           POSITIONAL_EXTRAS+=("$1"); shift ;;
    esac
done

EXTRAS=""
[ -n "$PROGRESS" ] && EXTRAS="$EXTRAS | progress:$PROGRESS"
for arg in "${POSITIONAL_EXTRAS[@]+"${POSITIONAL_EXTRAS[@]}"}"; do
    EXTRAS="$EXTRAS | $arg"
done
[ -n "$ARTIFACTS" ] && EXTRAS="$EXTRAS | artifact:$ARTIFACTS"

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
    local safe_summary
    safe_summary=$(json_escape "$SUMMARY")

    _write_and_rotate() {
        printf '{"ts":"%s","bot":"%s","status":"%s","summary":"%s","pr_url":"%s","issues":"%s","skill":"%s","progress":"%s","artifact":"%s"}\n' \
            "$ts" "$BOT" "$STATUS" "$safe_summary" "$pr_url" "$issues" "$skill" "$PROGRESS" "$ARTIFACTS" >> "$ledger"

        # Rotate: keep only last 7 days of entries (consistent with fleet-pulse reap).
        local cutoff
        cutoff=$(date_relative "-7 days" "%Y-%m-%dT%H:%M:%SZ") 2>/dev/null || return 0
        local tmp
        tmp=$(safe_mktemp)
        awk -F'"ts":"' -v cutoff="$cutoff" 'NF>1 { split($2, a, "\""); if (a[1] >= cutoff) print }' "$ledger" > "$tmp" \
            && mv "$tmp" "$ledger"
    }
    with_lock "$ledger.lock" _write_and_rotate
}
_emit_ledger_event "${POSITIONAL_EXTRAS[@]+"${POSITIONAL_EXTRAS[@]}"}" || true

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
