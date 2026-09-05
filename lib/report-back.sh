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

# Explicit arg guard FIRST (before any resolution work) — NOT bare $1/$2/$3
# under set -u: macOS bash 3.2 exits 0 through lib-common's EXIT trap (its
# `|| true` cleanup tail resets the reported status), masking usage errors.
if [ $# -lt 3 ]; then
    echo "Usage: report-back.sh <bot> <status> <summary> [extras...]" >&2
    exit 1
fi
BOT="$1"
STATUS="$2"
SUMMARY="$3"
shift 3

MANAGER_SESSION="${MANAGER_TMUX:-claude-bot}"
# The manager's private tmux server socket: prefer the composed field, else
# reverse-look-up from its session name among the sibling bots.
MANAGER_SOCKET="$(resolve_peer_socket "${MANAGER_TMUX_SOCKET:-}" "$MANAGER_SESSION")"

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
TASK_ID=""
# Set when a SUPPLIED --task is not in the bot's open set (#1032). Recorded,
# never acted on: the report still carries the id the caller gave.
TASK_ANOMALY=""
POSITIONAL_EXTRAS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --progress)
            # Integer 0-100 ONLY (#1372 review F2): this value is interpolated
            # into JSON — legacy ledger AND plane batch — so a free-form value
            # was a JSON injection seam (a crafted --progress forged duplicate
            # keys that redirected the report's task facts to arbitrary ids).
            case "$2" in
                ''|*[!0-9]*) echo "report-back: --progress must be an integer 0-100, got '$2'" >&2; exit 2 ;;
            esac
            # Length cap BEFORE arithmetic (gauntlet round, probed): a
            # 20-digit all-numeric value passed the digit gate, wrapped
            # $((10#...)) to a negative 64-bit, passed -le 100, and the
            # pydantic ge=0 refusal then dropped the report's communication
            # AND task fact from the plane. 0-100 needs at most 3 digits.
            [ ${#2} -le 3 ] || { echo "report-back: --progress must be an integer 0-100, got '$2'" >&2; exit 2; }
            # Canonicalize (10#) — '01' passed the digit gate but is invalid
            # JSON (#1372 re-verify F2 residual: an armed linked report landed
            # zero plane rows on a leading-zero value).
            PROGRESS=$(( 10#$2 ))
            [ "$PROGRESS" -le 100 ] || { echo "report-back: --progress must be <= 100, got '$2'" >&2; exit 2; }
            shift 2 ;;
        --artifact)  ARTIFACTS="${ARTIFACTS:+$ARTIFACTS,}$2"; shift 2 ;;
        --pr)        POSITIONAL_EXTRAS+=("pr:$2"); shift 2 ;;
        --issues)    POSITIONAL_EXTRAS+=("issues:$2"); shift 2 ;;
        --skill)     POSITIONAL_EXTRAS+=("skill:$2"); shift 2 ;;
        --task)      TASK_ID="$2"; shift 2 ;;
        *)           POSITIONAL_EXTRAS+=("$1"); shift ;;
    esac
done


# Resolve the dispatch this report closes when the caller omitted --task (#835).
# An id'd dispatch closes ONLY on a terminal report echoing the same id, and
# workers routinely omit it — so id'd dispatches stayed open until they aged out
# and the watchdog cried wolf over finished work. Correct by default rather than
# by discipline: the worker no longer has to remember.
#
# Terminal statuses only: a progress report closes nothing, so resolving one
# would spend the lookup to stamp an id no consumer reads.
#
# This does NOT loosen the watchdog's join (that would be the #447 blanket-close
# bug). dispatch-overdue.py owns "which dispatch is open" for both readers, so
# the id supplied here is one this bot genuinely has open — and the watchdog
# still verifies it independently. Fail-open at every step: a missing python3, an
# unreachable plane, or nothing open all leave TASK_ID empty and the report
# behaves exactly as it did before. A report-back must never fail because a
# watchdog helper was unavailable.
case "$STATUS" in
    completed|failed|blocked)
        if [ -z "$TASK_ID" ] && command -v python3 >/dev/null 2>&1; then
            # The resolver reads the plane of this fleet (F18 R2a) — no ledger
            # paths, no file-exists gate (a gate on the retired file once made
            # the resolver dead on a host whose files were gone).
            TASK_ID="$(python3 "$LIB_DIR/dispatch-overdue.py" --open-task "$BOT" 2>/dev/null || true)"
        # A SUPPLIED id is checked but NEVER changed (#1032). The resolver above
        # runs only when --task is omitted, so a wrong id was accepted verbatim
        # where an absent one would have been repaired — the auto-resolver was
        # bypassed by exactly the input it is most needed for. Measured: 18 of 43
        # mispaired rows carried an id minted BEFORE the dispatch they stranded.
        #
        # VISIBLE, NOT CORRECTED, and that boundary is the whole point. Oldest-
        # open-first would have produced the SAME wrong pairing in the filed
        # instance, because the mis-pairing is FIFO-shaped — so "resolve it for
        # them" is not merely risky here, it is measurably no better. Blanket-
        # closing older rows is #447. A tool that silently picks a row is worse
        # than one that says it cannot tell: the first sends nobody to look.
        elif [ -n "$TASK_ID" ] && command -v python3 >/dev/null 2>&1; then
            _rb_open="$(python3 "$LIB_DIR/dispatch-overdue.py" --open "$BOT" 2>/dev/null \
                | awk '{print $3}' || true)"
            # Only a NON-EMPTY open set can contradict the caller. An empty
            # one means the bot has nothing open — the plane unreachable, the
            # row already closed, or this is an unsolicited report — none of
            # which is evidence the id is wrong. Fail open: absence of
            # evidence must not become evidence of absence (#1146).
            if [ -n "$_rb_open" ] && ! printf '%s\n' "$_rb_open" \
                    | grep -qxF "$TASK_ID"; then
                TASK_ANOMALY="supplied-id-not-open"
                printf 'report-back: --task %s is not open for %s; reporting it unchanged.\n' \
                    "$TASK_ID" "$BOT" >&2
                printf 'report-back: open now: %s\n' \
                    "$(printf '%s' "$_rb_open" | tr '\n' ' ')" >&2
            fi
        fi
        ;;
esac

EXTRAS=""
[ -n "$PROGRESS" ] && EXTRAS="$EXTRAS | progress:$PROGRESS"
for arg in "${POSITIONAL_EXTRAS[@]+"${POSITIONAL_EXTRAS[@]}"}"; do
    EXTRAS="$EXTRAS | $arg"
done
[ -n "$ARTIFACTS" ] && EXTRAS="$EXTRAS | artifact:$ARTIFACTS"
# Echo the dispatch envelope's task id — the overdue watchdog's join key
# (semantics: overdue_all in dispatch-overdue.py).
[ -n "$TASK_ID" ] && EXTRAS="$EXTRAS | task:$TASK_ID"

MESSAGE="[BOTREPORT] $BOT | $STATUS | $SUMMARY$EXTRAS"

# --- the plane record (PR-B T5; phase-2 plan §3/§6b; F18 closure R1) ----------
# Same arming contract as dispatch-task: the plane is the ONLY record;
# PLANE_EMIT_DISABLED=1 (the harness exemption) silences it; every failure
# disclosed, never blocking — and an unrecorded report is said LOUDLY, there
# being no other record. The plane record is intent-FIRST (F9): the report
# communication (+ its task facts, one atomic batch) exists before the send —
# a crash between intent and send leaves a visible intent-without-transmission.
PLANE_ARMED=0
if plane_armed report-back --require-fleet; then
    PLANE_ARMED=1
fi
PLANE_MSG_ID="" PLANE_LINK_WI="" PLANE_LINK_ASG=""
_plane_lookup_dispatch_ids() {
    # The join dispatch-task recorded: the plane assignment carrying this task
    # id (source_ref dispatch-log:<id>) holds the construct ids. Fail-open — an
    # unlinked report simply carries no task facts, disclosed by its own row.
    # Join hardening (#1372 re-verify blocking residual on F3, tightened in
    # the gauntlet round). Grammar gates BEFORE any grep, because grep -F
    # treats a NEWLINE in the pattern as pattern-OR — a task id carrying
    # '\n"bot":"x"' matched and linked an unrelated assignment:
    #   1. TASK_ID must match the minted grammar exactly (t-<epoch>-<hex4>).
    #      A supplied id is machine-minted; anything else skips the link,
    #      fail-open — the report still lands, just unlinked.
    #   2. BOT must be a plain token (the session-name alphabet).
    # WHOLE-STRING bash regex, pattern-in-variable (the 3.2-safe idiom) —
    # the prior case-glob + grep pair both checked per-LINE: the glob star
    # spans newlines and grep-on-stdin anchors each line, so
    # 't-1-abcd<newline>junk' passed BOTH gates and reached the join as
    # pattern-OR, the exact hazard the gates exist for (measured on 3.2.57).
    # The bot match below is CASE-INSENSITIVE, matching dispatch-overdue.py's
    # join semantics for real this time (the prior claim was wrong: a forged
    # lowercase 'w1' row outranked the legitimate 'W1' one).
    local _task_pat='^t-[0-9]+-[0-9a-f]{4}$' _bot_pat='^[A-Za-z0-9._-]+$'
    [[ "$TASK_ID" =~ $_task_pat ]] || return 0
    [[ "$BOT" =~ $_bot_pat ]] || return 0
    local _ids
    _ids=$(python3 -S -E "$(dirname "${BASH_SOURCE[0]}")/plane-lookup.py" \
        --root "${CLAUDLOBBY_ROOT:-}" --task-id "$TASK_ID" \
        --assignee "bot:${FLEET_NAME:-}/$BOT" 2>/dev/null || true)
    if [ -n "$_ids" ]; then
        PLANE_LINK_WI=${_ids%% *}; _ids=${_ids#* }; PLANE_LINK_ASG=${_ids%% *}
    fi
    return 0
}
_plane_session_uid() {
    # Published by the SessionStart hook (plane-session-start.sh, T7) — the
    # transcript identity task facts carry. Absent file = hook unarmed or a
    # pre-hook session; the fact is simply unattributed, never guessed.
    local f="${BOT_DIR:-}/data/.plane-session"
    [ -n "${BOT_DIR:-}" ] && [ -f "$f" ] || return 0
    sed -n 's/.*"session_uid":"\(sess_[0-9a-f]*\)".*/\1/p' "$f" | head -1
}
_plane_emit_report_intent() {
    PLANE_MSG_ID="$(plane_mint_id msg)"
    [ -n "$TASK_ID" ] && { _plane_lookup_dispatch_ids || true; }
    local sess_frag="" _sess
    _sess="$(_plane_session_uid || true)"
    [ -n "$_sess" ] && sess_frag=",\"session_uid\":\"$_sess\""
    # Distinct values escaped ONCE (gauntlet round): fleet and sender used to
    # be re-escaped per event across the comm + task + anomaly batch — each
    # json_escape a fork-pipeline on the Pi.
    local safe_msg safe_fleet safe_sender sender_alias link_frag="" pr_url="" ex
    safe_msg=$(json_escape "$MESSAGE")
    safe_fleet=$(json_escape "$FLEET_NAME")
    sender_alias="bot:$FLEET_NAME/$BOT"
    safe_sender=$(json_escape "$sender_alias")
    # The recipient alias carries the MANAGER's fleet, not the worker's
    # (gauntlet round): cross-fleet managers are a supported shape
    # (resolve_peer_socket's cross-fleet fallback), and stamping the worker's
    # own fleet materialized a phantom party row on the return leg of exactly
    # the traffic dispatch-task's peer resolver exists for.
    local mgr_fleet
    mgr_fleet="$(plane_peer_fleet "$MANAGER_SESSION" || true)"
    if [ -n "$PLANE_LINK_WI" ]; then
        link_frag="\"work_item_id\":\"$PLANE_LINK_WI\",\"assignment_id\":\"$PLANE_LINK_ASG\","
    fi
    for ex in "${POSITIONAL_EXTRAS[@]+"${POSITIONAL_EXTRAS[@]}"}"; do
        case "$ex" in pr:*) pr_url="${ex#pr:}" ;; esac
    done
    local comm events
    comm="{\"event_type\":\"communication\",\"emitter\":\"report-back\",\"source_ref\":\"report-back:$PLANE_MSG_ID\",\"fleet\":\"$safe_fleet\",\"payload\":{\"msg_id\":\"$PLANE_MSG_ID\",\"sender\":\"$safe_sender\",\"recipient\":\"$(json_escape "bot:${mgr_fleet:-$FLEET_NAME}/$MANAGER_SESSION")\",\"recipient_raw\":\"$(json_escape "$MANAGER_SESSION")\",\"message_class\":\"report\",${link_frag}\"body\":\"$safe_msg\"}}"
    events="$comm"
    if [ -n "$PLANE_LINK_WI" ]; then
        # Task facts ride the same atomic batch (F4). Status -> token per §8:
        # blocked reports are terminal-shaped on this estate (6/6 measured) ->
        # returned_blocked; the supplied-id anomaly is its own first-class
        # fact (§6b #6) alongside the report token, never instead of it.
        local ev="" frag=""
        case "$STATUS" in
            completed) ev="completed" ;;
            failed)    ev="failed" ;;
            blocked)   ev="returned_blocked" ;;
            progress)  ev="progress" ;;
        esac
        [ -n "$PROGRESS" ] && frag=",\"progress\":$PROGRESS"
        [ -n "$pr_url" ] && frag="$frag,\"pr_url\":\"$(json_escape "$pr_url")\""
        if [ -n "$ev" ]; then
            events="$events,{\"event_type\":\"task\",\"emitter\":\"report-back\",\"source_ref\":\"report-back:$PLANE_MSG_ID\",\"fleet\":\"$safe_fleet\",\"payload\":{\"work_item_id\":\"$PLANE_LINK_WI\",\"assignment_id\":\"$PLANE_LINK_ASG\",\"event\":\"$ev\",\"actor\":\"$safe_sender\",\"summary\":\"$(json_escape "$SUMMARY")\"$frag$sess_frag}}"
        fi
        if [ "$TASK_ANOMALY" = "supplied-id-not-open" ]; then
            events="$events,{\"event_type\":\"task\",\"emitter\":\"report-back\",\"source_ref\":\"report-back:$PLANE_MSG_ID\",\"fleet\":\"$safe_fleet\",\"payload\":{\"work_item_id\":\"$PLANE_LINK_WI\",\"assignment_id\":\"$PLANE_LINK_ASG\",\"event\":\"supplied_id_not_open\",\"actor\":\"$safe_sender\",\"summary\":\"$(json_escape "--task $TASK_ID was not in the open set at report time")\"$sess_frag}}"
        fi
    fi
    # EVERY terminal report — id-less, id'd, or naming an id the plane could
    # not link — answers every OPEN id-less dispatch of this bot (cutover 6a,
    # widened by F18 R2a): the legacy ledger closed an id-less row on the
    # bot's next terminal report of ANY kind, and the first plane build
    # closed them only for id-less reports, leaving an id'd report's rows
    # open (found by the matcher suite's port). Each gets the terminal task
    # event, so the resolver guard releases and the overdue reader stops
    # paging it. The worker did something terminal; the id-less rows it was
    # holding are answered.
    local _idless_ev="" _pairs _wi _asg
    case "$STATUS" in
        completed) _idless_ev="completed" ;;
        failed)    _idless_ev="failed" ;;
        blocked)   _idless_ev="returned_blocked" ;;
    esac
    if [ -n "$_idless_ev" ]; then
        _pairs=$(python3 -S -E "$(dirname "${BASH_SOURCE[0]}")/plane-lookup.py" \
            --root "${CLAUDLOBBY_ROOT:-}" --open-idless --fleet "${FLEET_NAME:-}" --bot "$BOT" \
            2>/dev/null || true)
        while read -r _wi _asg; do
            [ -n "$_asg" ] || continue
            events="$events,{\"event_type\":\"task\",\"emitter\":\"report-back\",\"source_ref\":\"report-back:$PLANE_MSG_ID\",\"fleet\":\"$safe_fleet\",\"payload\":{\"work_item_id\":\"$_wi\",\"assignment_id\":\"$_asg\",\"event\":\"$_idless_ev\",\"actor\":\"$safe_sender\",\"summary\":\"$(json_escape "$SUMMARY")\"${sess_frag:-}}}"
        done <<EOF_IDLESS
$_pairs
EOF_IDLESS
    fi
    # A report whose STATUS reached no task event (a terminal note that resolved
    # nothing, an id'd report the plane could not link, or a PROGRESS report
    # without a task id) still has a status two readers need — the idle-worker
    # check (terminal, never progress) and the overdue reader's progress grace
    # (F18 R2a: the legacy grace deferred on any progress report BY BOT, and a
    # progress report resolves no id, so without this marker a long task that
    # reports id-less progress paged as overdue at its deadline) — so it rides
    # the plane as a `report_status` system event on the bot's actor
    # (alias-resolved at ingest), under the same report-back:<msg> ref, never
    # beside a task event (one fact).
    case ",$events," in
        *'"event_type":"task"'*) ;;
        *)
            case "$STATUS" in
                completed|failed|blocked|progress)
                    events="$events,{\"event_type\":\"system\",\"emitter\":\"report-back\",\"source_ref\":\"report-back:$PLANE_MSG_ID\",\"fleet\":\"$safe_fleet\",\"payload\":{\"event\":\"report_status\",\"subject_kind\":\"actor\",\"subject\":\"$safe_sender\",\"data\":{\"status\":\"$STATUS\",\"msg_id\":\"$PLANE_MSG_ID\"}}}" ;;
            esac ;;
    esac
    local _batch
    printf -v _batch '{"events":[%s]}' "$events"
    plane_emit_events report-back <<<"$_batch"            # same shell: PLANE_EMIT_LAST_RC reaches the ledger decision
}
if [ "$PLANE_ARMED" = "1" ]; then
    _plane_emit_report_intent || true
    if [ "${PLANE_EMIT_LAST_RC:-0}" -ne 0 ]; then
        echo "report-back: the plane did NOT record this report (rc=$PLANE_EMIT_LAST_RC) -- sending anyway; there is no other record" >&2
    fi
fi

# Cross-socket send via the one safe primitive: prechecks the manager session on
# its socket, two-step send, and logs a send_miss (no silent drop) on a miss.
rb_send_rc=0
bot_tmux_send "$MANAGER_SOCKET" "$MANAGER_SESSION" "$MESSAGE" || rb_send_rc=$?

if [ "$PLANE_ARMED" = "1" ]; then
    _plane_state="pane_submitted"
    [ "$rb_send_rc" -ne 0 ] && _plane_state="failed"
    printf '{"events":[%s]}' \
        "$(plane_tx_event report-back "$FLEET_NAME" tmux "$PLANE_MSG_ID" "$MANAGER_SESSION" "$_plane_state")" \
        | plane_emit_events report-back || true
fi

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
