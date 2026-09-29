#!/bin/bash
# run-bounded.sh <seconds> <command> [args...]
#
# How every composed launchd timer job runs (#1965, the launchd half of #897).
# launchd has no runtime bound, and it skips a calendar fire while the previous
# run is still going, so one wedged run silently disables its job. This wrapper
# bounds the run with with_timeout and writes one line when the job starts and
# one when it ends. The plist sends both lines, with the job's own output, to
# the job's launchd log (StandardOutPath / StandardErrorPath).
#
# The start line is written before lib-common is sourced, so a job that started
# but never logged can be told apart from one that never started.
#
# The job runs in the background so that a stop from launchd (bootout,
# kickstart -k) still reaches it. launchd signals this process and, at its exit,
# the process group it started; with_timeout runs the job in a process group of
# its own, which that clean-up does not reach. The trap relays the signal to
# with_timeout's child, and with_timeout relays it to the job's whole group.
#
# On a host with neither timeout(1) nor gtimeout, with_timeout bounds the run
# only once its perl fallback (#1978) is in lib-common; before that it runs the
# job unbounded, and the start and end lines are still written.

_rb_now() { date '+%Y-%m-%dT%H:%M:%S%z'; }

printf '%s run-bounded start pid=%s budget=%ss:%s\n' \
    "$(_rb_now)" "$$" "${1:-}" "$(shift; printf ' %s' "$@")" >&2

if [ "$#" -lt 2 ]; then
    echo "run-bounded: usage: run-bounded.sh <seconds> <command> [args...]" >&2
    exit 125
fi
_rb_budget="$1"
shift

if [ ! -f "${0%/*}/lib-common.sh" ]; then
    echo "run-bounded: no lib-common.sh beside ${0}; the job did not run" >&2
    exit 125
fi
# shellcheck source=lib-common.sh
. "${0%/*}/lib-common.sh"
set +e

_rb_t0=$SECONDS
with_timeout "$_rb_budget" "$@" &
_rb_job=$!

# with_timeout's child is timeout(1), gtimeout or perl, which relays the signal
# to the job's group; on a host where with_timeout runs the job unbounded, it is
# the job itself. TERM whatever arrived: a background job ignores INT.
_rb_relay() {
    local kids
    kids="$(pgrep -P "$_rb_job" 2>/dev/null || true)"
    # shellcheck disable=SC2086
    kill -TERM ${kids:-$_rb_job} 2>/dev/null || true
}
trap _rb_relay TERM INT HUP

# A trapped signal interrupts wait while the job is still running; wait again
# until it has exited, so the status below is the job's own.
_rb_rc=0
while :; do
    wait "$_rb_job"
    _rb_rc=$?
    kill -0 "$_rb_job" 2>/dev/null || break
done

printf '%s run-bounded exit rc=%s after %ss (budget %ss)\n' \
    "$(_rb_now)" "$_rb_rc" "$((SECONDS - _rb_t0))" "$_rb_budget" >&2
exit "$_rb_rc"
