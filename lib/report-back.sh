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
            [ "$2" -le 100 ] || { echo "report-back: --progress must be <= 100, got '$2'" >&2; exit 2; }
            PROGRESS="$2"; shift 2 ;;
        --artifact)  ARTIFACTS="${ARTIFACTS:+$ARTIFACTS,}$2"; shift 2 ;;
        --pr)        POSITIONAL_EXTRAS+=("pr:$2"); shift 2 ;;
        --issues)    POSITIONAL_EXTRAS+=("issues:$2"); shift 2 ;;
        --skill)     POSITIONAL_EXTRAS+=("skill:$2"); shift 2 ;;
        --task)      TASK_ID="$2"; shift 2 ;;
        *)           POSITIONAL_EXTRAS+=("$1"); shift ;;
    esac
done

# The ledger this report is appended to, and that the watchdog joins against —
# resolved once, through the shared helpers, so the resolver below and the writer
# further down can never read and write different files.
REPORT_LEDGER="$(fleet_runtime_dir)/report-back.jsonl"

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
# unreadable ledger, or nothing open all leave TASK_ID empty and the report
# behaves exactly as it did before. A report-back must never fail because a
# watchdog helper was unavailable.
case "$STATUS" in
    completed|failed|blocked)
        if [ -z "$TASK_ID" ] && command -v python3 >/dev/null 2>&1; then
            _rb_dispatch="$(dispatch_ledger_path)"
            if [ -f "$_rb_dispatch" ]; then
                TASK_ID="$(python3 "$LIB_DIR/dispatch-overdue.py" --open-task \
                    "$BOT" "$_rb_dispatch" "$REPORT_LEDGER" 2>/dev/null || true)"
            fi
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
            _rb_dispatch="$(dispatch_ledger_path)"
            if [ -f "$_rb_dispatch" ]; then
                _rb_open="$(python3 "$LIB_DIR/dispatch-overdue.py" --open \
                    "$BOT" "$_rb_dispatch" "$REPORT_LEDGER" 2>/dev/null \
                    | awk '{print $3}' || true)"
                # Only a NON-EMPTY open set can contradict the caller. An empty
                # one means the bot has nothing open — the ledger is missing, the
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

# --- observable-plane dual-write (PR-B T5; phase-2 plan §3/§6b) ----------------
# Same arming contract as dispatch-task: dormant unless the fleet sets
# PLANE_EMIT_ENABLED=1; PLANE_EMIT_DISABLED=1 wins; every failure disclosed,
# never blocking — the legacy JSONL row below stays the load-bearing record in
# its EXISTING position. The plane record is intent-FIRST (F9): the report
# communication (+ its task facts, one atomic batch) exists before the send —
# the deliberate crash-exposure flip the phase-2 plan names as the canary
# sharp edge: a crash between intent and send now leaves a visible
# intent-without-transmission instead of a sent-report-without-ledger-row.
PLANE_ARMED=0
if [ "${PLANE_EMIT_ENABLED:-0}" = "1" ] && [ "${PLANE_EMIT_DISABLED:-0}" != "1" ]; then
    if [ -n "${FLEET_NAME:-}" ]; then
        PLANE_ARMED=1
    else
        echo "report-back: PLANE_EMIT_ENABLED but FLEET_NAME is empty — plane rows are fleet-scoped, skipping (report unaffected)" >&2
    fi
fi
PLANE_MSG_ID="" PLANE_LINK_WI="" PLANE_LINK_ASG=""
_plane_hex32() { od -An -tx1 -N16 /dev/urandom | tr -d ' \n'; }
_plane_emit() {
    # stderr passes through — the shim's fallback disclosure is the contract.
    # Phase-neutral wording (#1372 review F16): intent emits fire BEFORE the
    # send, so the message must not claim the action already happened.
    "$LIB_DIR/plane-emit.sh" >/dev/null || \
        echo "report-back: plane record failed rc=$? (report action unaffected — legacy ledger remains the record)" >&2
}
_plane_lookup_dispatch_ids() {
    # The join dispatch-task wrote for us: the newest ledger row carrying this
    # task id holds the plane construct ids. Fail-open — an unarmed-era or
    # pre-PR-B row has empty fields and the task facts are simply not linked.
    local dlog row
    dlog="$(dispatch_ledger_path)"
    [ -f "$dlog" ] || return 0
    # Match task id AND bot (#1372 review F3): the id-only grep linked a w1
    # report to w2's assignment when two dispatches shared an id — the join
    # key is the PAIR, exactly as dispatch-overdue.py joins it. grep -F on the
    # exact JSON fragments; json_escape guarantees task text cannot fabricate
    # either (quotes are escaped in the task field).
    row=$(grep -F "\"task_id\":\"$TASK_ID\"" "$dlog" 2>/dev/null \
        | grep -F "\"bot\":\"$BOT\"" | tail -1 || true)
    [ -n "$row" ] || return 0
    PLANE_LINK_WI=$(printf '%s' "$row" | sed -n 's/.*"plane_work_item_id":"\([a-z0-9_]*\)".*/\1/p')
    PLANE_LINK_ASG=$(printf '%s' "$row" | sed -n 's/.*"plane_assignment_id":"\([a-z0-9_]*\)".*/\1/p')
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
    PLANE_MSG_ID="msg_$(_plane_hex32)"
    [ -n "$TASK_ID" ] && { _plane_lookup_dispatch_ids || true; }
    local sess_frag="" _sess
    _sess="$(_plane_session_uid || true)"
    [ -n "$_sess" ] && sess_frag=",\"session_uid\":\"$_sess\""
    local safe_msg sender_alias link_frag="" pr_url="" ex
    safe_msg=$(json_escape "$MESSAGE")
    sender_alias="bot:$FLEET_NAME/$BOT"
    if [ -n "$PLANE_LINK_WI" ]; then
        link_frag="\"work_item_id\":\"$PLANE_LINK_WI\",\"assignment_id\":\"$PLANE_LINK_ASG\","
    fi
    for ex in "${POSITIONAL_EXTRAS[@]+"${POSITIONAL_EXTRAS[@]}"}"; do
        case "$ex" in pr:*) pr_url="${ex#pr:}" ;; esac
    done
    local comm events
    comm="{\"event_type\":\"communication\",\"emitter\":\"report-back\",\"source_ref\":\"report-back:$PLANE_MSG_ID\",\"fleet\":\"$(json_escape "$FLEET_NAME")\",\"payload\":{\"msg_id\":\"$PLANE_MSG_ID\",\"sender\":\"$(json_escape "$sender_alias")\",\"recipient\":\"$(json_escape "bot:$FLEET_NAME/$MANAGER_SESSION")\",\"recipient_raw\":\"$(json_escape "$MANAGER_SESSION")\",\"message_class\":\"report\",${link_frag}\"body\":\"$safe_msg\"}}"
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
            events="$events,{\"event_type\":\"task\",\"emitter\":\"report-back\",\"source_ref\":\"report-back:$PLANE_MSG_ID\",\"fleet\":\"$(json_escape "$FLEET_NAME")\",\"payload\":{\"work_item_id\":\"$PLANE_LINK_WI\",\"assignment_id\":\"$PLANE_LINK_ASG\",\"event\":\"$ev\",\"actor\":\"$(json_escape "$sender_alias")\",\"summary\":\"$(json_escape "$SUMMARY")\"$frag$sess_frag}}"
        fi
        if [ "$TASK_ANOMALY" = "supplied-id-not-open" ]; then
            events="$events,{\"event_type\":\"task\",\"emitter\":\"report-back\",\"source_ref\":\"report-back:$PLANE_MSG_ID\",\"fleet\":\"$(json_escape "$FLEET_NAME")\",\"payload\":{\"work_item_id\":\"$PLANE_LINK_WI\",\"assignment_id\":\"$PLANE_LINK_ASG\",\"event\":\"supplied_id_not_open\",\"actor\":\"$(json_escape "$sender_alias")\",\"summary\":\"$(json_escape "--task $TASK_ID was not in the open set at report time")\"$sess_frag}}"
        fi
    fi
    printf '{"events":[%s]}' "$events" | _plane_emit
}
if [ "$PLANE_ARMED" = "1" ]; then
    _plane_emit_report_intent || true
fi

# Cross-socket send via the one safe primitive: prechecks the manager session on
# its socket, two-step send, and logs a send_miss (no silent drop) on a miss.
rb_send_rc=0
bot_tmux_send "$MANAGER_SOCKET" "$MANAGER_SESSION" "$MESSAGE" || rb_send_rc=$?

if [ "$PLANE_ARMED" = "1" ]; then
    _plane_state="pane_submitted"
    [ "$rb_send_rc" -ne 0 ] && _plane_state="failed"
    printf '{"events":[{"event_type":"transmission","emitter":"report-back","fleet":"%s","payload":{"msg_id":"%s","attempt_no":1,"carrier":"tmux","destination":"%s","state":"%s"}}]}' \
        "$(json_escape "$FLEET_NAME")" "$PLANE_MSG_ID" \
        "$(json_escape "$MANAGER_SESSION")" "$_plane_state" | _plane_emit || true
fi

# Append structured JSONL event to the fleet-level report-back ledger.
# Path follows overlay convention: local/<fleet>/runtime/ or root runtime/fleet/
# (fleet_runtime_dir owns that rule; Python twin: Paths.fleet_state).
_emit_ledger_event() {
    local ledger="$REPORT_LEDGER"
    mkdir -p "$(dirname "$ledger")"
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
        # plane_msg_id (PR-B T5): the plane communication this report minted —
        # the parity join for the report lane (source_ref report-back:<id>).
        # Always emitted, empty when unarmed (schema-uniform convention).
        printf '{"ts":"%s","bot":"%s","task_id":"%s","status":"%s","summary":"%s","pr_url":"%s","issues":"%s","skill":"%s","progress":"%s","artifact":"%s","task_anomaly":"%s","plane_msg_id":"%s"}\n' \
            "$ts" "$BOT" "$(json_escape "$TASK_ID")" "$STATUS" "$safe_summary" "$pr_url" "$issues" "$skill" "$PROGRESS" "$ARTIFACTS" "$TASK_ANOMALY" "$PLANE_MSG_ID" >> "$ledger"
        rotate_jsonl_by_ts "$ledger"
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
