#!/usr/bin/env bash
# tests/test_pane_send_lock.sh — the per-recipient send lock (#2036)
#
# Two senders to one pane interleaved their chunks. pane_send_verified types a
# payload in 400-byte chunks 0.15s apart (#1493), so a large send takes
# seconds, and nothing serialised the senders of ONE recipient: a second send
# that started inside that window wrote its chunks between the first one's.
# Seen live on 2026-09-30, where a manager's query landed inside a worker's
# report and both receipt trailers broke.
#
# Hermetic, like tests/test_pane_send_verified.sh: bot_tmux is stubbed after
# sourcing lib-common. Every keystroke that reaches any fake pane is appended
# to ONE log in arrival order, so the property under test (a payload's chunks
# arrive together and are followed by its own Enter) is read straight off the
# order the pane received them. The senders run CONCURRENTLY, as background
# jobs of this shell: the defect needs two sends overlapping in time, and a
# sequential test cannot show it.
#
# RUN IT UNDER `/bin/bash` on macOS (bash 3.2), as the sibling suite says.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../claudlobby/_runtime_scripts"
FIXTURES="$SCRIPT_DIR/fixtures/pane-states"
PASS=0; FAIL=0; TOTAL=0

assert_eq() {
    TOTAL=$((TOTAL + 1))
    local desc="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        echo "  PASS: $desc"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $desc (expected '$expected', got '$actual')"
        FAIL=$((FAIL + 1))
    fi
}

# Every wait the send makes, collapsed, except the one this suite is about: the
# pause between chunks stays NON-zero, because two sends only interleave when
# they overlap in time. Each sender below sets its own.
export PANE_SEND_SETTLE_S=0
export PANE_SEND_VERIFY_TICKS=1
export PANE_SEND_CHUNK_BYTES=400
export PANE_SEND_CHUNK_SETTLE_S=0.05

# shellcheck source=../claudlobby/_runtime_scripts/lib-common.sh
. "$LIB_DIR/lib-common.sh"

TMPD=$(mktemp -d)
BG_PIDS=""
cleanup() {
    local p
    for p in $BG_PIDS; do kill "$p" 2>/dev/null || true; done
    rm -rf "$TMPD"
}
trap cleanup EXIT

# The sibling suite's hermetic identity and plane capture: every emit lands in
# CAPTURE through a stub cold rung, never in a real ledger or plane.
SYNTH_ID="synthetic.lockprobe"
export BOT_DIR="$TMPD/synth-bot" BOT_ID="$SYNTH_ID"
export CLAUDLOBBY_ROOT="$TMPD/synth-root"
mkdir -p "$BOT_DIR/data" "$CLAUDLOBBY_ROOT"
export FLEET_NAME="synthetic-fleet"
export PLANE_SOCKET="$TMPD/no-daemon.sock"
CAPTURE="$TMPD/plane-capture.jsonl"
: > "$CAPTURE"
PLANE_EMIT_CLI="$TMPD/capture-cli"
export PLANE_EMIT_CLI
printf '%s\n' '#!/bin/bash' 'f="${@: -1}"' 'cat "$f" >> "'"$CAPTURE"'"' 'echo >> "'"$CAPTURE"'"' > "$PLANE_EMIT_CLI"
chmod +x "$PLANE_EMIT_CLI"

# Every keystroke any fake pane receives, one line each, in arrival order:
#   <socket>|<target>|chunk|<bytes>   a typed chunk
#   <socket>|<target>|key|<key>       a key (Enter)
# The payloads below are built from [A-Z0-9 ] only, so `|` never occurs in a
# chunk and one line is always one keystroke call.
PANE_LOG="$TMPD/pane.log"
: > "$PANE_LOG"

bot_tmux() {
    local sock="$1"; shift
    case "${1:-}" in
        send-keys)
            # send-keys -t <target> -l -- <chunk>   or   send-keys -t <target> Enter
            local target="${3:-}"
            if [ "${4:-}" = "-l" ] && [ "${5:-}" = "--" ]; then
                # A pane that dies after its first chunk: the send fails mid-payload.
                if [ -n "${FAIL_TARGET:-}" ] && [ "$target" = "$FAIL_TARGET" ] &&
                    grep -qF -- "$sock|$target|chunk|" "$PANE_LOG"; then
                    return 1
                fi
                printf '%s|%s|chunk|%s\n' "$sock" "$target" "${6:-}" >> "$PANE_LOG"
            else
                printf '%s|%s|key|%s\n' "$sock" "$target" "${4:-}" >> "$PANE_LOG"
            fi
            ;;
        capture-pane)
            # A pane whose last send was submitted: the verify ends on its first tick.
            cat "$FIXTURES/input-clean-submit.txt"
            ;;
    esac
    return 0
}

# make_payload <letter> <tokens>: numbered tokens, so no two chunks are alike
# (the identical-chunk rule never fires) and every byte names its sender.
make_payload() {
    local l="$1" n="$2" i=1 tok out=""
    while [ "$i" -le "$n" ]; do
        printf -v tok '%s%04d ' "$l" "$i"
        out="$out$tok"
        i=$((i + 1))
    done
    printf '%s' "$out"
}

# submitted <socket> <target>: what that pane SUBMITTED, one line per Enter —
# the chunks it received, in arrival order, joined up to each Enter.
submitted() {
    awk -F'|' -v s="$1" -v t="$2" '
        $1 == s && $2 == t && $3 == "chunk" { seg = seg $4; next }
        $1 == s && $2 == t && $3 == "key" && $4 == "Enter" { print seg; seg = ""; next }
        END { if (seg != "") print "UNSUBMITTED:" seg }' "$PANE_LOG"
}

# wait_for <fixed string>: until the pane log holds it (at most ~5s).
wait_for() {
    local i=0
    while [ "$i" -lt 100 ]; do
        grep -qF -- "$1" "$PANE_LOG" && return 0
        sleep 0.05
        i=$((i + 1))
    done
    return 1
}

# first_line <fixed string>: the pane-log line number where it first appears.
first_line() {
    grep -nF -- "$1" "$PANE_LOG" | head -1 | cut -d: -f1
}

# lock_file_for <socket> <target>: the lock file the SHIPPED rule names, or
# nothing where lib-common has no lock (main before #2036).
lock_file_for() {
    if declare -F _pane_send_lock_file >/dev/null; then
        _pane_send_lock_file "$1" "$2"
        printf '%s' "$_PANE_SEND_LOCK_FILE"
    fi
}

# lock_free <file>: 0 when the file exists and nobody holds its lock.
lock_free() {
    [ -n "$1" ] && [ -e "$1" ] || return 1
    python3 -S -E -c 'import fcntl, sys
f = open(sys.argv[1], "a")
try:
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(1)' "$1"
}

# hold_lock <file> <seconds>: another sender holds the pane, from a process
# this suite owns; returns once it holds. Its record has the shape a sender's
# has, so a refused sender can name it.
hold_lock() {
    mkdir -p "${1%/*}"
    rm -f "$TMPD/held"
    python3 -S -E -c 'import fcntl, os, sys, time
f = open(sys.argv[1], "a+")
fcntl.flock(f, fcntl.LOCK_EX)
f.seek(0)
f.truncate()
f.write("pid=%d since=test bot=external-holder door=test what=payload\n" % os.getpid())
f.flush()
open(sys.argv[2], "w").close()
time.sleep(float(sys.argv[3]))' "$1" "$TMPD/held" "$2" &
    HOLDER_PID=$!
    BG_PIDS="$BG_PIDS $HOLDER_PID"
    local i=0
    while [ ! -e "$TMPD/held" ] && [ "$i" -lt 100 ]; do
        sleep 0.05
        i=$((i + 1))
    done
}

A=$(make_payload A 300)   # 1800 bytes: 5 chunks at the 400-byte cap
B=$(make_payload B 300)

echo "=== #2036: two senders to ONE pane each arrive whole, then their own Enter ==="

# THE defect. A starts first and is slow (0.2s between chunks); B starts once
# A's first chunk is in the pane, while A still has four to go. Without a lock
# B's chunks land between A's, and A's Enter submits a mixture. A also pauses
# 0.3s before its Enter, so a lock that covered the chunks but not the Enter
# (the issue asks for the whole send, verify and repair included) fails too.
: > "$PANE_LOG"
rca=0; rcb=0
( PANE_SEND_CHUNK_SETTLE_S=0.2 PANE_SEND_SETTLE_S=0.3 pane_send_verified sockX botX "$A" ) >/dev/null 2>"$TMPD/a.err" &
pa=$!
wait_for "sockX|botX|chunk|A0001" || true
( PANE_SEND_CHUNK_SETTLE_S=0.01 pane_send_verified sockX botX "$B" ) >/dev/null 2>"$TMPD/b.err" &
pb=$!
wait "$pa" || rca=$?
wait "$pb" || rcb=$?
got=$(submitted sockX botX | sort)
want=$(printf '%s\n%s\n' "$A" "$B" | sort)
if [ "$got" = "$want" ]; then r=whole; else r="interleaved"; fi
assert_eq "two concurrent sends to one pane arrive as two whole payloads, each submitted by its own Enter" "whole" "$r"
if [ "$r" != whole ]; then
    submitted sockX botX | awk '{ printf "    segment %d: %d bytes, starts %s\n", NR, length($0), substr($0, 1, 24) }'
fi
assert_eq "...and both senders report success" "0 0" "$rca $rcb"

echo "=== the lock is per RECIPIENT: other panes are not held up ==="

# A guard, not the defect: passes with or without a lock, and fails a lock too
# coarse to be one per pane. B targets another session while A is mid-send; B
# must finish first, since it is small and fast and A has seconds to go.
: > "$PANE_LOG"
( PANE_SEND_CHUNK_SETTLE_S=0.4 pane_send_verified sockX botX "$A" ) >/dev/null 2>&1 &
pa=$!
wait_for "sockX|botX|chunk|A0001" || true
( PANE_SEND_CHUNK_SETTLE_S=0.01 pane_send_verified sockX botY "$B" ) >/dev/null 2>&1 &
pb=$!
wait "$pa" || true
wait "$pb" || true
la=$(first_line "sockX|botX|key|Enter")
lb=$(first_line "sockX|botY|key|Enter")
if [ -n "$la" ] && [ -n "$lb" ] && [ "$lb" -lt "$la" ]; then r=independent; else r="serialised (A Enter at ${la:-none}, B at ${lb:-none})"; fi
assert_eq "a send to another session is not held up by a send in progress" "independent" "$r"

# The same session NAME on another socket is another server's pane.
: > "$PANE_LOG"
( PANE_SEND_CHUNK_SETTLE_S=0.4 pane_send_verified sockX botX "$A" ) >/dev/null 2>&1 &
pa=$!
wait_for "sockX|botX|chunk|A0001" || true
( PANE_SEND_CHUNK_SETTLE_S=0.01 pane_send_verified sockY botX "$B" ) >/dev/null 2>&1 &
pb=$!
wait "$pa" || true
wait "$pb" || true
la=$(first_line "sockX|botX|key|Enter")
lb=$(first_line "sockY|botX|key|Enter")
if [ -n "$la" ] && [ -n "$lb" ] && [ "$lb" -lt "$la" ]; then r=independent; else r="serialised (A Enter at ${la:-none}, B at ${lb:-none})"; fi
assert_eq "the same session name on another socket is not held up either" "independent" "$r"

echo "=== a sender that cannot get the lock in time sends NOTHING, loudly ==="

# The holder holds for 30s and the check allows 15s: the refused send also
# records a send_miss, whose emit is bounded at 10s and takes seconds on a
# loaded host, so a bound is told from waiting the holder out by a wide margin.
lf=$(lock_file_for sockX botZ)
HOLDER_PID=""
[ -z "$lf" ] || hold_lock "$lf" 30
: > "$PANE_LOG"; : > "$CAPTURE"
rc=0; start=$SECONDS
PANE_SEND_LOCK_WAIT_S=1 pane_send_verified sockX botZ "must never be typed" 2>"$TMPD/timeout.err" || rc=$?
elapsed=$((SECONDS - start))
if [ "$rc" -ne 0 ]; then r=refused; else r="sent (rc 0)"; fi
assert_eq "a send whose pane stays locked past its bound is refused" "refused" "$r"
r=$(grep -c '^sockX|botZ|' "$PANE_LOG" || true)
assert_eq "...and not one keystroke reached that pane (never sends unlocked)" "0" "$r"
if [ "$elapsed" -lt 15 ]; then r=bounded; else r="waited ${elapsed}s"; fi
assert_eq "...and the wait was bounded (a 1s bound against a 30s holder)" "bounded" "$r"
r=$(grep -cE '"send_miss"' "$CAPTURE" || true)
assert_eq "...and it is recorded as a send_miss" "1" "$r"
r=$(grep -cE '"reason": ?"recipient-lock-timeout"' "$CAPTURE" || true)
assert_eq "...whose reason is recipient-lock-timeout" "1" "$r"
r=$(grep -c 'external-holder' "$TMPD/timeout.err" || true)
[ "$r" -ge 1 ] && r=named || r="not named"
assert_eq "...and stderr says it was not sent, naming the holder" "named" "$r"
r=$(grep -c 'NOT sent' "$TMPD/timeout.err" || true)
[ "$r" -ge 1 ] && r=loud || r=quiet
assert_eq "...in words an operator can find (NOT sent)" "loud" "$r"
[ -z "$HOLDER_PID" ] || kill "$HOLDER_PID" 2>/dev/null || true

echo "=== the lock is released on every exit path ==="

lf=$(lock_file_for sockX botR)
: > "$PANE_LOG"
pane_send_verified sockX botR "a clean send" >/dev/null 2>&1 || true
lock_free "$lf" && r=free || r=held
assert_eq "after a clean send the pane's lock is free" "free" "$r"

lf=$(lock_file_for sockX botE)
: > "$PANE_LOG"
rc=0
FAIL_TARGET=botE pane_send_verified sockX botE "$A" >/dev/null 2>&1 || rc=$?
if [ "$rc" -ne 0 ]; then r=failed; else r="rc 0"; fi
assert_eq "a send whose pane dies mid-payload fails" "failed" "$r"
lock_free "$lf" && r=free || r=held
assert_eq "...and leaves the pane's lock free" "free" "$r"

# SIGKILL runs no trap. The kernel drops the lock with the last descriptor, so
# the holder's death frees it once its in-flight child (a chunk's 1s sleep)
# exits. The send left alone would hold the pane for 4s more, and the check
# gives the kill 1.5s, so a pass is the kill's doing, not the send ending.
lf=$(lock_file_for sockX botK)
: > "$PANE_LOG"
( PANE_SEND_CHUNK_SETTLE_S=1 pane_send_verified sockX botK "$A" ) >/dev/null 2>&1 &
pk=$!
BG_PIDS="$BG_PIDS $pk"
wait_for "sockX|botK|chunk|A0001" || true
holder=""
[ -z "$lf" ] || holder=$(sed -n 's/^pid=\([0-9][0-9]*\) .*/\1/p' "$lf" 2>/dev/null | head -1 || true)
r=$(sed -n '1p' "$lf" 2>/dev/null || true)
case "$r" in
    *"bot=$SYNTH_ID door="*"what=payload"*) r=named ;;
    *) r="record: ${r:-none}" ;;
esac
assert_eq "a sender holding a pane records who it is in the lock file" "named" "$r"
lock_free "$lf" && r=free || r=held
assert_eq "...and the pane is held while it sends" "held" "$r"
[ -z "$holder" ] || kill -9 "$holder" 2>/dev/null || true
r=held; i=0
while [ "$i" -lt 30 ]; do
    if lock_free "$lf"; then r=free; break; fi
    sleep 0.05
    i=$((i + 1))
done
assert_eq "a sender SIGKILLed mid-send leaves the pane's lock free" "free" "$r"
wait "$pk" 2>/dev/null || true
: > "$PANE_LOG"
pane_send_verified sockX botK "after the kill" >/dev/null 2>&1 || true
r=$(submitted sockX botK)
assert_eq "...and the next send to that pane goes through" "after the kill" "$r"

echo "=== a lock that cannot be taken does not strand the fleet ==="

# The line: a lock that another send HOLDS is never sent past (above). A lock
# that cannot be taken at all is a broken host, not a concurrent send, so the
# send goes out, loudly, rather than every dispatch and startup prompt on the
# host stopping at once.
: > "$TMPD/not-a-dir"
: > "$PANE_LOG"; : > "$CAPTURE"
rc=0
PANE_SEND_LOCK_DIR="$TMPD/not-a-dir/locks" pane_send_verified sockX botF "sent unlocked" 2>"$TMPD/unlocked.err" || rc=$?
r=$(submitted sockX botF)
assert_eq "an unusable lock dir: the send still goes out" "sent unlocked" "$r"
assert_eq "...and reports success" "0" "$rc"
r=$(grep -c 'WITHOUT' "$TMPD/unlocked.err" || true)
[ "$r" -ge 1 ] && r=loud || r=quiet
assert_eq "...and says on stderr that it went WITHOUT the lock" "loud" "$r"
r=$(grep -cE '"send_unlocked"' "$CAPTURE" || true)
assert_eq "...and records a send_unlocked" "1" "$r"

# The same for a lock helper that cannot run (here: its python fails).
REAL_PY=$(command -v python3)
SHIM="$TMPD/shim"
mkdir -p "$SHIM"
cat > "$SHIM/python3" <<SHIMEOF
#!/bin/bash
# Test shim: the plane lookup answers "no receipt" (rc 1); the lock helper
# fails when LOCK_PY_FAIL is set; everything else is the real python3.
case "\$*" in
    *plane-lookup.py*) exit 1 ;;
    *pane-send-lock*) [ -z "\${LOCK_PY_FAIL:-}" ] || exit 1 ;;
esac
exec "$REAL_PY" "\$@"
SHIMEOF
chmod +x "$SHIM/python3"
: > "$PANE_LOG"; : > "$CAPTURE"
rc=0
PATH="$SHIM:$PATH" LOCK_PY_FAIL=1 pane_send_verified sockX botP "sent anyway" 2>"$TMPD/pyfail.err" || rc=$?
r=$(submitted sockX botP)
assert_eq "a lock helper that fails: the send still goes out" "sent anyway" "$r"
r=$(grep -cE '"send_unlocked"' "$CAPTURE" || true)
assert_eq "...and records a send_unlocked" "1" "$r"

echo "=== the receipt's repair Enter goes under the same lock ==="

# pane_await_receipt presses ONE more Enter when no receipt comes. It is the
# one runtime-script keystroke outside pane_send_verified, and an Enter landing inside
# another sender's chunks submits that sender's payload half-typed.
MSG="msg_0123456789abcdef0123456789abcdef"
: > "$PANE_LOG"; : > "$CAPTURE"
PATH="$SHIM:$PATH" PANE_RECEIPT_WAIT_S=0.2 pane_await_receipt sockX botQ "$MSG" >/dev/null 2>&1 || true
r=$(grep -c '^sockX|botQ|key|Enter$' "$PANE_LOG" || true)
assert_eq "a free pane: the repair Enter is pressed once" "1" "$r"

lf=$(lock_file_for sockX botQ)
HOLDER_PID=""
[ -z "$lf" ] || hold_lock "$lf" 30
: > "$PANE_LOG"; : > "$CAPTURE"
PATH="$SHIM:$PATH" PANE_RECEIPT_WAIT_S=0.2 pane_await_receipt sockX botQ "$MSG" >/dev/null 2>"$TMPD/receipt.err" || true
r=$(grep -c '^sockX|botQ|key|Enter$' "$PANE_LOG" || true)
assert_eq "a pane another send holds: the repair Enter is NOT pressed into it" "0" "$r"
r=$(grep -cE '"reason": ?"recipient-lock-timeout"' "$CAPTURE" || true)
assert_eq "...and the skipped Enter is recorded (recipient-lock-timeout)" "1" "$r"
[ -z "$HOLDER_PID" ] || kill "$HOLDER_PID" 2>/dev/null || true

echo ""
echo "=== $PASS/$TOTAL passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
