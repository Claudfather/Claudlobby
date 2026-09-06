#!/bin/bash
# Manager dispatch wrapper — record the dispatch on the plane, then send.
# Usage: dispatch-task.sh [flags] <worker-session> <task...>
#
# Flags:
#   --deadline-min N   Override default deadline (minutes). 0 = open-ended:
#                      no expected_by is minted, so the row never goes overdue.
#   --repo NAME        Target repo (adds repo:<NAME> to envelope)
#   --priority LEVEL   Priority level (adds priority:<LEVEL> to envelope)
#   --ref URL          Reference URL (adds ref:<URL> to envelope)
#   --workstream ID    Workstream this task advances (envelope + plane work item)
#   --supersedes ID    This dispatch REPLACES an earlier one; the named task id is
#                      retired rather than left to age out and page. Opt-in, and
#                      inert when omitted — see the record comment below.
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
# the dispatch row AND transmit it as `task:<id>` — join semantics live in
# dispatch-overdue.py (overdue_all docstring). Raw-text sends (no flags) stay
# id-less: an id recorded but never transmitted would guarantee a
# false-positive overdue, since the worker cannot echo what it never saw.
#
# NON-`task` ENVELOPE SENDS ARE ALSO ID-LESS (#1187), for the same reason read
# forward instead of back: a `query` is answered inline, so nothing it produces
# can be JOINED against an id — not that it produces nothing. It DOES file a
# terminal report, and saying otherwise was false against the protocol the bot
# actually follows: `worker-lifecycle` routes `query` to "skip to Step 8", and
# Step 8 IS the terminal `[BOTREPORT]`. `restart` reports terminally too.
#
# The distinction is load-bearing, not pedantic (#1190). Believing the report
# never arrives is what left the receive side unguarded: a terminal report
# carrying no id falls into report-back.sh's #835 resolver, which stamps the
# bot's OLDEST open id'd dispatch — so a peer note silently closed unrelated
# in-progress work as `completed`. The guard now lives in
# dispatch-overdue.py::open_task_id, which is where the evidence is; an id
# minted here would still be unclosable from the moment it is written. The envelope
# format and the tracking used to be the same decision — `--botcommand` alone
# minted — which meant a manager who wanted the fleet message format for a peer
# note got a permanently open row as a side effect. They are separate now.
# Every send still records a communication on the plane; only `task` records a
# work item + assignment with an id.
#
# The plane's assignment (its expected_by) is what the fleet-pulse watchdog
# reads to flag `overdue_dispatch` if no terminal [BOTREPORT]
# (completed|failed|blocked) arrives by expected_by.
# Manager identity is this bot's $BOT_ID; the deadline defaults to
# $OBSERVABILITY_DISPATCH_DEADLINE (composed into EVERY bot.conf since chunk
# M-A, #1481 -- 86400s / 24h when the fleet declares no `dispatch_deadline`)
# and can be overridden with --deadline-min. Either door takes 0 to mean
# open-ended. Sending itself reuses lib/dispatch.sh.
#
# Claudron query-before preflight (plan P1e, fork F7) — env-knobbed, off by
# default:
#   CLAUDRON_QUERY_BEFORE=1  enable the preflight
#   CLAUDRON_QUERY_LIMIT     max fleet-memory pointers to inject (default 3)
# When enabled and the claudron CLI + CLAUDRON_VAULT_PATH resolve, the task
# text gains a single-line "[fleet memory: <title> (<abs path>); ...]" prefix
# of lookup pointers (titles + paths only, never note bodies — the worker
# reads the files itself). claudron_hits in the dispatch row records how many
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
# built, so both enveloped and raw-text dispatches carry them and the dispatch row
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
    # WHY 30 AND NOT 200 -- the number is DERIVED from the scorer, not chosen,
    # because 30 looks far too small to a reader who has not done this
    # arithmetic. The first version of this cap was 200 and reproduced the very
    # collapse the block above describes, just less often.
    #
    # Claudron scores a multi-word query by SUMMING per-token scores with no
    # normalisation by token count, then clamping
    # (`claudron/knowledge.py:394-407`, `token_total += term_total` then
    # `min(token_total, SCORE_CAP)`). Verified in source at that version:
    #
    #     SCORE_CAP            = 200
    #     W_TITLE_EXACT        = 100   (strongest single match)
    #     W_TITLE_WORD_OVERLAP =  50   (per token, multi-word path)
    #     W_BODY               =  20   (WEAKEST match that still scores)
    #
    # The ceiling is therefore reachable by ACCUMULATION alone: 10 tokens that
    # merely appear in a note's BODY sum to 10 x 20 = 200 = SCORE_CAP. A note
    # needs no topical relevance whatsoever, only ten shared common words.
    #
    # 200 chars is roughly 30 tokens, so EVERY sufficiently long note ties at
    # the cap at once. Ties do not rank, so selection falls through to
    # maturity/tier/insertion order -- a property of the VAULT, not of the
    # query. Measured estate-wide before this change: 482 dispatch headers
    # producing only 23 distinct pointer sets, 132 of 140 identical on one day.
    #
    # 30 chars is ~5 tokens of realistic prose, whose maximum achievable score
    # via the BODY path is 5 x 20 = 100 -- strictly below SCORE_CAP. That is the
    # property being bought: a note can no longer reach the ceiling on
    # incidental body matches, which needs ten tokens and is what 200 chars
    # (~30 tokens) handed to every long note at once.
    #
    # IT DOES NOT ELIMINATE TIES, and the honest bound is worth stating because
    # 30 invites the belief that it does. The TITLE-OVERLAP path pays 50 per
    # token, so FOUR tokens reach 200 -- about 24 characters. 30 chars sits
    # ABOVE that, so a query whose every word hits titles still saturates.
    # Measured on the live vault, same subject truncated to each width:
    #
    #     cap=200 (14 tok)  200 200 200   <- collapse
    #     cap=40  ( 6 tok)  200 200 200
    #     cap=30  ( 5 tok)  200 200 200   <- still flat on THIS query
    #     cap=24  ( 4 tok)  200 200 200   <- 4 x 50, the title-path threshold
    #     cap=18  ( 3 tok)  200 200 160   <- spread appears
    #     cap=12  ( 2 tok)  120 120 120   <- below the ceiling, but too little
    #                                        signal to rank on either
    #
    # So 30 fixes body-accumulation, not title saturation. That is still the
    # right trade: a note tying at the ceiling because every query word is in
    # its TITLE is a genuine topical match, which is not the defect -- the
    # defect was ten common words in a BODY. Going lower buys title-path spread
    # and starts costing subject matter (12 chars ranks on "restore the").
    # Anything below ~18 needs its own measurement, not an extrapolation of
    # this one.
    #
    # A/B on real ledger dispatches before the change landed: subject-bearing
    # heads (80% of traffic) -- 2 of 3 went from three unrelated notes to correct
    # ones. Attribution-prefixed heads (16%) -- relevance NOT restored, but score
    # SPREAD was (130/80 against a flat 200). Both outcomes are wanted: spread is
    # what lets a caller detect a degenerate set and emit nothing at all.
    #
    # So the cap is bounded ABOVE by the scorer and below by needing enough
    # subject to rank on. Raising it back toward 200 re-buys the collapse;
    # Claudron#143 fixes the scorer, and this ceiling can be revisited then --
    # but not before, and not without redoing this arithmetic.
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
    query=$(printf '%s' "$subject" | cut -c1-"${CLAUDRON_QUERY_MAX_CHARS:-30}")
    raw=$(claudron lookup --json \
        --limit "${CLAUDRON_QUERY_LIMIT:-3}" "$query" 2>/dev/null) || return 0
    # Emits "<count>\t<title (abs path); ...>". Claudron-supplied strings are
    # sanitized to printable-by-construction before use: pipes become "/"
    # (the [BOTCOMMAND] envelope is pipe-delimited) and runs of whitespace OR
    # control bytes collapse to single spaces — an embedded newline would
    # split the single-line dispatch row into invalid JSON that line-oriented
    # rotation then truncates, and a non-whitespace control (a YAML "\e" in a
    # note title reaches here as a raw ESC) would record bytes the worker
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

# The deadline, in SECONDS (--deadline-min is the one door in minutes). The
# composer writes OBSERVABILITY_DISPATCH_DEADLINE into every bot.conf since
# chunk M-A (#1481) -- 86400 when the fleet declares none -- so this literal
# is only ever reached by a bot.conf composed before that, or by a hand run
# with no bot.conf. It matches the composed default rather than the old 1800
# so the two can never disagree about the same fact.
#
# ZERO DISABLES, on either door: an open-ended dispatch mints no expected_by
# at all, the same `null` a control dispatch carries. Not "now + 0", which
# would be overdue in the second it was sent -- the loudest possible reading
# of "no deadline please".
#
# A NON-INTEGER IS REFUSED HERE, LOUDLY (the M-A fold, F8). The guard used to
# leave one alone "so the arithmetic below discloses it" -- and the arithmetic
# does not: `$(( abc * 60 ))` under `set -u` is an unbound-variable fault
# inside the ERR trap, which exited 0 having sent nothing and recorded
# nothing. A typo on either door (`--deadline-min abc`, or a bot.conf carrying
# `OBSERVABILITY_DISPATCH_DEADLINE=abc`) silently dropped the whole dispatch.
# The glob is `*[!0-9]*` and NOT `*[!0-9-]*`: allowing `-` let `12-34` through
# the class test and into `[ "$X" -le 0 ]`, which is itself an error, and a
# NEGATIVE deadline is not a thing anyway -- `0` is the door for open-ended.
_reject_non_integer() {
    case "$2" in
        ''|*[!0-9]*)
            echo "dispatch-task: $1 must be a non-negative integer, got '$2' (0 = open-ended)" >&2
            exit 1 ;;
    esac
}
if [ -n "$DEADLINE_MIN" ]; then
    _reject_non_integer --deadline-min "$DEADLINE_MIN"
    DEADLINE_S=$(( DEADLINE_MIN * 60 ))
else
    DEADLINE_S="${OBSERVABILITY_DISPATCH_DEADLINE:-86400}"
    _reject_non_integer OBSERVABILITY_DISPATCH_DEADLINE "$DEADLINE_S"
fi
if [ "$DEADLINE_S" -le 0 ]; then
    EXPECTED_BY_JSON="null"
fi

MANAGER="${BOT_ID:-${BOT_NAME:-unknown}}"
CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$(cd "$LIB_DIR/.." && pwd)}"

now_epoch=$(date +%s)
expected_by=$(( now_epoch + DEADLINE_S ))
# Empty unless the envelope branch above withheld the deadline for a non-`task`
# type. Resolved here rather than there because the deadline is not computed
# until now, and a `null` set earlier must survive this assignment.
[ -n "$EXPECTED_BY_JSON" ] || EXPECTED_BY_JSON="$expected_by"
ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Escape backslash + double-quote for valid JSON (no jq dependency).
safe_task=$(json_escape "$TASK")

# --- the plane record (PR-B T4; phase-2 plan §3/§6b; F18 closure R1) ----------
# The plane is the ONLY record of a dispatch: PLANE_EMIT_DISABLED=1 (the harness
# exemption) is the one thing that silences it. Every plane failure is disclosed
# on stderr and NEVER blocks the dispatch — the send is the mission — but an
# unrecorded dispatch is said LOUDLY, because there is no other record.
# Construct ids are minted HERE; report-back joins through the plane's own
# source_ref (dispatch-log:<task_id>).
PLANE_ARMED=0
if plane_armed dispatch-task --require-fleet; then
    PLANE_ARMED=1
fi
PLANE_MSG_ID="" PLANE_WI_ID="" PLANE_ASG_ID=""
# --supersedes → the plane (cutover chunk 1): look the superseded dispatch up
# BY ITS LEGACY TASK ID (source_ref), set supersedes_msg_id on this dispatch's
# communication, and emit a terminal `superseded` task event on the retired
# assignment (successor_id = this assignment). Until now --supersedes was
# JSONL-only (14 of 189 closed rows historically), so the plane's "open" set
# over-reported. Not found in the plane → no wiring, disclosed.
SUP_WI="" SUP_ASG="" SUP_MSG="" sup_frag="" sup_ev=""
if [ "$PLANE_ARMED" = "1" ]; then
    # Cutover chunk 6a: EVERY dispatch mints the construct triple, id-less ones
    # too (query / cancel / compact / restart) -- the plane must hold every
    # dispatch the legacy ledger holds, or the flipped readers cannot see an
    # overdue id-less dispatch nor apply the resolver id-less guard (#1418).
    PLANE_MSG_ID="$(plane_mint_id msg)"
    PLANE_WI_ID="$(plane_mint_id wi)"
    PLANE_ASG_ID="$(plane_mint_id asg)"
fi

# Recipient context, fail-open: the alias needs the RECIPIENT fleet (§6b #4 —
# envelope fleet is the SENDER, the recipient fleet rides the alias; 44.6% of
# dispatch traffic is cross-fleet), and carrier_queued needs the busy probe.
# Any resolution failure degrades to recipient_raw-only and not-busy.
PLANE_PEER_FLEET="" PLANE_PEER_BUSY=0
_plane_peer_context() {
    local session="$1" peer_dir=""
    peer_dir="$(fleet_runtime_dir 2>/dev/null)/bots/$session"
    [ -d "$peer_dir" ] || peer_dir=$(_resolve_cross_fleet_bot_dir "$session" 2>/dev/null || true)
    [ -n "$peer_dir" ] && [ -d "$peer_dir" ] || return 0
    # Peer fleet from the peer's OWN bot.conf first (#1372 review F7): parsing
    # the path component after /local/ attributed nested vault layouts
    # (local/<system>/<fleet>/runtime/bots — this estate's ACTUAL shape) to
    # the outer container. Fallback derives the component immediately before
    # /runtime/bots, which is fleet-named in both flat and nested layouts.
    PLANE_PEER_FLEET="$(bot_conf_get "$peer_dir" FLEET_NAME "" 2>/dev/null || true)"
    if [ -z "$PLANE_PEER_FLEET" ]; then
        case "$peer_dir" in
            */runtime/bots/*)
                PLANE_PEER_FLEET="$(basename "${peer_dir%/runtime/bots/*}")" ;;
            *) PLANE_PEER_FLEET="${FLEET_NAME:-}" ;;
        esac
    fi
    # Busy probe through the SUPPORTED doors (#1372 review F6): the socket
    # comes from tmux_socket_for_bot (bare bot_conf_get BOT_SERVICE misses the
    # fleet-scoped refusal semantics) and busyness from bot_is_busy — the
    # lib-common SSOT whose signature is (socket, session). pane_is_busy takes
    # pane TEXT; called with a socket it always read not-busy, so a busy pane
    # emitted pane_submitted instead of carrier_queued.
    local sock sess
    sock=$(tmux_socket_for_bot "$peer_dir" 2>/dev/null || true)
    sess=$(basename "$peer_dir")
    # peer_dir rides as the third arg (gauntlet round): without it,
    # bot_is_busy re-runs the whole session->dir resolution this function
    # just did — including a second estate-wide cross-fleet sweep for the
    # 44.6% of dispatches that are cross-fleet.
    if [ -n "$sock" ] && bot_is_busy "$sock" "$sess" "$peer_dir" 2>/dev/null; then
        PLANE_PEER_BUSY=1
    fi
    return 0
}
[ "$PLANE_ARMED" = "1" ] && { _plane_peer_context "$WORKER_SESSION" || true; }

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
    # ONE run answers both tiers: line 1 is the open-row count (the quiet
    # tier), the rest the note (the loud tier). Asked AFTER the peer context
    # and for the WORKER's fleet: the sender's roster does not hold a
    # cross-fleet worker (44.6% of dispatches), so the hint read 0 open
    # there and could never speak (the structural lens, F18 R2a).
    _dt_out=$(python3 "$LIB_DIR/dispatch-supersede-hint.py" \
        --bot "$WORKER_SESSION" --task "$TASK" --ref "$DISPATCH_REF" \
        --fleet "${PLANE_PEER_FLEET:-${FLEET_NAME:-}}" 2>/dev/null || true)
    OPEN_AT_DISPATCH=$(printf '%s\n' "$_dt_out" | head -n 1)
    case "$OPEN_AT_DISPATCH" in ''|*[!0-9]*) OPEN_AT_DISPATCH=0 ;; esac
    _dt_hint=$(printf '%s\n' "$_dt_out" | tail -n +2)
    # Only the loud tier is printed, and only when the caller has NOT declared.
    if [ -z "$DISPATCH_SUPERSEDES" ] && [ -n "$_dt_hint" ]; then
        printf '%s\n' "$_dt_hint" >&2
    fi
fi

_plane_emit_intent() {
    # Intent BEFORE transport (F9): the communication (and for id'd tasks the
    # work_item + assignment) exists before the first send attempt.
    # Distinct values are escaped ONCE into safe_* locals (gauntlet round,
    # measured): each json_escape is a fork-pipeline ~8-12ms on the Pi, and
    # the fleet/sender/worker values used to be escaped 3-4x per emission —
    # 50-100ms of the 200ms door budget.
    local safe_msg safe_fleet safe_sender safe_worker
    local sender_alias recip_alias recip_field cmd_type msg_class src_ref
    safe_msg=$(json_escape "$DISPATCH_MSG")
    safe_fleet=$(json_escape "$FLEET_NAME")
    sender_alias="bot:$FLEET_NAME/$MANAGER"
    safe_sender=$(json_escape "$sender_alias")
    safe_worker=$(json_escape "$WORKER_SESSION")
    recip_field=""
    if [ -n "$PLANE_PEER_FLEET" ]; then
        recip_alias="bot:$PLANE_PEER_FLEET/$WORKER_SESSION"
        recip_field="\"recipient\":\"$(json_escape "$recip_alias")\","
    fi
    # message_class by what the send ASKS FOR (door ruling, PR-B): task and
    # freeform ask for work (task_request); query asks for an answer
    # (question); cancel/compact/restart are control verbs (raw_control).
    # command_type is DOOR-FLAG provenance only (§6b #5) — never parsed from
    # the payload text; freeform carries none.
    cmd_type=""
    msg_class="task_request"
    if [ "$FORCE_ENVELOPE" = "1" ]; then
        cmd_type="\"command_type\":\"$DISPATCH_TYPE\","
        case "$DISPATCH_TYPE" in
            query) msg_class="question" ;;
            cancel|compact|restart) msg_class="raw_control" ;;
        esac
    fi
    # Fragments built via if/else, never inline `$([ ... ] && ...)` — a false
    # test inside a command substitution returns rc 1 into the assignment and
    # errexit kills the door.
    # The dispatch ref: the legacy task id, or the importer content key of the
    # dispatch row for an id-less dispatch (cutover chunk 6a) -- one ref for the
    # live door and any later import, so they classify as one fact.
    local dispatch_ref
    if [ -n "$TASK_ID" ]; then
        dispatch_ref="dispatch-log:$TASK_ID"
    else
        local _key
        _key=$(sha256_hex32 "$DISPATCH_RECORD" 2>/dev/null || true)
        if [ -z "$_key" ]; then
            # No sha tool on this host: disclose, and emit the communication only
            # rather than mint a malformed ref (unreachable on Linux/macOS).
            echo "dispatch-task: no sha256 tool -- id-less dispatch emitted as a communication only" >&2
            dispatch_ref=""
        else
            dispatch_ref="dispatch-log:sha:$_key"
        fi
    fi
    src_ref=""
    [ -n "$dispatch_ref" ] && src_ref="\"source_ref\":\"$dispatch_ref\","
    if [ -n "$DISPATCH_SUPERSEDES" ] && [ -n "$PLANE_ASG_ID" ]; then
        # scoped to THIS worker (#518's rule, which the legacy join carried and
        # the first plane build dropped): a same-named id held by another bot
        # is never the one this dispatch retires
        _sup=$(python3 -S -E "$LIB_DIR/plane-lookup.py" --root "${CLAUDLOBBY_ROOT:-}" \
            --task-id "$DISPATCH_SUPERSEDES" \
            --assignee "bot:${PLANE_PEER_FLEET:-$FLEET_NAME}/$WORKER_SESSION" 2>/dev/null || true)
        if [ -n "$_sup" ]; then
            SUP_WI=${_sup%% *}; _sup=${_sup#* }; SUP_ASG=${_sup%% *}; SUP_MSG=${_sup##* }
            [ -n "$SUP_MSG" ] && [ "$SUP_MSG" != "$SUP_ASG" ] && sup_frag="\"supersedes_msg_id\":\"$SUP_MSG\","
            # source_ref names THIS dispatch (the successor): the supersession is a
            # fact this row created; the retired row is named by assignment_id.
            sup_ev="{\"event_type\":\"task\",\"emitter\":\"dispatch-task\",\"source_ref\":\"$dispatch_ref\",\"fleet\":\"$safe_fleet\",\"payload\":{\"work_item_id\":\"$SUP_WI\",\"assignment_id\":\"$SUP_ASG\",\"event\":\"superseded\",\"successor_id\":\"$PLANE_ASG_ID\"}}"
        else
            echo "dispatch-task: --supersedes $DISPATCH_SUPERSEDES not found in the plane — legacy-only supersession" >&2
        fi
    fi
    local link_frag="" ws_frag="" repo_frag="" deadline_frag="" iso_deadline=""
    if [ -n "$PLANE_WI_ID" ]; then
        link_frag="\"work_item_id\":\"$PLANE_WI_ID\",\"assignment_id\":\"$PLANE_ASG_ID\","
    fi
    local comm wi_ev asg_ev
    comm="{\"event_type\":\"communication\",\"emitter\":\"dispatch-task\",$src_ref\"fleet\":\"$safe_fleet\",\"payload\":{\"msg_id\":\"$PLANE_MSG_ID\",${sup_frag}\"sender\":\"$safe_sender\",${recip_field}\"recipient_raw\":\"$safe_worker\",\"message_class\":\"$msg_class\",${cmd_type}${link_frag}\"body\":\"$safe_msg\"}}"
    if [ -n "$dispatch_ref" ]; then
        # The plane's deadline MIRRORS the dispatch row's: a control dispatch (query /
        # cancel / compact / restart) withholds it on both sides, for the reason
        # written above the null — an id-less row with a deadline goes overdue
        # and names nothing. The first 6a build stamped it on the plane only, so
        # the plane read "overdue" on queries the row deliberately kept silent
        # and the shadow paged on every one of them.
        if [ "$EXPECTED_BY_JSON" != "null" ]; then
            iso_deadline=$(epoch_to_iso_utc "$expected_by" || true)
            [ -n "$iso_deadline" ] && deadline_frag=",\"expected_by\":\"$iso_deadline\""
        fi
        [ -n "$DISPATCH_WORKSTREAM" ] && ws_frag=",\"workstream_id\":\"$(json_escape "$DISPATCH_WORKSTREAM")\""
        case "$DISPATCH_REPO" in
            */*) repo_frag=",\"repo\":\"$(json_escape "$DISPATCH_REPO")\"" ;;
        esac
        wi_ev="{\"event_type\":\"work_item\",\"emitter\":\"dispatch-task\",\"source_ref\":\"$dispatch_ref\",\"fleet\":\"$safe_fleet\",\"payload\":{\"work_item_id\":\"$PLANE_WI_ID\",\"title\":\"$safe_task\",\"created_by\":\"$safe_sender\"${ws_frag}${repo_frag}}}"
        asg_ev="{\"event_type\":\"assignment\",\"emitter\":\"dispatch-task\",\"source_ref\":\"$dispatch_ref\",\"fleet\":\"$safe_fleet\",\"payload\":{\"assignment_id\":\"$PLANE_ASG_ID\",\"work_item_id\":\"$PLANE_WI_ID\",\"assignee\":\"$(json_escape "bot:${PLANE_PEER_FLEET:-$FLEET_NAME}/$WORKER_SESSION")\",\"assigned_by\":\"$safe_sender\"${deadline_frag},\"dispatch_msg_id\":\"$PLANE_MSG_ID\"}}"
        local _batch
        printf -v _batch '{"events":[%s,%s,%s%s]}' "$wi_ev" "$asg_ev" "$comm" "${sup_ev:+,$sup_ev}"
        plane_emit_events dispatch-task <<<"$_batch"       # same shell: PLANE_EMIT_LAST_RC reaches the record decision
    else
        local _batch
        printf -v _batch '{"events":[%s]}' "$comm"
        plane_emit_events dispatch-task <<<"$_batch"
    fi
}

_plane_emit_transmission() {
    # Outcome-typed AFTER the send (§6b #7): a clean send into a BUSY pane is
    # carrier_queued (accepted-not-consumed, not activation); a clean send
    # into an idle pane is pane_submitted; a miss is failed.
    local state="$1"
    printf '{"events":[%s]}' \
        "$(plane_tx_event dispatch-task "$FLEET_NAME" tmux "$PLANE_MSG_ID" "$WORKER_SESSION" "$state")" \
        | plane_emit_events dispatch-task
}

# The dispatch row exactly as the retired ledger wrote it, composed ONCE
# (printf -v: no fork) and kept for its CONTENT KEY alone: an id-less
# dispatch's plane ref is dispatch-log:sha:<key> of this text (the importer's
# derivation for an unstamped row), so the ref stayed stable across the F18
# closure. Nothing writes it anywhere.
printf -v DISPATCH_RECORD '{"ts":"%s","manager":"%s","bot":"%s","task_id":"%s","workstream":"%s","task":"%s","dispatched_at":%s,"expected_by":%s,"claudron_hits":"%s","supersedes":"%s","open_at_dispatch":%s,"plane_msg_id":"%s","plane_work_item_id":"%s","plane_assignment_id":"%s"}' \
        "$ts" "$MANAGER" "$WORKER_SESSION" "$TASK_ID" "$(json_escape "$DISPATCH_WORKSTREAM")" "$safe_task" "$now_epoch" "$EXPECTED_BY_JSON" "$CLAUDRON_HITS" "$(json_escape "$DISPATCH_SUPERSEDES")" "$OPEN_AT_DISPATCH" "$PLANE_MSG_ID" "$PLANE_WI_ID" "$PLANE_ASG_ID"
# Plane intent BEFORE transport (F9): a crash between here and the send leaves
# an intent with no transmission, visible, and exactly what reconciliation
# exists to surface. An emission the shim could not record is DISCLOSED loudly
# and the send proceeds: the send is the mission, and there is no other record.
if [ "$PLANE_ARMED" = "1" ]; then
    _plane_emit_intent || true
    if [ "${PLANE_EMIT_LAST_RC:-0}" -ne 0 ]; then
        echo "dispatch-task: the plane did NOT record this dispatch (rc=$PLANE_EMIT_LAST_RC) -- sending anyway; there is no other record" >&2
    fi
fi

# Send via the low-level race-safe primitive (re-validates the session).
send_rc=0
"$LIB_DIR/dispatch.sh" "$WORKER_SESSION" "$DISPATCH_MSG" || send_rc=$?

# Outcome-typed transmission (PR-B T4/§6b #7): clean send into an idle pane =
# pane_submitted; clean send into a pane the pre-send probe saw BUSY =
# carrier_queued (the TUI accepted it, a running turn parked it — not
# activation); a miss = failed.
if [ "$PLANE_ARMED" = "1" ]; then
    if [ "$send_rc" -ne 0 ]; then
        _plane_emit_transmission "failed" || true
    elif [ "$PLANE_PEER_BUSY" = "1" ]; then
        _plane_emit_transmission "carrier_queued" || true
    else
        _plane_emit_transmission "pane_submitted" || true
    fi
fi
exit "$send_rc"
