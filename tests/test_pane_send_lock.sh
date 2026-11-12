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

# The sibling suite's hermetic identity and plane capture: the socket is dead,
# so every emit is staged as a raw batch under the scratch root, never in a real
# plane, and cap_refresh copies the staged events into CAPTURE for the asserts.
SYNTH_ID="synthetic.lockprobe"
export BOT_DIR="$TMPD/synth-bot" BOT_ID="$SYNTH_ID"
export CLAUDLOBBY_ROOT="$TMPD/synth-root"
mkdir -p "$BOT_DIR/data" "$CLAUDLOBBY_ROOT/state"
export FLEET_NAME="synthetic-fleet"
export PLANE_SOCKET="$TMPD/no-daemon.sock"
CAPTURE="$TMPD/plane-capture.jsonl"
: > "$CAPTURE"
export PLANE_EMIT_DISABLED=0
cap_reset() { rm -f "$CLAUDLOBBY_ROOT/state/plane/staged/"*.batch; : > "$CAPTURE"; }
cap_refresh() {
    python3 - "$CLAUDLOBBY_ROOT/state/plane/staged" "$CAPTURE" <<'PY'
import json, pathlib, sys
with open(sys.argv[2], "w") as out:
    for path in sorted(pathlib.Path(sys.argv[1]).glob("*.batch")):
        for event in json.loads(path.read_text())["events"]:
            out.write(json.dumps(event) + "\n")
PY
}

# Every keystroke any fake pane receives, one line each, in arrival order:
#   <socket>|<target>|chunk|<bytes>   a typed chunk
#   <socket>|<target>|key|<key>       a key (Enter)
# The payloads below are built from [A-Z0-9 ] only, so `|` never occurs in a
# chunk and one line is always one keystroke call.
PANE_LOG="$TMPD/pane.log"
: > "$PANE_LOG"

# Each fake pane's input box: what was typed into it since its last Enter, one
# file per <socket>|<target>. A send presses Enter only once the box SHOWS the
# end of its payload (#1236), and a TUI draws only what it has read, so the
# fake draws exactly what reached it. Concurrent senders append to one file in
# arrival order, as they would land in one real box.
BOXES="$TMPD/boxes"
mkdir -p "$BOXES"
box_file() { local k="$1|$2"; printf '%s/%s' "$BOXES" "${k//[!A-Za-z0-9._-]/_}"; }

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
                printf '%s' "${6:-}" >> "$(box_file "$sock" "$target")"
            else
                printf '%s|%s|key|%s\n' "$sock" "$target" "${4:-}" >> "$PANE_LOG"
                # An Enter submits the box, which empties, unless a case told this
                # pane to swallow it: <box>.swallow counts the Enters it keeps, as a
                # TUI that read the Enter with the text keeps the text.
                if [ "${4:-}" = Enter ]; then
                    local bf n
                    bf=$(box_file "$sock" "$target")
                    n=$(cat "$bf.swallow" 2>/dev/null || true)
                    case "$n" in ''|*[!0-9]*) n=0 ;; esac
                    if [ "$n" -gt 0 ]; then printf '%s' "$((n - 1))" > "$bf.swallow"; else : > "$bf"; fi
                fi
            fi
            ;;
        capture-pane)
            # The box as the TUI draws it: what was typed since the last Enter, on
            # the live hint frame's input line after the glyph and its NBSP (the
            # sibling suite's typed_frame). Empty, it is a pane whose last send was
            # submitted, so the verify after an Enter ends on its first tick.
            local prev="" a target="" bf
            for a in "$@"; do [ "$prev" = "-t" ] && target="$a"; prev="$a"; done
            bf=$(box_file "$sock" "$target")
            if [ -s "$bf" ]; then
                _BOX_TEXT="$(cat "$bf")" LC_ALL=C awk -v re="$_PANE_INPUT_GLYPH_RE" '
                    { line[NR] = $0; if (match($0, re)) { last = NR; glyph = substr($0, 1, RLENGTH) } }
                    END { for (i = 1; i <= NR; i++)
                              print (i == last ? glyph "\302\240" ENVIRON["_BOX_TEXT"] : line[i]) }' \
                    "$FIXTURES/input-placeholder-hint.txt"
            else
                cat "$FIXTURES/input-clean-submit.txt"
            fi
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

echo "=== an Enter the box swallows: its retry comes before any other sender's keys ==="

# vera's #2040 review: the lock must cover the WHOLE send, the retry Enter
# included, and a lock let go after the first Enter passed every other check.
# Here the pane keeps A's text after A's first Enter (a swallowed Enter), so A
# presses another after its verify window; B starts the moment A's first Enter
# lands. Under one lock no B chunk may arrive between A's two Enters. The
# control uses separate lock directories: the occupied-input check must now
# refuse B instead of appending to A. With one shared lock B waits until A
# submits, then sends normally. A has no letter B, so B chunks identify B.
# swallowed_pair <lock dir for A> <lock dir for B>: the number of B chunks the
# pane received between its first and second Enter.
swallowed_pair() {
    : > "$PANE_LOG"
    local bf pa pb t=0
    bf=$(box_file sockX botW)
    : > "$bf"
    printf '1' > "$bf.swallow"
    ( PANE_SEND_LOCK_DIR="$1" PANE_SEND_VERIFY_TICKS=3 pane_send_verified sockX botW "$A" ) >/dev/null 2>&1 &
    pa=$!
    BG_PIDS="$BG_PIDS $pa"
    while [ "$t" -lt 250 ] && ! grep -q '^sockX|botW|key|Enter$' "$PANE_LOG"; do
        sleep 0.02
        t=$((t + 1))
    done
    ( PANE_SEND_LOCK_DIR="$2" pane_send_verified sockX botW "$B" ) >/dev/null 2>&1 &
    pb=$!
    BG_PIDS="$BG_PIDS $pb"
    wait "$pa" || true
    wait "$pb" || true
    awk -F'|' '$1 == "sockX" && $2 == "botW" {
            if ($3 == "key" && $4 == "Enter") { enters++; next }
            if ($3 == "chunk" && enters == 1 && index($4, "B")) n++
        } END { print n + 0 }' "$PANE_LOG"
}
r=$(swallowed_pair "$TMPD/swallow-lock-a" "$TMPD/swallow-lock-b")
assert_eq "separate locks: held-input refusal prevents B appending to A" "0" "$r"
r=$(grep -c '^sockX|botW|chunk|B' "$PANE_LOG" || true)
assert_eq "separate locks: B sent no new payload into the hold" "0" "$r"
r=$(swallowed_pair "" "")
assert_eq "one lock: no B chunk arrives between A's swallowed Enter and its retry" "0" "$r"
r=$(submitted sockX botW)
case "$r" in *"$B"*) r=submitted ;; *) r=missing ;; esac
assert_eq "one lock: B sends after A submits, rather than being refused" "submitted" "$r"

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
: > "$PANE_LOG"; cap_reset
rc=0; start=$SECONDS
PANE_SEND_LOCK_WAIT_S=1 pane_send_verified sockX botZ "must never be typed" 2>"$TMPD/timeout.err" || rc=$?
elapsed=$((SECONDS - start))
if [ "$rc" -ne 0 ]; then r=refused; else r="sent (rc 0)"; fi
assert_eq "a send whose pane stays locked past its bound is refused" "refused" "$r"
r=$(grep -c '^sockX|botZ|' "$PANE_LOG" || true)
assert_eq "...and not one keystroke reached that pane (never sends unlocked)" "0" "$r"
if [ "$elapsed" -lt 15 ]; then r=bounded; else r="waited ${elapsed}s"; fi
assert_eq "...and the wait was bounded (a 1s bound against a 30s holder)" "bounded" "$r"
cap_refresh; r=$(grep -cE '"send_miss"' "$CAPTURE" || true)
assert_eq "...and it is recorded as a send_miss" "1" "$r"
cap_refresh; r=$(grep -cE '"reason": ?"recipient-lock-timeout"' "$CAPTURE" || true)
assert_eq "...whose reason is recipient-lock-timeout" "1" "$r"
r=$(grep -c 'external-holder' "$TMPD/timeout.err" || true)
[ "$r" -ge 1 ] && r=named || r="not named"
assert_eq "...and stderr says it was not sent, naming the holder" "named" "$r"
r=$(grep -c 'NOT sent' "$TMPD/timeout.err" || true)
[ "$r" -ge 1 ] && r=loud || r=quiet
assert_eq "...in words an operator can find (NOT sent)" "loud" "$r"
[ -z "$HOLDER_PID" ] || kill "$HOLDER_PID" 2>/dev/null || true

echo "=== an occupied input box is refused after taking the recipient lock ==="
: > "$PANE_LOG"
held_box=$(box_file sockX botHeld)
lf=$(lock_file_for sockX botHeld)
python3 - "$lf" "$held_box" "$TMPD/held-ready" <<'PYLOCK' &
import fcntl, pathlib, sys, time
with open(sys.argv[1], "a+") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    pathlib.Path(sys.argv[3]).touch()
    time.sleep(0.4)
    pathlib.Path(sys.argv[2]).write_text("PREEXISTING INPUT")
    time.sleep(0.2)
PYLOCK
holder=$!
while [ ! -f "$TMPD/held-ready" ]; do sleep 0.02; done
rc=0
pane_send_verified sockX botHeld "NEW INPUT" 2>"$TMPD/held.err" || rc=$?
wait "$holder"
assert_eq "held input returns a distinct definite refusal" "4" "$rc"
assert_eq "the inspection used the pane after the prior holder released it" "PREEXISTING INPUT" "$(cat "$held_box")"
assert_eq "no payload or Enter was sent to the occupied box" "0" "$(wc -l < "$PANE_LOG" | tr -d ' ')"
lock_free "$lf" && r=free || r=held
assert_eq "the refusal releases the recipient lock" "free" "$r"

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
prior=$(cat "$(box_file sockX botK)")
: > "$PANE_LOG"
rc=0
pane_send_verified sockX botK "after the kill" >/dev/null 2>&1 || rc=$?
assert_eq "...and a next send refuses the killed sender's stranded input" "4" "$rc"
assert_eq "...without changing the stranded bytes" "$prior" "$(cat "$(box_file sockX botK)")"
assert_eq "...without typing or pressing Enter" "0" "$(wc -l < "$PANE_LOG" | tr -d ' ')"

echo "=== a lock that cannot be taken does not strand the fleet ==="

# The line: a lock that another send HOLDS is never sent past (above). A lock
# that cannot be taken at all is a broken host, not a concurrent send, so the
# send goes out, loudly, rather than every dispatch and startup prompt on the
# host stopping at once.
: > "$TMPD/not-a-dir"
: > "$PANE_LOG"; cap_reset
rc=0
PANE_SEND_LOCK_DIR="$TMPD/not-a-dir/locks" pane_send_verified sockX botF "sent unlocked" 2>"$TMPD/unlocked.err" || rc=$?
r=$(submitted sockX botF)
assert_eq "an unusable lock dir: the send still goes out" "sent unlocked" "$r"
assert_eq "...and reports success" "0" "$rc"
r=$(grep -c 'WITHOUT' "$TMPD/unlocked.err" || true)
[ "$r" -ge 1 ] && r=loud || r=quiet
assert_eq "...and says on stderr that it went WITHOUT the lock" "loud" "$r"
cap_refresh; r=$(grep -cE '"send_unlocked"' "$CAPTURE" || true)
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
: > "$PANE_LOG"; cap_reset
rc=0
PATH="$SHIM:$PATH" LOCK_PY_FAIL=1 pane_send_verified sockX botP "sent anyway" 2>"$TMPD/pyfail.err" || rc=$?
r=$(submitted sockX botP)
assert_eq "a lock helper that fails: the send still goes out" "sent anyway" "$r"
cap_refresh; r=$(grep -cE '"send_unlocked"' "$CAPTURE" || true)
assert_eq "...and records a send_unlocked" "1" "$r"

echo "=== the receipt's repair Enter goes under the same lock ==="

# pane_await_receipt presses ONE more Enter when no receipt comes. It is the
# one runtime-script keystroke outside pane_send_verified, and an Enter landing inside
# another sender's chunks submits that sender's payload half-typed.
MSG="msg_0123456789abcdef0123456789abcdef"
: > "$PANE_LOG"; cap_reset
PATH="$SHIM:$PATH" PANE_RECEIPT_WAIT_S=0.2 pane_await_receipt sockX botQ "$MSG" >/dev/null 2>&1 || true
r=$(grep -c '^sockX|botQ|key|Enter$' "$PANE_LOG" || true)
assert_eq "a free pane: the repair Enter is pressed once" "1" "$r"

lf=$(lock_file_for sockX botQ)
HOLDER_PID=""
[ -z "$lf" ] || hold_lock "$lf" 30
: > "$PANE_LOG"; cap_reset
PATH="$SHIM:$PATH" PANE_RECEIPT_WAIT_S=0.2 pane_await_receipt sockX botQ "$MSG" >/dev/null 2>"$TMPD/receipt.err" || true
r=$(grep -c '^sockX|botQ|key|Enter$' "$PANE_LOG" || true)
assert_eq "a pane another send holds: the repair Enter is NOT pressed into it" "0" "$r"
cap_refresh; r=$(grep -cE '"reason": ?"recipient-lock-timeout"' "$CAPTURE" || true)
assert_eq "...and the skipped Enter is recorded (recipient-lock-timeout)" "1" "$r"
[ -z "$HOLDER_PID" ] || kill "$HOLDER_PID" 2>/dev/null || true

echo "=== a pane has one lock, however a sender spells it ==="

# The CLI's transport names a pane =<session>: (tmux's exact match); start-bot,
# keepalive, fleet-pulse and the other injectors name it bare. Both spellings
# must take ONE lock, or a delivery and a push to the same pane interleave.
f1=$(lock_file_for sockX botS); f2=$(lock_file_for sockX '=botS:')
# Non-empty first: two empty answers are equal and would pass with no helper at all.
if [ -n "$f1" ] && [ "$f1" = "$f2" ]; then r=same; else r="different ('$f1' vs '$f2')"; fi
assert_eq "=botS: and botS name one lock file" "same" "$r"
lf=$(lock_file_for sockX botS)
HOLDER_PID=""
[ -z "$lf" ] || hold_lock "$lf" 30
: > "$PANE_LOG"; cap_reset
PANE_SEND_LOCK_WAIT_S=1 pane_send_verified sockX '=botS:' "must wait for the bare-named holder" >/dev/null 2>&1 || true
r=$(grep -c '^sockX|=botS:|' "$PANE_LOG" || true)
assert_eq "a send to =botS: waits for a holder that locked botS: nothing typed" "0" "$r"
[ -z "$HOLDER_PID" ] || kill "$HOLDER_PID" 2>/dev/null || true

echo "=== a single key goes under the same lock ==="

# bot interrupt's Escape (supervisor.sh) is a keystroke like any other: pressed
# between another sender's chunks, it acts on that sender's half-typed payload.
: > "$PANE_LOG"; cap_reset
rc=0; pane_send_key sockX botK Escape interrupt >/dev/null 2>&1 || rc=$?
r=$(grep -c '^sockX|botK|key|Escape$' "$PANE_LOG" || true)
assert_eq "a free pane: the key is sent once" "1" "$r"
lf=$(lock_file_for sockX botK)
HOLDER_PID=""
[ -z "$lf" ] || hold_lock "$lf" 30
: > "$PANE_LOG"; cap_reset
rc=0; PANE_SEND_LOCK_WAIT_S=1 pane_send_key sockX botK Escape interrupt >/dev/null 2>&1 || rc=$?
r=$(grep -c '^sockX|botK|key|Escape$' "$PANE_LOG" || true)
assert_eq "a pane another send holds: the key is NOT sent" "0" "$r"
assert_eq "...and the call says the lock refused it (rc 75)" "75" "$rc"
[ -z "$HOLDER_PID" ] || kill "$HOLDER_PID" 2>/dev/null || true

echo ""
echo "=== $PASS/$TOTAL passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
