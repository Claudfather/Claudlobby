#!/bin/bash
# Manager dispatch wrapper — record the task to the dispatch ledger, then send.
# Usage: dispatch-task.sh [flags] <worker-session> <task...>
#
# Flags:
#   --deadline-min N   Override default deadline (minutes)
#   --repo NAME        Target repo (adds repo:<NAME> to envelope)
#   --priority LEVEL   Priority level (adds priority:<LEVEL> to envelope)
#   --ref URL          Reference URL (adds ref:<URL> to envelope)
#   --workstream ID    Workstream this task advances (envelope + ledger)
#   --botcommand       Force the [BOTCOMMAND] envelope with no other fields
#
# When any envelope flag is given, the task is wrapped:
#   [BOTCOMMAND] <caller> | task | <summary> [| repo:..] [| priority:..]
#     [| ref:..] [| workstream:..] | task:<task-id>
# Envelope sends MINT a task id (mint_task_id, lib-common) — the id is
# recorded in the ledger row AND transmitted, so the worker can echo it in
# its [BOTREPORT] and the overdue watchdog joins on identity. Raw-text sends
# (no flags) stay fully legacy: no id anywhere, matched by (bot, ts) — an id
# recorded but never transmitted would guarantee a false-positive overdue,
# since the worker cannot echo what it never saw.
#
# Appends {ts,manager,bot,task,task_id?,dispatched_at,expected_by} to
# state/dispatch-log.jsonl (self-rotated on OBSERVABILITY_REAP_DAYS, like the
# report ledger) so the fleet-pulse watchdog can flag `overdue_dispatch` if no
# terminal [BOTREPORT] (completed|failed|blocked) arrives by expected_by.
# Manager identity is this bot's $BOT_ID; the deadline defaults to
# $OBSERVABILITY_DISPATCH_DEADLINE (composed into bot.conf) and can be
# overridden with --deadline-min. Sending itself reuses lib/dispatch.sh.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

DEADLINE_MIN=""
DISPATCH_REPO=""
DISPATCH_PRIORITY=""
DISPATCH_REF=""
DISPATCH_WORKSTREAM=""
FORCE_ENVELOPE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --deadline-min) DEADLINE_MIN="${2:?--deadline-min needs a value}"; shift 2 ;;
        --repo)         DISPATCH_REPO="${2:?--repo needs a value}"; shift 2 ;;
        --priority)     DISPATCH_PRIORITY="${2:?--priority needs a value}"; shift 2 ;;
        --ref)          DISPATCH_REF="${2:?--ref needs a value}"; shift 2 ;;
        --workstream)   DISPATCH_WORKSTREAM="${2:?--workstream needs a value}"; shift 2 ;;
        --botcommand)   FORCE_ENVELOPE=1; shift ;;
        -*)             echo "dispatch-task: unknown flag '$1'" >&2; exit 1 ;;
        *)              break ;;
    esac
done

# Explicit arg guard — NOT ${1:?}: under macOS bash 3.2 the expansion error
# path exits 0 through the error trap, silently masking usage errors.
if [ $# -lt 1 ]; then
    echo "Usage: dispatch-task.sh [flags] <worker-session> <task...>" >&2
    exit 1
fi
WORKER_SESSION="$1"
shift
TASK="$*"
[ -n "$TASK" ] || { echo "dispatch-task: empty task" >&2; exit 1; }

# Worker's private tmux server socket, reverse-resolved from its session name
# (matches what dispatch.sh resolves for the actual send). Tolerant: an
# unresolvable peer yields an empty socket so the check below reports a clean
# "does not exist" rather than the resolver's guard crashing this script.
WORKER_SOCKET="$(tmux_socket_for_session "$WORKER_SESSION" 2>/dev/null || true)"

TASK_ID=""
if [ -n "$FORCE_ENVELOPE" ] || [ -n "$DISPATCH_REPO" ] || [ -n "$DISPATCH_PRIORITY" ] \
   || [ -n "$DISPATCH_REF" ] || [ -n "$DISPATCH_WORKSTREAM" ]; then
    TASK_ID=$(mint_task_id)
    CALLER="${BOT_NAME:-${MANAGER_TMUX:-unknown}}"
    DISPATCH_MSG="[BOTCOMMAND] $CALLER | task | $TASK"
    [ -n "$DISPATCH_REPO" ]       && DISPATCH_MSG="$DISPATCH_MSG | repo:$DISPATCH_REPO"
    [ -n "$DISPATCH_PRIORITY" ]   && DISPATCH_MSG="$DISPATCH_MSG | priority:$DISPATCH_PRIORITY"
    [ -n "$DISPATCH_REF" ]        && DISPATCH_MSG="$DISPATCH_MSG | ref:$DISPATCH_REF"
    [ -n "$DISPATCH_WORKSTREAM" ] && DISPATCH_MSG="$DISPATCH_MSG | workstream:$DISPATCH_WORKSTREAM"
    DISPATCH_MSG="$DISPATCH_MSG | task:$TASK_ID"
else
    DISPATCH_MSG="$TASK"
fi

# Fail before recording if the worker isn't there — no orphan ledger entries.
if ! check_tmux_session "$WORKER_SESSION" "$WORKER_SOCKET"; then
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
    local id_field=""
    [ -n "$TASK_ID" ] && id_field="\"task_id\":\"$TASK_ID\","
    local ws_field=""
    [ -n "$DISPATCH_WORKSTREAM" ] && ws_field="\"workstream\":\"$(json_escape "$DISPATCH_WORKSTREAM")\","
    printf '{"ts":"%s","manager":"%s","bot":"%s",%s%s"task":"%s","dispatched_at":%s,"expected_by":%s}\n' \
        "$ts" "$MANAGER" "$WORKER_SESSION" "$id_field" "$ws_field" "$safe_task" "$now_epoch" "$expected_by" >> "$LEDGER"

    # Self-rotate on the fleet retention window (mirrors report-back.jsonl —
    # closes the unbounded-growth half of #467). Entries older than the
    # window have long since either closed or aged past the overdue cap.
    # CONSTRAINT: keep DISPATCH_OVERDUE_MAX_AGE_S (default 24h) BELOW this
    # window (default 7d) — a max_age raised past the reap window would let
    # rotation silently prune a still-alerting dispatch row.
    local reap_days="${OBSERVABILITY_REAP_DAYS:-7}"
    local cutoff
    cutoff=$(date_relative "-${reap_days} days" "%Y-%m-%dT%H:%M:%SZ") 2>/dev/null || return 0
    local tmp
    tmp=$(safe_mktemp)
    awk -F'"ts":"' -v cutoff="$cutoff" 'NF>1 { split($2, a, "\""); if (a[1] >= cutoff) print }' "$LEDGER" > "$tmp" \
        && mv "$tmp" "$LEDGER"
}
with_lock "$LEDGER.lock" _append_ledger

# Send via the low-level race-safe primitive (re-validates the session).
"$LIB_DIR/dispatch.sh" "$WORKER_SESSION" "$DISPATCH_MSG"
