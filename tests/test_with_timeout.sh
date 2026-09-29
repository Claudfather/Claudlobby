#!/usr/bin/env bash
# tests/test_with_timeout.sh -- with_timeout on a host with no timeout(1) and no
# gtimeout (#917).
#
# A stock macOS host resolves neither. with_timeout falls back to perl there,
# which ships with macOS, and the fallback has to keep the contract timeout(1)
# gives every caller: rc 124 when the bound expires, the command's own status
# otherwise, and the signal sent to the command's whole process group, because a
# bounded `claude -p` has children of its own.
#
# The fallback arm runs every case with timeout and gtimeout masked from PATH,
# the way a stock Mac resolves them. Where a real timeout(1) exists (Linux) the
# oracle arm runs the same cases through it, unmasked, against the same
# expectations. Those are timeout(1)'s own values, measured, so a wrong
# expectation fails in the oracle arm instead of certifying the fallback.
#
# Runs under /bin/bash 3.2 on macOS (.github/workflows/macos-shell.yml) and under
# pytest on Linux (tests/test_with_timeout.py). Every case runs in a fresh $BASH,
# so the bash under test is the one that sources lib-common and runs the case.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$HERE/../lib/lib-common.sh"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/with-timeout.XXXXXX")"
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

# The host PATH as one directory of links, first match wins, with no timeout and
# no gtimeout in it: the shape of _path_without in
# tests/test_update_claude_code_verify.py, in bash so /bin/bash runs it on a
# runner with no Python set up.
MASK="$WORK/masked-bin"
mkdir "$MASK"
old_ifs="$IFS"
IFS=:
for d in $PATH; do
    [ -d "$d" ] || continue
    for f in "$d"/*; do
        n="${f##*/}"
        case "$n" in timeout | gtimeout) continue ;; esac
        [ -e "$MASK/$n" ] && continue
        if [ -f "$f" ] && [ -x "$f" ]; then ln -s "$f" "$MASK/$n"; fi
    done
done
IFS="$old_ifs"

# One case: a fresh $BASH with nothing inherited but what is listed, lib-common
# sourced under PATH=$1, then the case code $2. W is the scratch dir.
run() {  # <PATH> <code>
    env -i HOME="$WORK/home" PATH="$1" LANG=C PLANE_EMIT_DISABLED=1 W="$WORK" \
        "$BASH" -c '. "$0" || exit 99; set +e; eval "$1"' "$LIB" "$2" 2>/dev/null
}

printf '#!/bin/sh\necho unreachable\n' >"$WORK/notexec"
chmod 644 "$WORK/notexec"
printf '#!/bin/sh\nsleep 10\n' >"$WORK/hangs"
chmod 755 "$WORK/hangs"
USR1=$((128 + $(kill -l USR1)))

ARMS="fallback"
if command -v timeout >/dev/null 2>&1 || command -v gtimeout >/dev/null 2>&1; then
    ARMS="oracle fallback"
fi
echo "bash under test: $BASH ($BASH_VERSION); arms: $ARMS"

for arm in $ARMS; do
    if [ "$arm" = oracle ]; then P="$PATH"; else P="$MASK"; fi
    echo "=== $arm arm ==="

    if [ "$arm" = oracle ]; then
        check "timeout(1) or gtimeout resolves (the oracle is real)" "set" \
            "$(run "$P" 'echo "${_TIMEOUT_BIN:+set}"')"
    else
        # The mask must take effect, or this arm tests timeout(1) and passes
        # without ever running the fallback.
        check "the mask hides timeout and gtimeout from lib-common" "none" \
            "$(run "$P" 'echo "${_TIMEOUT_BIN:-none}"')"
        check "perl is on the masked PATH" "yes" \
            "$(run "$P" 'type -P perl >/dev/null && echo yes')"
    fi
    check "with_timeout_bounds: the bound is enforced here" "bounded" \
        "$(run "$P" 'if with_timeout_bounds; then echo bounded; else echo unbounded; fi')"

    check "expiry returns 124, at the bound" "124 1" \
        "$(run "$P" 's=$SECONDS; with_timeout 1 sleep 10; rc=$?; echo "$rc $((SECONDS - s <= 5 ? 1 : 0))"')"
    check "a normal exit keeps status 0" "0" \
        "$(run "$P" 'with_timeout 5 true; echo $?')"
    check "a normal exit keeps status 3" "3" \
        "$(run "$P" 'with_timeout 5 sh -c "exit 3"; echo $?')"
    check "stdout passes through" "[ok]" \
        "$(run "$P" 'echo "[$(with_timeout 5 echo ok)]"')"
    check "stdin passes through" "[hi]" \
        "$(run "$P" 'echo "[$(echo hi | with_timeout 5 cat)]"')"

    # A child the command backgrounds dies with it: the signal goes to the whole
    # process group. The sh dying alone would leave the sleep running.
    rm -f "$WORK/gc"
    check "expiry signals the command's whole process group" "124 dead" "$(run "$P" '
        with_timeout 1 sh -c "sleep 10 & echo \$! >\"\$W/gc\"; wait"; rc=$?
        gc=$(cat "$W/gc"); i=0
        while kill -0 "$gc" 2>/dev/null && [ "$i" -lt 30 ]; do sleep 0.1; i=$((i + 1)); done
        if kill -0 "$gc" 2>/dev/null; then st=alive; kill "$gc"; else st=dead; fi
        echo "$rc $st"')"
    check "a command that exits 0 on TERM still reports expiry" "124" \
        "$(run "$P" 'with_timeout 1 sh -c "trap \"exit 0\" TERM; sleep 10 & wait"; echo $?')"

    # A stopped command acts on the signal only once continued: timeout(1)
    # follows the signal with SIGCONT. A command in its own process group that
    # reads the terminal is stopped by SIGTTIN, so this is not hypothetical.
    # Bounded here, so a wrapper that never ends it reports "hung", not a hang.
    rm -f "$WORK/rc"
    check "a stopped command is still ended at the bound" "124" "$(run "$P" '
        ( with_timeout 1 sh -c "kill -STOP \$\$"; echo $? >"$W/rc" ) &
        sub=$!; i=0
        while kill -0 "$sub" 2>/dev/null && [ "$i" -lt 100 ]; do sleep 0.1; i=$((i + 1)); done
        if kill -0 "$sub" 2>/dev/null; then
            pkill -KILL -P "$sub"; kill -KILL "$sub"; echo hung
        else
            cat "$W/rc"
        fi')"

    # A TERM sent to the wrapper itself, not the group, is relayed to the group.
    rm -f "$WORK/gc" "$WORK/rc"
    check "a TERM to the wrapper reaches the command's group" "143 dead" "$(run "$P" '
        ( with_timeout 30 sh -c "sleep 30 & echo \$! >\"\$W/gc\"; wait"; echo $? >"$W/rc" ) &
        sub=$!; i=0
        while [ ! -s "$W/gc" ] && [ "$i" -lt 50 ]; do sleep 0.1; i=$((i + 1)); done
        kill -TERM "$(pgrep -P "$sub" | head -n 1)"; wait "$sub"
        gc=$(cat "$W/gc"); i=0
        while kill -0 "$gc" 2>/dev/null && [ "$i" -lt 30 ]; do sleep 0.1; i=$((i + 1)); done
        if kill -0 "$gc" 2>/dev/null; then st=alive; kill "$gc"; else st=dead; fi
        echo "$(cat "$W/rc") $st"')"
    check "a signal death passes through as 128+N" "$USR1" \
        "$(run "$P" 'with_timeout 5 sh -c "kill -USR1 \$\$"; echo $?')"

    check "a command that is not there is 127" "127" \
        "$(run "$P" 'with_timeout 5 "$W/no-such-command"; echo $?')"
    check "a command that cannot run is 126" "126" \
        "$(run "$P" 'with_timeout 5 "$W/notexec"; echo $?')"
    check "duration 0 sets no bound" "5" \
        "$(run "$P" 'with_timeout 0 sh -c "sleep 1; exit 5"; echo $?')"
    check "a fractional duration" "124" \
        "$(run "$P" 'with_timeout 0.5 sleep 10; echo $?')"
    check "a duration with a unit suffix" "124" \
        "$(run "$P" 'with_timeout 1s sleep 10; echo $?')"
    check "an invalid duration is refused with 125" "125" \
        "$(run "$P" 'with_timeout abc true; echo $?')"

    # The caller whose 124 used to count only when timeout(1) resolved.
    check "measure_claude_version reports a hang as a hang" \
        "1 $WORK/hangs --version did not finish within 1s" \
        "$(run "$P" 'CLAUDE_VERSION_TIMEOUT_S=1 measure_claude_version "$W/hangs"; echo "$? $CLAUDE_VERSION_WHY"')"
done

echo "=== $PASS/$TOTAL passed ==="
[ "$PASS" -eq "$TOTAL" ]
