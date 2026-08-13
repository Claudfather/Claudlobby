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
#   --supersedes ID    This dispatch REPLACES an earlier one; the named task id is
#                      retired rather than left to age out and page. Opt-in, and
#                      inert when omitted — see the ledger-write comment below.
#                      When omitted and the bot already has an open row that
#                      references the same issue, a note names the id to pass
#                      (#1032). It never blocks and never guesses.
#   --botcommand       Force the [BOTCOMMAND] envelope with no other fields.
#                      Now exactly equivalent to `--type task` — kept as an
#                      alias rather than removed because running managers hold
#                      it in composed context, and a composed file does not
#                      reach a live session until it restarts.
#   --type TYPE        Envelope type: task|cancel|compact|restart|query
#                      (default task). Implies --botcommand. ONLY `task` mints.
#
# `task` envelope sends MINT a task id (mint_task_id, lib-common), record it in
# the ledger row AND transmit it as `task:<id>` — join semantics live in
# dispatch-overdue.py (overdue_all docstring). Raw-text sends (no flags) stay
# id-less: an id recorded but never transmitted would guarantee a
# false-positive overdue, since the worker cannot echo what it never saw.
#
# NON-`task` ENVELOPE SENDS ARE ALSO ID-LESS (#1187), for the same reason read
# forward instead of back: a `query` is answered inline BY DEFINITION and can
# never produce the terminal report an id exists to be joined against, so an id
# minted for one is unclosable from the moment it is written. The envelope
# format and the tracking used to be the same decision — `--botcommand` alone
# minted — which meant a manager who wanted the fleet message format for a peer
# note got a permanently open row as a side effect. They are separate now.
# Every send still writes a ledger row; only `task` writes one with an id.
#
# Appends {ts,manager,bot,task_id,workstream,task,dispatched_at,expected_by,
# claudron_hits,supersedes,open_at_dispatch} to state/dispatch-log.jsonl (self-rotated via
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
DISPATCH_SUPERSEDES=""
FORCE_ENVELOPE=""
DISPATCH_TYPE="task"

# The [BOTCOMMAND] type vocabulary. PROSE IS THE SOURCE: this list restates
# `library/protocols/worker-lifecycle.md` and `library/protocols/dispatch.md`,
# which are what a bot actually reads, and `tests/test_dispatch_type.py` parses
# both docs and fails if this copy drifts from either. A second definition of a
# shared vocabulary that nothing reconciles is how the estate's greps became
# ambiguous; this one is reconciled.
DISPATCH_TYPES="task cancel compact restart query"

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
        --supersedes)   DISPATCH_SUPERSEDES=$(_flag_val "$1" "${2:-}"); shift 2 ;;
        --botcommand)   FORCE_ENVELOPE=1; shift ;;
        --type)         DISPATCH_TYPE=$(_flag_val "$1" "${2:-}"); FORCE_ENVELOPE=1; shift 2 ;;
        -*)             echo "dispatch-task: unknown flag '$1'" >&2; exit 1 ;;
        *)              break ;;
    esac
done

# REFUSE an unrecognised type rather than falling back to `task`. A fallback is
# the defect this flag exists to remove, re-created one layer up: `--type quiery`
# would mint a tracked row for a message that asks nothing, and the caller who
# typed it would have no way to tell. Loud and unsent beats sent and untrue.
case " $DISPATCH_TYPES " in
    *" $DISPATCH_TYPE "*) ;;
    *)
        echo "dispatch-task: unknown --type '$DISPATCH_TYPE' (must be one of: $DISPATCH_TYPES)" >&2
        exit 1
        ;;
esac

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
    local raw parsed pointers query subject stripped
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
    # The head is only the subject when the CALLER leads with the subject. The
    # fleet comms protocol asks for that, but this file cannot enforce a
    # convention about text composed before it ever runs -- and measured traffic
    # breaks it. A real dispatch whose head is a `[fleet memory: ...]` preamble
    # caps to 200 characters of pure boilerplate, cut mid-word, with ZERO subject
    # matter in it. That reproduces the exact defect this wedge exists to fix,
    # via saturation instead of raw length. So the one known-shape prefix is
    # stripped before the cap, the same way claudron strings are sanitized to a
    # known shape below.
    #
    # This wedge does not poison its OWN query. The prepend at the foot of this
    # function runs after the lookup -- verified by execution rather than by line
    # order: the pointer titles a run injects are absent from the query that same
    # run sent. Any preamble seen here was carried in by the caller.
    #
    # The loop, rather than a single strip: a stacked preamble (a dispatch
    # composed from an already-rendered one) puts the query straight back at 100%
    # boilerplate, which is the same defect and not a different one.
    #
    # Stripping to the FIRST "] " is deliberate. A note title containing "]"
    # leaves residue behind; consuming to the LAST one would eat subject matter.
    # Residue only degrades the query, eating the subject reproduces the bug --
    # so the ambiguous case fails toward residue.
    subject="$TASK"
    while [ "${subject#\[fleet memory: }" != "$subject" ]; do
        # Guards the malformed no-"] " case: the expansion below is then a no-op
        # and the loop would never terminate.
        stripped="${subject#*\] }"
        [ "$stripped" != "$subject" ] || break
        subject="$stripped"
    done
    # A task that was ONLY a preamble strips to nothing. Querying on empty is
    # not a degraded lookup, it is the ORIGINAL defect through the opposite
    # door: with no subject to rank against, whatever sits at the global ceiling
    # comes back, which is the inert pointer set this wedge exists to stop
    # emitting. Nothing to search on means no pointers, not arbitrary ones.
    case "$subject" in
        *[![:space:]]*) : ;;
        *) return 0 ;;
    esac
    query=$(printf '%s' "$subject" | cut -c1-"${CLAUDRON_QUERY_MAX_CHARS:-200}")
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
EXPECTED_BY_JSON=""
if [ -n "$FORCE_ENVELOPE" ] || [ -n "$DISPATCH_REPO" ] || [ -n "$DISPATCH_PRIORITY" ] \
   || [ -n "$DISPATCH_REF" ] || [ -n "$DISPATCH_WORKSTREAM" ]; then
    # THE ENVELOPE AND THE TRACKING ARE NOW SEPARATE DECISIONS (#1187). They used
    # to be one: any envelope send minted, so a manager who wanted the fleet
    # message format for a peer note — a finding, a relay, a retraction — got a
    # tracked row as a side effect. Nothing was ever asked of the recipient, so
    # nothing could ever close it. Measured: 68 open rows on this host, 57 of
    # them to managers, none of them answerable.
    #
    # ONLY `task` MINTS, and the reason it is the right axis is that `query` is
    # not an intent label the sender has to intuit — `worker-lifecycle` DEFINES
    # it as answered inline, incapable of producing a terminal report. The sender
    # asserts a checkable property rather than guessing.
    #
    # THE INVARIANT, and it is what the axis was chosen to satisfy: a
    # misclassification must degrade to UNTRACKED, never to UNCLOSABLE. Calling a
    # real task a `query` costs an id-less row — exactly what every raw-text send
    # already is, a known and survivable state, and the recipient can still do the
    # work and report. The reverse, which is what shipped before this, costs a row
    # that no one can ever close.
    if [ "$DISPATCH_TYPE" = "task" ]; then
        TASK_ID=$(mint_task_id)
    else
        # AND NO DEADLINE — withholding the id alone is half a fix, which is
        # what the first version of this change shipped. `expected_by` is what
        # the watchdog actually reads: `dispatch-overdue.py --all` matches on
        # the deadline, not on the id, so an id-less row with a deadline still
        # goes overdue and still pushes a `[FLEET-PULSE]` alert — and because
        # `overdue_ids` drops the empty id, that alert says a task is late and
        # NAMES NOTHING. Measured: `vera <at> <by> 100 -`. Strictly worse to
        # diagnose than the row this change set out to stop minting.
        #
        # `null` rather than 0 or omitted: `_classify_all` skips rows whose
        # `expected_by` is not an int, and `open_dispatches` documents that it
        # deliberately does not filter on it — so a null deadline is silent in
        # both doors with no consumer change. Verified against both.
        #
        # RAW-TEXT SENDS KEEP THEIR DEADLINE. They are id-less too, but they are
        # matched by bot+time on purpose ("one report closes all open dispatches
        # for that bot"), which is documented behaviour and a live call pattern.
        # The gate is the TYPE, never the emptiness of the id.
        EXPECTED_BY_JSON="null"
    fi
    CALLER="${BOT_NAME:-${MANAGER_TMUX:-unknown}}"
    DISPATCH_MSG="[BOTCOMMAND] $CALLER | $DISPATCH_TYPE | $TASK"
    [ -n "$DISPATCH_REPO" ]       && DISPATCH_MSG="$DISPATCH_MSG | repo:$DISPATCH_REPO"
    [ -n "$DISPATCH_PRIORITY" ]   && DISPATCH_MSG="$DISPATCH_MSG | priority:$DISPATCH_PRIORITY"
    [ -n "$DISPATCH_REF" ]        && DISPATCH_MSG="$DISPATCH_MSG | ref:$DISPATCH_REF"
    [ -n "$DISPATCH_WORKSTREAM" ] && DISPATCH_MSG="$DISPATCH_MSG | workstream:$DISPATCH_WORKSTREAM"
    # Guarded, because TASK_ID is now legitimately empty for a non-`task` type.
    # An unguarded append transmits a bare `| task:` — an envelope field with no
    # value, which a worker would echo back as nothing and which reads like a
    # truncated message rather than a deliberate absence.
    [ -n "$TASK_ID" ] && DISPATCH_MSG="$DISPATCH_MSG | task:$TASK_ID"
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
# Empty unless the envelope branch above withheld the deadline for a non-`task`
# type. Resolved here rather than there because the deadline is not computed
# until now, and a `null` set earlier must survive this assignment.
[ -n "$EXPECTED_BY_JSON" ] || EXPECTED_BY_JSON="$expected_by"
ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# --- undeclared-supersession visibility (#1032) -------------------------------
# `--supersedes` retired ZERO rows in a week here because nobody passed it, while
# 25 of 43 mispaired rows were re-dispatch shaped — exactly its case. A usage gap
# closed by intending to remember is not closed, so the tool says it at the only
# moment the intent exists.
#
# TWO TIERS, and the split is a measurement not a preference. 51% of id'd
# dispatches go to a bot that already holds an open row, so speaking on that would
# fire on half of all traffic — the same dead-signal defect #1032 is about. Only
# the shared-reference case (11%) is said out loud; the count is recorded.
#
# Never blocks and never rewrites intent: queueing two tasks on one bot is
# legitimate, and the tool cannot tell a queue from a replacement. Skipped
# entirely when the caller already declared, and fail-open at every step — a
# dispatch must never fail because a hint helper was unavailable.
OPEN_AT_DISPATCH=0
if [ -n "$TASK_ID" ] && command -v python3 >/dev/null 2>&1; then
    _dt_reports="$(fleet_runtime_dir)/report-back.jsonl"
    _dt_hint=$(python3 "$LIB_DIR/dispatch-supersede-hint.py" \
        --bot "$WORKER_SESSION" --dispatch-log "$LEDGER" \
        --report-ledger "$_dt_reports" --task "$TASK" --ref "$DISPATCH_REF" 2>/dev/null || true)
    OPEN_AT_DISPATCH=$(python3 "$LIB_DIR/dispatch-supersede-hint.py" --count-only \
        --bot "$WORKER_SESSION" --dispatch-log "$LEDGER" \
        --report-ledger "$_dt_reports" --task "$TASK" --ref "$DISPATCH_REF" 2>/dev/null || echo 0)
    case "$OPEN_AT_DISPATCH" in ''|*[!0-9]*) OPEN_AT_DISPATCH=0 ;; esac
    # Only the loud tier is printed, and only when the caller has NOT declared.
    if [ -z "$DISPATCH_SUPERSEDES" ] && [ -n "$_dt_hint" ]; then
        printf '%s\n' "$_dt_hint" >&2
    fi
fi

# Escape backslash + double-quote for valid JSON (no jq dependency).
safe_task=$(json_escape "$TASK")

_append_ledger() {
    # `supersedes` is the one field that records INTENT rather than what happened.
    # A re-dispatch replaces an earlier task; the older row will never be separately
    # answered, so it ages out and pages the manager about work that shipped. Nothing
    # downstream can infer that from the ledger, because the ledger records what was
    # SENT, never what was MEANT — two dispatches to one bot look identical whether
    # the second replaces the first or queues behind it. Only the caller knows, and
    # only at this moment.
    #
    # OPT-IN, AND THAT IS THE SAFETY PROPERTY. Omitting the flag reproduces today's
    # behaviour exactly: the row retires on nothing and the watchdog eventually pages.
    # So a forgotten flag costs a false page — the status quo — and can never retire a
    # dispatch someone still owes. The failure mode of forgetting is inert, which is
    # what makes this safe to default off. Inferring supersession from timing instead
    # was measured and rejected: over 189 closed rows, 14 were "superseded" by a later
    # closure and still answered afterwards, 3 of them unambiguously genuine work
    # answered 6-7h late. Retiring those would have turned a false-page bug into a
    # silently-dropped-task bug.
    #
    # `open_at_dispatch` is the QUIET tier of the #1032 visibility pair: how many
    # rows this bot already had open when this one was minted. It is recorded
    # rather than spoken because it is true of 51% of dispatches — see the
    # two-tier note above. Recording it is what makes the usage gap MEASURABLE:
    # `open_at_dispatch > 0 AND supersedes == ""` is the population that should
    # shrink as the declaration habit lands, and without the field there is no
    # before to compare an after against. Digits by construction (validated to 0
    # on any non-numeric), so no escaping.
    #
    # Schema-uniform rows: task_id/workstream/claudron_hits/supersedes/
    # open_at_dispatch always emitted (empty = absent, matching the report
    # ledger's always-emit convention; every consumer treats "" as falsy).
    # claudron_hits is digits-or-empty by construction (the preflight parser
    # prints a count), so no escaping.
    printf '{"ts":"%s","manager":"%s","bot":"%s","task_id":"%s","workstream":"%s","task":"%s","dispatched_at":%s,"expected_by":%s,"claudron_hits":"%s","supersedes":"%s","open_at_dispatch":%s}\n' \
        "$ts" "$MANAGER" "$WORKER_SESSION" "$TASK_ID" "$(json_escape "$DISPATCH_WORKSTREAM")" "$safe_task" "$now_epoch" "$EXPECTED_BY_JSON" "$CLAUDRON_HITS" "$(json_escape "$DISPATCH_SUPERSEDES")" "$OPEN_AT_DISPATCH" >> "$LEDGER"
    rotate_jsonl_by_ts "$LEDGER"
}
with_lock "$LEDGER.lock" _append_ledger

# Send via the low-level race-safe primitive (re-validates the session).
"$LIB_DIR/dispatch.sh" "$WORKER_SESSION" "$DISPATCH_MSG"
