#!/bin/bash
# turn-marker.sh — UserPromptSubmit, Stop and StopFailure hook (#1053): the
# bot's own record of when input last reached its session and when its last
# turn ended. Composed for every bot by claudlobby/system.yaml:
#
#   UserPromptSubmit   writes data/.turn-input   a prompt was submitted to the
#                                                session (typed, or sent
#                                                through its pane)
#   Stop, StopFailure  writes data/.turn-ended   the turn ended: normally, or
#                                                on an error (a usage limit,
#                                                an API error)
#
# The fact is the file's modification time. Its one line names the event that
# last wrote it, and each write replaces it. Nothing reads the markers yet:
# #1053's compaction executor will, beside data/.last-tool-call (bot-vitals.sh),
# to tell a finished turn from a running one. A reader compares all three: a
# Stop that another Stop hook blocks lets the turn go on, so .turn-ended alone
# can be older than the turn's next tool call. They outlive a session restart:
# nothing removes them, and data-sweep.sh does not match their names.
#
# stdout stays empty and every path exits 0: a UserPromptSubmit hook's stdout
# is added to the model's context, a Stop hook's can be read as a decision, and
# exit 2 from either would block the prompt or the stop. No -e for that reason.
# Refuses without an absolute BOT_DIR (never a cwd write: a bot's working
# directory is its project, #874), and writes nothing when data/ is missing.
# Nothing here starts a process but cat: the payload is read whole, as a pipe
# must be, and the marker is written by a shell builtin.

set -uo pipefail
# Read first, on every path: the writer must not meet a closed pipe.
PAYLOAD="$(cat 2>/dev/null || true)"
case "${BOT_DIR:-}" in /*) ;; *) exit 0 ;; esac
[ -d "$BOT_DIR/data" ] || exit 0
# The event from the payload's own key, with or without spaces around the
# colon. JSON escapes every quote inside a string, so a prompt that quotes
# this key cannot match it.
re='"hook_event_name"[[:space:]]*:[[:space:]]*"([A-Za-z]+)"'
[[ $PAYLOAD =~ $re ]] || exit 0
event=${BASH_REMATCH[1]}
case "$event" in
    UserPromptSubmit) marker=.turn-input ;;
    Stop | StopFailure) marker=.turn-ended ;;
    *) exit 0 ;;
esac
# A read-only data/ (or marker) fails the write; the hook still exits 0.
{ printf '%s\n' "$event" >"$BOT_DIR/data/$marker"; } 2>/dev/null || true
exit 0
