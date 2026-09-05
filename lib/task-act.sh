#!/bin/bash
# task-act.sh — a MANAGER's two acts on one open task (chunk M-A, #1481).
#
# Usage:
#   task-act.sh withdraw <task-id> --reason "why"
#   task-act.sh escalate <task-id> "the question for the human"
#
# WITHDRAW retires a dispatch the manager no longer wants answered -- the
# undelivered broadcast, the task overtaken by events -- as a terminal
# `cancelled` task event. Before it, a manager had exactly two ways to end a
# row: get a report, or re-dispatch with `--supersedes`. Neither fits a send
# that never reached the bot, so nine such rows sat in one fleet's attention
# queue for 22 hours with no door to close them.
#
# ESCALATE raises the task FOR HUMAN GUIDANCE as a NON-terminal `escalated`
# task event carrying the question. Non-terminal is the ruling and it is the
# whole point: the task stays open while the human decides, so the work is
# not lost and the manager still owns it. It clears itself -- the attention
# arm holds only while `escalated` is the assignment's newest task event, so
# a re-dispatch, a withdrawal, a report or any progress ends it with no
# second door and no state to reconcile.
#
# THE PLANE IS THE ONLY RECORD, so unlike dispatch-task.sh (whose mission is
# the SEND, and which therefore sends anyway and discloses) an act whose
# whole content is the record REFUSES at rc 3 when it cannot be recorded.
# There is nothing else for it to have done.
#
# RESOLUTION REFUSES RATHER THAN GUESSES. A manager holds task ids, not the
# roster, so unlike `--supersedes` this door cannot scope the lookup by
# assignee. It asks for every OPEN assignment carrying the id and refuses
# when more than one matches, naming them: a task id is unique per dispatch
# but not across bots (#526 lets two fleets hold the same bot name, and a
# re-dispatch under one id is legal), and cancelling the wrong worker's task
# is worse than making the caller name the row.
#
# Identity is this bot's $BOT_ID / $FLEET_NAME, the dispatch door's rule --
# both come from the composed bot.conf a session sources.
#
# Exit codes: 0 acted - 1 usage - 2 nothing open under that id (or ambiguous)
# - 3 the plane could not record it (nothing happened).
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

_usage() {
    cat >&2 <<'USAGE'
Usage:
  task-act.sh withdraw <task-id> --reason "why"
  task-act.sh escalate <task-id> "the question for the human"
USAGE
    exit 1
}

ACT="${1:-}"
shift || true
case "$ACT" in
    withdraw|escalate) ;;
    *) _usage ;;
esac

TASK_ID="${1:-}"
shift || true
[ -n "$TASK_ID" ] || _usage

REASON=""
QUESTION=""
if [ "$ACT" = "withdraw" ]; then
    while [ $# -gt 0 ]; do
        case "$1" in
            --reason)
                if [ -z "${2:-}" ]; then
                    echo "task-act: --reason needs a value" >&2
                    exit 1
                fi
                REASON="$2"; shift 2 ;;
            -*) echo "task-act: unknown flag '$1'" >&2; exit 1 ;;
            *)  break ;;
        esac
    done
    # A withdrawal with no reason is a row nobody can later explain -- the
    # exact shape the attention queue already suffers from. Required, not
    # defaulted.
    [ -n "$REASON" ] || { echo "task-act: withdraw needs --reason \"why\"" >&2; exit 1; }
else
    QUESTION="$*"
    [ -n "$QUESTION" ] || { echo "task-act: escalate needs the question" >&2; exit 1; }
fi

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$(cd "$LIB_DIR/.." && pwd)}"
ACTOR="${BOT_ID:-${BOT_NAME:-}}"
if [ -z "$ACTOR" ]; then
    echo "task-act: BOT_ID is empty -- this door records WHO acted, and an act by nobody is not a record" >&2
    exit 1
fi
if ! plane_armed task-act --require-fleet; then
    # PLANE_EMIT_DISABLED=1 or no fleet: there is no other record, so there is
    # no degraded mode to fall back to.
    echo "task-act: the plane is not armed here -- the act is the record, so nothing was done" >&2
    exit 3
fi

# --- resolve the row, or refuse naming the candidates ------------------------
ROWS=$(python3 -S -E "$LIB_DIR/plane-lookup.py" --root "$CLAUDLOBBY_ROOT" \
    --task-id "$TASK_ID" --all-open) || {
    echo "task-act: the plane could not be read -- unreachable, not empty" >&2
    exit 3
}
if [ -z "$ROWS" ]; then
    echo "task-act: no OPEN assignment carries $TASK_ID (a closed one answers empty too -- check 'claudlobby brief')" >&2
    exit 2
fi
MATCHES=$(printf '%s\n' "$ROWS" | grep -c . || true)
if [ "$MATCHES" -gt 1 ]; then
    echo "task-act: $TASK_ID matches $MATCHES open assignments -- refusing to pick one:" >&2
    printf '%s\n' "$ROWS" | while read -r _wi _asg _msg who fleet; do
        echo "  $_asg  assignee $who  fleet $fleet" >&2
    done
    echo "task-act: name the assignment on the plane, or close the duplicates first" >&2
    exit 2
fi
read -r WI ASG _MSG _WHO _FLEET <<<"$ROWS"

# --- the one event -----------------------------------------------------------
safe_fleet=$(json_escape "$FLEET_NAME")
actor_alias="bot:$FLEET_NAME/$ACTOR"
safe_actor=$(json_escape "$actor_alias")
# The prose rides its OWN field and the summary stays short. Both are capped
# at 4 KB by the contract, and repeating a long reason inside the summary is
# how a legal reason becomes an over-cap summary that REJECTS the whole
# emission -- which for this door means the act does not happen.
if [ "$ACT" = "withdraw" ]; then
    EVENT="cancelled"
    TEXT_FRAG="\"reason\":\"$(json_escape "$REASON")\""
    SUMMARY="withdrawn by $ACTOR"
else
    EVENT="escalated"
    TEXT_FRAG="\"question\":\"$(json_escape "$QUESTION")\""
    SUMMARY="escalated by $ACTOR"
fi
# source_ref names the DISPATCH the act is about (the same `dispatch-log:<id>`
# every door on this row stamps), so the act joins the row it retires or
# raises with no per-door schema -- plane-parity's rule. The task id is
# ESCAPED like every other caller-supplied value: it arrives from a human
# typing it, not from the mint.
printf -v BATCH '{"events":[{"event_type":"task","emitter":"task-act","source_ref":"dispatch-log:%s","fleet":"%s","payload":{"work_item_id":"%s","assignment_id":"%s","event":"%s","actor":"%s","by":"%s",%s,"summary":"%s"}}]}' \
    "$(json_escape "$TASK_ID")" "$safe_fleet" "$WI" "$ASG" "$EVENT" "$safe_actor" \
    "$(json_escape "$ACTOR")" "$TEXT_FRAG" "$(json_escape "$SUMMARY")"

# Here-string, never a pipeline: PLANE_EMIT_LAST_RC must reach the decision
# below, and a pipeline runs the function in a subshell where it cannot.
plane_emit_events task-act <<<"$BATCH"
if [ "${PLANE_EMIT_LAST_RC:-0}" -ne 0 ]; then
    echo "task-act: the plane did NOT record this $ACT (rc=$PLANE_EMIT_LAST_RC) -- nothing happened; there is no other record" >&2
    exit 3
fi

if [ "$ACT" = "withdraw" ]; then
    echo "withdrew $TASK_ID ($ASG): $REASON"
else
    echo "escalated $TASK_ID ($ASG): $QUESTION"
fi
