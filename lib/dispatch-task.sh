#!/bin/bash
# Manager dispatch wrapper — record the task to the dispatch ledger, then send.
# Usage: dispatch-task.sh [flags] <worker-session> <task...>
#
# Flags:
#   --deadline-min N   Override default deadline (minutes)
#   --repo NAME        Target repo (adds repo:<NAME> to envelope)
#   --priority LEVEL   Priority level (adds priority:<LEVEL> to envelope)
#   --ref URL          Reference URL (adds ref:<URL> to envelope)
#
# When --repo, --priority, or --ref is given, the task text is wrapped in a
# structured [BOTCOMMAND] envelope:
#   [BOTCOMMAND] <caller> | task | <summary> | repo:<repo> | priority:<pri> | ref:<url>
# When none of those flags are passed, the raw task text is sent as-is.
#
# Appends {ts,manager,bot,task,dispatched_at,expected_by} to
# state/dispatch-log.jsonl so the fleet-pulse watchdog can flag the task
# `overdue_dispatch` if no terminal [BOTREPORT] (completed|failed|blocked)
# arrives by expected_by. Manager identity is this bot's $BOT_ID; the deadline
# defaults to $OBSERVABILITY_DISPATCH_DEADLINE (composed into bot.conf) and can
# be overridden with --deadline-min. Sending itself reuses lib/dispatch.sh.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

DEADLINE_MIN=""
DISPATCH_REPO=""
DISPATCH_PRIORITY=""
DISPATCH_REF=""

while [ $# -gt 0 ]; do
    case "$1" in
        --deadline-min) DEADLINE_MIN="${2:?--deadline-min needs a value}"; shift 2 ;;
        --repo)         DISPATCH_REPO="${2:?--repo needs a value}"; shift 2 ;;
        --priority)     DISPATCH_PRIORITY="${2:?--priority needs a value}"; shift 2 ;;
        --ref)          DISPATCH_REF="${2:?--ref needs a value}"; shift 2 ;;
        -*)             echo "dispatch-task: unknown flag '$1'" >&2; exit 1 ;;
        *)              break ;;
    esac
done

WORKER_SESSION="${1:?Usage: dispatch-task.sh [flags] <worker-session> <task...>}"
shift
TASK="$*"
[ -n "$TASK" ] || { echo "dispatch-task: empty task" >&2; exit 1; }

if [ -n "$DISPATCH_REPO" ] || [ -n "$DISPATCH_PRIORITY" ] || [ -n "$DISPATCH_REF" ]; then
    CALLER="${BOT_NAME:-${MANAGER_TMUX:-unknown}}"
    DISPATCH_MSG="[BOTCOMMAND] $CALLER | task | $TASK"
    [ -n "$DISPATCH_REPO" ]     && DISPATCH_MSG="$DISPATCH_MSG | repo:$DISPATCH_REPO"
    [ -n "$DISPATCH_PRIORITY" ] && DISPATCH_MSG="$DISPATCH_MSG | priority:$DISPATCH_PRIORITY"
    [ -n "$DISPATCH_REF" ]      && DISPATCH_MSG="$DISPATCH_MSG | ref:$DISPATCH_REF"
else
    DISPATCH_MSG="$TASK"
fi

# Fail before recording if the worker isn't there — no orphan ledger entries.
if ! check_tmux_session "$WORKER_SESSION"; then
    echo "dispatch-task: session '$WORKER_SESSION' does not exist" >&2
    exit 1
fi

if [ -n "$DEADLINE_MIN" ]; then
    DEADLINE_S=$(( DEADLINE_MIN * 60 ))
else
    DEADLINE_S="${OBSERVABILITY_DISPATCH_DEADLINE:-1800}"
fi

MANAGER="${BOT_ID:-${BOT_NAME:-unknown}}"
CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$(cd "$LIB_DIR/.." && pwd)}"
LEDGER_DIR="$CLAUDLOBBY_ROOT/state"
mkdir -p "$LEDGER_DIR"
LEDGER="$LEDGER_DIR/dispatch-log.jsonl"

now_epoch=$(date +%s)
expected_by=$(( now_epoch + DEADLINE_S ))
ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Escape backslash + double-quote for valid JSON (no jq dependency).
safe_task=$(json_escape "$TASK")

_append_ledger() {
    printf '{"ts":"%s","manager":"%s","bot":"%s","task":"%s","dispatched_at":%s,"expected_by":%s}\n' \
        "$ts" "$MANAGER" "$WORKER_SESSION" "$safe_task" "$now_epoch" "$expected_by" >> "$LEDGER"
}
with_lock "$LEDGER.lock" _append_ledger

# Send via the low-level race-safe primitive (re-validates the session).
"$LIB_DIR/dispatch.sh" "$WORKER_SESSION" "$DISPATCH_MSG"
