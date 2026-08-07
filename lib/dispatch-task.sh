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
# Envelope sends MINT a task id (mint_task_id, lib-common), record it in the
# ledger row AND transmit it as `task:<id>` — join semantics live in
# dispatch-overdue.py (overdue_all docstring). Raw-text sends (no flags) stay
# id-less: an id recorded but never transmitted would guarantee a
# false-positive overdue, since the worker cannot echo what it never saw.
#
# Appends {ts,manager,bot,task_id,workstream,task,dispatched_at,expected_by,
# claudron_hits} to state/dispatch-log.jsonl (self-rotated via
# rotate_jsonl_by_ts) so the fleet-pulse watchdog can flag `overdue_dispatch`
# if no terminal [BOTREPORT] (completed|failed|blocked) arrives by expected_by.
# Manager identity is this bot's $BOT_ID; the deadline defaults to
# $OBSERVABILITY_DISPATCH_DEADLINE (composed into bot.conf) and can be
# overridden with --deadline-min. Sending itself reuses lib/dispatch.sh.
#
# Claudron query-before preflight (plan P1e, fork F7) — env-knobbed, off by
# default:
#   CLAUDRON_QUERY_BEFORE=1  enable the preflight
#   CLAUDRON_QUERY_LIMIT     max fleet-memory pointers to inject (default 3)
# When enabled and the claudron CLI + CLAUDRON_VAULT_PATH resolve, the task
# text gains a single-line "[fleet memory: <title> (<abs path>); ...]" prefix
# of lookup pointers (titles + paths only, never note bodies — the worker
# reads the files itself). claudron_hits in the ledger row records how many
# pointers were injected ("" = preflight did not run, "0" = ran, no hits).
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

# _flag_val <flag> <value?> — explicit missing-value guard (NOT ${2:?}: see
# the arg-guard note below — expansion faults exit 0 through the EXIT trap).
_flag_val() {
    if [ -z "${2:-}" ]; then
        echo "dispatch-task: $1 needs a value" >&2
        exit 1
    fi
    printf '%s' "$2"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --deadline-min) DEADLINE_MIN=$(_flag_val "$1" "${2:-}"); shift 2 ;;
        --repo)         DISPATCH_REPO=$(_flag_val "$1" "${2:-}"); shift 2 ;;
        --priority)     DISPATCH_PRIORITY=$(_flag_val "$1" "${2:-}"); shift 2 ;;
        --ref)          DISPATCH_REF=$(_flag_val "$1" "${2:-}"); shift 2 ;;
        --workstream)   DISPATCH_WORKSTREAM=$(_flag_val "$1" "${2:-}"); shift 2 ;;
        --botcommand)   FORCE_ENVELOPE=1; shift ;;
        -*)             echo "dispatch-task: unknown flag '$1'" >&2; exit 1 ;;
        *)              break ;;
    esac
done

# Explicit arg guard — NOT ${1:?}: under macOS bash 3.2 an expansion fault
# exits 0 through lib-common's EXIT trap (its `|| true` cleanup tail resets
# the reported status), silently masking usage errors.
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

# Fail before recording if the worker isn't there — no orphan ledger entries,
# and no preflight subprocesses spent on a dead-session dispatch.
if ! check_tmux_session "$WORKER_SESSION" "$WORKER_SOCKET"; then
    echo "dispatch-task: session '$WORKER_SESSION' does not exist" >&2
    exit 1
fi

# --- Claudron query-before preflight (plan P1e, fork F7) --------------------
# Prepends compact fleet-memory pointers to the task before the envelope is
# built, so both enveloped and raw-text dispatches carry them and the ledger
# records the enriched task. The wedge must never block a dispatch: any
# missing prerequisite, lookup failure, or unparseable output degrades to a
# plain send. CLAUDRON_VAULT_PATH is the canonical vault address and the CLI
# reads it itself (Claudron CLI_CONTRACT.md §Environment, row 2) — this wedge
# exports nothing and passes no --vault, so bot and CLI can never resolve two
# different vaults. A vault that does not resolve exits 3 with nothing on
# stdout; that and every other failure are absorbed by the
# `2>/dev/null || return 0` net plus the parser's JSON validation.
# Result shape: the CLI-contract envelope {ok, command, data:{query, results}}.
# Version skew is real — the host CLI is installed out of band and can lag the
# repo's [vault] pin — so the parser reads data.results then top-level results
# (the pre-envelope 0.1.x shape) and degrades to injecting nothing rather than
# erroring.
CLAUDRON_HITS=""
_claudron_query_before() {
    [ "${CLAUDRON_QUERY_BEFORE:-}" = "1" ] || return 0
    [ -d "${CLAUDRON_VAULT_PATH:-}" ] || return 0
    # The documented opt-in precondition (and the common no-op on
    # claudron-less hosts); every failure past here is caught by the
    # 2>/dev/null + return-0 net on the invocations themselves.
    command -v claudron >/dev/null 2>&1 || return 0
    local raw parsed pointers query
    # QUERY IS THE HEAD OF THE TASK, NOT THE WHOLE TASK.
    #
    # Passing the entire payload collapses the ranking rather than widening it.
    # Measured: a short query graded 160/120/80/80 across candidates; the same
    # lookup with a full dispatch as the query returned a FLAT 200 for four
    # unrelated notes. Whatever sits at the global ceiling comes back, because a
    # paragraph resembles nothing in particular. The result is a pointer set that
    # is topically INERT — the same handful returned across unrelated subjects.
    #
    # And a signal that fires on every input carries no information about the
    # input. Two readers independently reported these pointers becoming chrome
    # within an hour — including, on the day it applied, the note whose title
    # names the exact error class both of them then committed. That is the case
    # for this fix on its own, separate from anything the ranking does.
    #
    # A CHARACTER cap, not a line cap: `TASK="$*"` and a dispatch is one line by
    # construction (the tmux send is single-line), so "first line or two" is the
    # whole payload and would change nothing.
    #
    # The head is the right slice rather than an arbitrary one: the fleet's own
    # comms protocol requires a dispatch to lead with the decision or the ask, so
    # the subject is front-loaded by construction. The `[fleet memory: …]` prefix
    # is prepended BELOW, after this lookup, so it cannot poison its own query.
    query=$(printf '%s' "$TASK" | cut -c1-"${CLAUDRON_QUERY_MAX_CHARS:-200}")
    raw=$(claudron lookup --json \
        --limit "${CLAUDRON_QUERY_LIMIT:-3}" "$query" 2>/dev/null) || return 0
    # Emits "<count>\t<title (abs path); ...>". Claudron-supplied strings are
    # sanitized to printable-by-construction before use: pipes become "/"
    # (the [BOTCOMMAND] envelope is pipe-delimited) and runs of whitespace OR
    # control bytes collapse to single spaces — an embedded newline would
    # split the single-line ledger row into invalid JSON that line-oriented
    # rotation then truncates, and a non-whitespace control (a YAML "\e" in a
    # note title reaches here as a raw ESC) would ledger bytes the worker
    # never receives once the tmux-side sanitizer strips them.
    # Any unexpected JSON shape exits 1 into the return-0 net.
    parsed=$(printf '%s' "$raw" | python3 -c '
import json, re, sys

def clean(value):
    # Whole CSI sequences first — collapsing the ESC alone would leave the
    # printable remainder ("[31m") as residue in the pointer text.
    value = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", str(value))
    return re.sub(r"[\s\x00-\x1f\x7f]+", " ", value.replace("|", "/")).strip()

try:
    data = json.load(sys.stdin)
    # 0.2.0 envelope {data:{results}} first, then 0.1.x flat {results};
    # any other shape hits the except below via .get/.rstrip failing.
    inner = data.get("data") or data
    results = inner.get("results") or []
    root = clean(sys.argv[1].rstrip("/"))
    pointers = []
    for r in results:
        title = clean(r.get("title", ""))
        path = clean(r.get("path", ""))
        if title and path:
            pointers.append("%s (%s/%s)" % (title, root, path))
except Exception:
    sys.exit(1)
sys.stdout.write("%d\t%s" % (len(pointers), "; ".join(pointers)))
' "$CLAUDRON_VAULT_PATH" 2>/dev/null) || return 0
    CLAUDRON_HITS="${parsed%%$'\t'*}"
    pointers="${parsed#*$'\t'}"
    if [ -n "$pointers" ]; then
        TASK="[fleet memory: $pointers] $TASK"
    fi
}
_claudron_query_before

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

if [ -n "$DEADLINE_MIN" ]; then
    DEADLINE_S=$(( DEADLINE_MIN * 60 ))
else
    DEADLINE_S="${OBSERVABILITY_DISPATCH_DEADLINE:-1800}"
fi

MANAGER="${BOT_ID:-${BOT_NAME:-unknown}}"
CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$(cd "$LIB_DIR/.." && pwd)}"
LEDGER="$(dispatch_ledger_path)"
mkdir -p "$(dirname "$LEDGER")"

now_epoch=$(date +%s)
expected_by=$(( now_epoch + DEADLINE_S ))
ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Escape backslash + double-quote for valid JSON (no jq dependency).
safe_task=$(json_escape "$TASK")

_append_ledger() {
    # Schema-uniform rows: task_id/workstream/claudron_hits always emitted
    # (empty = absent, matching the report ledger's always-emit convention;
    # every consumer treats "" as falsy). claudron_hits is digits-or-empty by
    # construction (the preflight parser prints a count), so no escaping.
    printf '{"ts":"%s","manager":"%s","bot":"%s","task_id":"%s","workstream":"%s","task":"%s","dispatched_at":%s,"expected_by":%s,"claudron_hits":"%s"}\n' \
        "$ts" "$MANAGER" "$WORKER_SESSION" "$TASK_ID" "$(json_escape "$DISPATCH_WORKSTREAM")" "$safe_task" "$now_epoch" "$expected_by" "$CLAUDRON_HITS" >> "$LEDGER"
    rotate_jsonl_by_ts "$LEDGER"
}
with_lock "$LEDGER.lock" _append_ledger

# Send via the low-level race-safe primitive (re-validates the session).
"$LIB_DIR/dispatch.sh" "$WORKER_SESSION" "$DISPATCH_MSG"
