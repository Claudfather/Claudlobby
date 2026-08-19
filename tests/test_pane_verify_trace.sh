#!/usr/bin/env bash
# tests/test_pane_verify_trace.sh — per-tick verify instrumentation (#1236).
#
# INSTRUMENT ONLY. pane_send_verified reproduces the strand at ~1-in-3 under
# load but does not explain it: we know the verify loop exits clean on the first
# tick where pane_holds_unsubmitted returns false, and we do NOT know why the
# predicate returned false. Three candidates, none eliminated: render lag at
# tick 1, the _PANE_MIN_VISIBLE_MATCH floor, chrome the NBSP stripper misses.
#
# These tests pin two things. First, that the trace records enough per tick to
# TELL THOSE THREE APART. Second, and load-bearing given the hazard that
# instrumenting a race can move it, that the trace changes NOTHING about the
# decisions and costs nothing when it is off.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../lib"
FIXTURES="$SCRIPT_DIR/fixtures/pane-states"
PASS=0; FAIL=0; TOTAL=0

assert_eq() {
    TOTAL=$((TOTAL + 1))
    local desc="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then echo "  PASS: $desc"; PASS=$((PASS + 1));
    else echo "  FAIL: $desc (expected '$expected', got '$actual')"; FAIL=$((FAIL + 1)); fi
}

export PANE_SEND_SETTLE_S=0 PANE_SEND_VERIFY_TICKS=2
export PANE_READY_POLL_S=0.02 PANE_READY_TICKS=6 PANE_RECOVER_TICKS=2
export _PANE_VERIFY_POLL_S=0

# shellcheck source=../lib/lib-common.sh
. "$LIB_DIR/lib-common.sh"

TMPD=$(mktemp -d); trap 'rm -rf "$TMPD"' EXIT
SENT_LOG="$TMPD/sent.log"; PANE_SCRIPT="$TMPD/panes"
export BOT_DIR="$TMPD/synth-bot" BOT_ID="synthetic.traceprobe"
export CLAUDLOBBY_ROOT="$TMPD/synth-root"
mkdir -p "$BOT_DIR/data"

bot_tmux() {
    shift
    case "${1:-}" in
        send-keys) shift 3; printf '%s\n' "$*" >> "$SENT_LOG" ;;
        capture-pane)
            local remaining fixture
            remaining=$(cat "$PANE_SCRIPT")
            fixture=$(printf '%s\n' "$remaining" | head -1)
            printf '%s\n' "$remaining" | tail -n +2 > "$PANE_SCRIPT.tmp"
            [ -s "$PANE_SCRIPT.tmp" ] && mv "$PANE_SCRIPT.tmp" "$PANE_SCRIPT" || rm -f "$PANE_SCRIPT.tmp"
            cat "$fixture"
            ;;
        *) return 0 ;;
    esac
}

echo "== trace is off by default =="
: > "$SENT_LOG"; printf '%s\n' "$FIXTURES/input-clean-submit.txt" > "$PANE_SCRIPT"
unset PANE_VERIFY_TRACE
pane_send_verified sock sess "hello world payload" >/dev/null 2>&1 || true
assert_eq "nothing is written when the knob is unset" "0" "$(find "$TMPD" -name 'tick-*.pane' | wc -l | tr -d ' ')"

echo "== decisions are identical with the trace on =="
: > "$SENT_LOG"; printf '%s\n' "$FIXTURES/input-stuck-literal.txt" > "$PANE_SCRIPT"
pane_send_verified sock sess "hello world payload" >/dev/null 2>&1 || true
sends_off=$(wc -l < "$SENT_LOG" | tr -d ' ')
: > "$SENT_LOG"; printf '%s\n' "$FIXTURES/input-stuck-literal.txt" > "$PANE_SCRIPT"
export PANE_VERIFY_TRACE="$TMPD/trace1.jsonl"
pane_send_verified sock sess "hello world payload" >/dev/null 2>&1 || true
sends_on=$(wc -l < "$SENT_LOG" | tr -d ' ')
assert_eq "the same keystrokes are sent with the trace on as off" "$sends_off" "$sends_on"
unset PANE_VERIFY_TRACE

echo "== a tick record carries what tells the three candidates apart =="
: > "$SENT_LOG"; printf '%s\n' "$FIXTURES/input-clean-submit.txt" > "$PANE_SCRIPT"
TRACE_DIR="$TMPD/trace2"; export PANE_VERIFY_TRACE="$TRACE_DIR"
pane_send_verified sock sess "hello world payload" >/dev/null 2>&1 || true
unset PANE_VERIFY_TRACE
rec=$(pane_trace_render "$TRACE_DIR" | head -1)
for f in tick box held region_present region_lines payload_len floor candidate lines pane_b64; do
    case "$rec" in *"\"$f\""*) got=yes ;; *) got=no ;; esac
    assert_eq "rendered record carries $f" "yes" "$got"
done

echo "== the hot path stores RAW frames, so the analysis really is offline =="
# The first design analysed inline and cost 202ms per tick against a 200ms poll
# -- it more than doubled the very interval under investigation. The tick must
# therefore write bytes and nothing else; if a tick file is ever anything but a
# byte-identical copy of the frame, the cost has crept back onto the hot path.
first_pane=$(cat "$TRACE_DIR/tick-1.pane" 2>/dev/null || printf 'MISSING')
expected_pane=$(cat "$FIXTURES/input-clean-submit.txt")
assert_eq "tick-1.pane is the frame verbatim, not a derived record" \
    "$(printf '%s' "$expected_pane" | cksum)" "$(printf '%s' "$first_pane" | cksum)"
assert_eq "the tick file holds no derived fields" "no" \
    "$(case "$first_pane" in *candidate*|*ge_floor*|*substr*) echo yes ;; *) echo no ;; esac)"

echo "== each candidate classifies distinctly =="
# no-region: pre-draw pane, no glyph at all -> the render-lag shape
printf 'starting up\nno box yet\n' > "$TMPD/predraw.txt"
assert_eq "a pane with no input box classifies no-region" "no-region" \
    "$(_pane_trace_candidate "$(cat "$TMPD/predraw.txt")" "hello world payload")"
# below-floor: a box holding fewer than _PANE_MIN_VISIBLE_MATCH chars of payload
printf '%s\n' "> hel" > "$TMPD/short.txt"
assert_eq "a box below the 12-char floor classifies below-floor" "below-floor" \
    "$(_pane_trace_candidate "$(cat "$TMPD/short.txt")" "hello world payload")"
# empty-box: a drawn box with nothing after the glyph. Split from below-floor
# on purpose -- this is render-lag-or-submitted, NOT the floor candidate.
printf '%s\n' "> " > "$TMPD/empty.txt"
assert_eq "a drawn but empty box classifies empty-box" "empty-box" \
    "$(_pane_trace_candidate "$(cat "$TMPD/empty.txt")" "hello world payload")"
# not-substring: a long line in the box that is not part of the payload
printf '%s\n' "> totally unrelated chrome text here" > "$TMPD/chrome.txt"
assert_eq "a long non-payload line classifies not-substring" "not-substring" \
    "$(_pane_trace_candidate "$(cat "$TMPD/chrome.txt")" "hello world payload")"
# held: the payload really is there
printf '%s\n' "> hello world payload" > "$TMPD/held.txt"
assert_eq "a box holding the payload classifies held" "held" \
    "$(_pane_trace_candidate "$(cat "$TMPD/held.txt")" "hello world payload")"

echo "== the trace never disagrees with the predicate it is explaining =="
# The whole instrument is worthless if its reconstruction drifts from the real
# decision. Assert agreement across every real pane fixture in the corpus.
mismatch=0; checked=0
for fx in "$FIXTURES"/*.txt; do
    pane=$(cat "$fx")
    for payload in "hello world payload" "/claudna:session resume --auto" "short"; do
        checked=$((checked + 1))
        if pane_holds_unsubmitted "$pane" "$payload"; then real=held; else real=not-held; fi
        cand=$(_pane_trace_candidate "$pane" "$payload")
        [ "$cand" = "held" ] && derived=held || derived=not-held
        [ "$derived" = "$real" ] || { mismatch=$((mismatch + 1)); echo "    drift on $(basename "$fx") / '$payload': real=$real derived=$cand"; }
    done
done
assert_eq "trace verdict agrees with pane_holds_unsubmitted on all $checked pane/payload pairs" "0" "$mismatch"

echo
echo "TOTAL=$TOTAL PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
