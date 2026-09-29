#!/usr/bin/env bash
# tests/test_run_bounded.sh -- lib/run-bounded.sh, which every composed launchd
# timer job runs through (#1965, the launchd half of #897).
#
# launchd has no runtime bound and skips a calendar fire while the previous run
# is still going, so one wedged run silently disables its job. The wrapper bounds
# the run with with_timeout, writes a line when the job starts (before
# lib-common is sourced) and one when it ends, and relays a stop from launchd
# (bootout, kickstart -k) to the job, which with_timeout runs in a process group
# of its own that launchd's clean-up does not reach.
#
# Runs under /bin/bash 3.2 on macOS (.github/workflows/macos-shell.yml) and under
# pytest on Linux (tests/test_run_bounded.py). Every case runs the wrapper with
# $BASH, so the bash under test is the one that runs it.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$HERE/../lib"
WRAP="$LIB/run-bounded.sh"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/run-bounded.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
mkdir "$WORK/home"
PASS=0
TOTAL=0

check() {  # <description> <expected> <actual>
    TOTAL=$((TOTAL + 1))
    if [ "$2" = "$3" ]; then
        PASS=$((PASS + 1))
        echo "  PASS: $1"
    else
        echo "  FAIL: $1 (expected [$2], got [$3])"
    fi
}

# Match <actual> against a shell pattern.
check_like() {  # <description> <pattern> <actual>
    TOTAL=$((TOTAL + 1))
    # shellcheck disable=SC2254
    case "$3" in
        $2) PASS=$((PASS + 1)); echo "  PASS: $1" ;;
        *) echo "  FAIL: $1 (expected like [$2], got [$3])" ;;
    esac
}

# The wrapper as launchd starts it: a closed environment, stdout and stderr to
# one place. W is the scratch dir.
run() {  # <wrapper args...>
    env -i HOME="$WORK/home" PATH="$PATH" LANG=C PLANE_EMIT_DISABLED=1 W="$WORK" \
        "$BASH" "$WRAP" "$@" 2>&1
}

echo "bash under test: $BASH ($BASH_VERSION)"

# The bound cases need a with_timeout that bounds. On a host with neither
# timeout(1) nor gtimeout that is #1978's perl fallback; without it they fail
# for that reason, and this says so first.
check "with_timeout bounds a command on this host (precondition)" "124" \
    "$(env -i HOME="$WORK/home" PATH="$PATH" PLANE_EMIT_DISABLED=1 "$BASH" -c \
        '. "$0" || exit 99; set +e; with_timeout 1 sleep 5; echo $?' "$LIB/lib-common.sh" 2>/dev/null)"

echo "=== the start line ==="
# A copy with no lib-common.sh beside it: the start line must land anyway, so a
# job that started but never logged is told apart from one that never started.
mkdir "$WORK/nolib"
cp "$WRAP" "$WORK/nolib/run-bounded.sh"
out="$(env -i HOME="$WORK/home" PATH="$PATH" "$BASH" "$WORK/nolib/run-bounded.sh" 5 true 2>&1)"
rc=$?
check_like "it is written before lib-common is sourced" \
    "*run-bounded start pid=* budget=5s: true" "$(printf '%s\n' "$out" | head -n 1)"
check "a wrapper that cannot source lib-common fails" "yes" "$([ "$rc" -ne 0 ] && echo yes)"

out="$(run 5 sh -c 'echo out; echo err >&2; exit 3')"
rc=$?
check "the job's exit status passes through" "3" "$rc"
check_like "the start line names the job and its budget" \
    "*run-bounded start pid=* budget=5s: sh -c *" "$(printf '%s\n' "$out" | head -n 1)"
check "the job's stdout passes through" "yes" "$(printf '%s\n' "$out" | grep -qx out && echo yes)"
check "the job's stderr passes through" "yes" "$(printf '%s\n' "$out" | grep -qx err && echo yes)"
check_like "the end line records the status and the budget" \
    "*run-bounded exit rc=3 after *s (budget 5s)" "$(printf '%s\n' "$out" | tail -n 1)"

echo "=== the bound ==="
s=$SECONDS
out="$(run 1 sleep 30)"
rc=$?
check "a job past its budget is stopped with 124" "124" "$rc"
check "it is stopped at the budget, not at its own end" "yes" "$([ $((SECONDS - s)) -le 8 ] && echo yes)"
check_like "the end line records the stop" \
    "*run-bounded exit rc=124 after *s (budget 1s)" "$(printf '%s\n' "$out" | tail -n 1)"

rm -f "$WORK/gc"
run 1 sh -c 'sleep 30 & echo $! >"$W/gc"; wait' >/dev/null
rc=$?
gc="$(cat "$WORK/gc" 2>/dev/null)"
i=0
while [ -n "$gc" ] && kill -0 "$gc" 2>/dev/null && [ "$i" -lt 30 ]; do sleep 0.1; i=$((i + 1)); done
check "the stop reaches a child the job started" "124 dead" \
    "$rc $(if [ -n "$gc" ] && kill -0 "$gc" 2>/dev/null; then kill "$gc"; echo alive; else echo dead; fi)"

echo "=== a stop from launchd ==="
# launchd stops a job (bootout, kickstart -k) by signalling this process. The job
# runs under with_timeout in a process group of its own, so without the relay it
# would outlive the stop, and the next start would overlap it.
rm -f "$WORK/gc"
env -i HOME="$WORK/home" PATH="$PATH" LANG=C PLANE_EMIT_DISABLED=1 W="$WORK" \
    "$BASH" "$WRAP" 60 sh -c 'sleep 60 & echo $! >"$W/gc"; wait' >"$WORK/stop.out" 2>&1 &
wrapper=$!
i=0
while [ ! -s "$WORK/gc" ] && [ "$i" -lt 50 ]; do sleep 0.1; i=$((i + 1)); done
kill -TERM "$wrapper"
i=0
while kill -0 "$wrapper" 2>/dev/null && [ "$i" -lt 100 ]; do sleep 0.1; i=$((i + 1)); done
if kill -0 "$wrapper" 2>/dev/null; then
    kill -KILL "$wrapper" 2>/dev/null
    stopped=no
else
    stopped=yes
fi
wait "$wrapper" 2>/dev/null
rc=$?
check "the wrapper exits on TERM" "yes" "$stopped"
gc="$(cat "$WORK/gc" 2>/dev/null)"
i=0
while [ -n "$gc" ] && kill -0 "$gc" 2>/dev/null && [ "$i" -lt 30 ]; do sleep 0.1; i=$((i + 1)); done
check "the TERM reaches the job and its children" "dead" \
    "$(if [ -n "$gc" ] && kill -0 "$gc" 2>/dev/null; then kill "$gc"; echo alive; else echo dead; fi)"
check "the stopped run exits 143" "143" "$rc"
check_like "the end line records the stop" \
    "*run-bounded exit rc=143 after *s (budget 60s)" "$(tail -n 1 "$WORK/stop.out")"

echo "=== usage ==="
out="$(run 5)"
rc=$?
check "no command is refused with 125" "125" "$rc"

echo "=== $PASS/$TOTAL passed ==="
[ "$PASS" -eq "$TOTAL" ]
