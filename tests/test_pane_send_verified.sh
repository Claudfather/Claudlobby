#!/usr/bin/env bash
# tests/test_pane_send_verified.sh — verified-send helper tests (#763)
#
# Hermetic: bot_tmux is stubbed after sourcing lib-common, so `send-keys` is
# recorded and `capture-pane` replays a fixture. No tmux server, no bot dir.
#
# The fixtures under tests/fixtures/pane-states/input-*.txt are REAL 80x24
# captures of a live Claude Code pane in each state (paths scrubbed, structure
# byte-preserved). That matters: the property under test is where the input line
# sits relative to the bottom of the pane, and a hand-drawn approximation that
# puts the prompt on the last line would pass while production fails — which is
# exactly how the previous `tail -3` verify shipped dead.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../lib"
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

# Collapse the settle/poll windows so the suite runs fast — and exercise the env
# knobs while doing it. Read per call, not frozen at source time, so a test may
# also change them partway through (the poll-tick case below does).
export PANE_SEND_SETTLE_S=0
export PANE_SEND_VERIFY_TICKS=1

# shellcheck source=../lib/lib-common.sh
. "$LIB_DIR/lib-common.sh"

TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT
SENT_LOG="$TMPD/sent.log"
PANE_SCRIPT="$TMPD/panes"   # newline-separated fixture paths, one per capture

# CONSTRUCTED event destination, suite-wide (#846 instance 3). Any
# pane_send_verified call in this file can emit; with the destination inherited
# from the ambient session, those rows landed in real per-bot and fleet
# ledgers. Every emit now lands under $TMPD by construction.
# The dot makes the marker unholdable by a real bot: compose_bot_conf rejects
# ids outside [A-Za-z0-9_-] (claudlobby/composer.py _SAFE_NAME_RE).
SYNTH_ID="synthetic.paneprobe"
export BOT_DIR="$TMPD/synth-bot" BOT_ID="$SYNTH_ID"
export CLAUDLOBBY_ROOT="$TMPD/synth-root"
mkdir -p "$BOT_DIR/data"

# Stub the single tmux chokepoint. send-keys appends its payload to SENT_LOG;
# capture-pane pops the next fixture from PANE_SCRIPT (repeating the last one),
# so a test can hand the poll loop a different pane on each tick.
bot_tmux() {
    shift  # socket
    case "${1:-}" in
        send-keys)
            shift 2  # send-keys -t
            shift    # session
            printf '%s\n' "$*" >> "$SENT_LOG"
            ;;
        capture-pane)
            local remaining fixture
            remaining=$(cat "$PANE_SCRIPT")
            fixture=$(printf '%s\n' "$remaining" | head -1)
            printf '%s\n' "$remaining" | tail -n +2 > "$PANE_SCRIPT.tmp"
            [ -s "$PANE_SCRIPT.tmp" ] && mv "$PANE_SCRIPT.tmp" "$PANE_SCRIPT" || rm -f "$PANE_SCRIPT.tmp"
            cat "$fixture"
            ;;
    esac
}

# run_send <text> <fixture...> -> echoes the number of send-keys calls made.
# 2 = text + Enter (clean submit). 3 = text + Enter + retry Enter.
run_send() {
    local text="$1"; shift
    : > "$SENT_LOG"
    printf '%s\n' "$@" > "$PANE_SCRIPT"
    pane_send_verified sock "$SYNTH_ID" "$text"
    wc -l < "$SENT_LOG" | tr -d ' '
}

echo "=== pane_input_region: anchors to the input line, not a fixed depth ==="

# The whole bug in one assertion. The literal command IS sitting unsubmitted at
# the input line, and the shipped `tail -3 | grep -F` cannot see it, because the
# box border, hint line and mode footer sit below the input line.
stuck=$(cat "$FIXTURES/input-stuck-literal.txt")
r=$(printf '%s\n' "$stuck" | tail -3 | grep -qF '/claudna:session resume --auto' && echo yes || echo no)
assert_eq "tail -3 does NOT reach the input line (the dead pre-#763 verify)" "no" "$r"
r=$(pane_holds_unsubmitted "$stuck" '/claudna:session resume --auto' && echo yes || echo no)
assert_eq "region-anchored verify DOES reach the input line" "yes" "$r"

# A submitted message is echoed into the transcript with the same glyph, so the
# anchor must take the LAST glyph line. A tail deep enough to reach the input
# line would also reach that echo and retry on a clean submit.
clean=$(cat "$FIXTURES/input-clean-submit.txt")
r=$(printf '%s\n' "$clean" | grep -cE '^[[:space:]]*(>|❯)')
assert_eq "clean-submit pane has a transcript glyph line as well as the input line" "2" "$r"
r=$(pane_input_region "$clean" | grep -cF 'PROBE763TRANSCRIPT' || true)
assert_eq "region excludes the transcript echo of a submitted command" "0" "$r"

# No prompt at all (mid-turn) means nothing is sitting unsubmitted.
r=$(pane_input_region "$(cat "$FIXTURES/busy-spinner.txt")" | wc -c | tr -d ' ')
assert_eq "pane with no prompt glyph yields an empty region" "0" "$r"

echo "=== pane_send_verified: retry fires only on positive evidence ==="

r=$(run_send '/claudna:session resume --auto' "$FIXTURES/input-stuck-literal.txt")
assert_eq "literal text stuck at the input line -> Enter resent" "3" "$r"

# craig's failure: a large payload renders as a collapsed placeholder, so the
# literal text is nowhere in the pane and no text probe can match it.
big="set +H; [BOTCOMMAND] ari | task | $(printf 'filler %.0s' $(seq 1 60))"
r=$(printf '%s\n' "$(cat "$FIXTURES/input-stuck-collapsed-paste.txt")" | grep -cF "${big:0:60}" || true)
assert_eq "collapsed-paste pane contains none of the payload text" "0" "$r"
r=$(run_send "$big" "$FIXTURES/input-stuck-collapsed-paste.txt")
assert_eq "collapsed paste stuck at the input line -> Enter resent" "3" "$r"

r=$(run_send 'PROBE763TRANSCRIPT reply ok' "$FIXTURES/input-clean-submit.txt")
assert_eq "clean submit (text visible in transcript) -> NO spurious Enter" "2" "$r"

r=$(run_send 'QUEUEDPAYLOAD763 follow-up' "$FIXTURES/input-queued-hint.txt")
assert_eq "send queued against a busy pane (TUI hint in box) -> NO spurious Enter" "2" "$r"

r=$(run_send 'anything' "$FIXTURES/busy-spinner.txt")
assert_eq "no prompt glyph (mid-turn) -> NO spurious Enter" "2" "$r"

echo "=== pane_send_verified: the poll gives a slow render time to settle ==="

# Two ticks: still stuck on the first capture, cleared by the second. The old
# fixed post-Enter sleep either waited too long or fired a needless retry.
export PANE_SEND_VERIFY_TICKS=3
r=$(run_send '/claudna:session resume --auto' \
        "$FIXTURES/input-stuck-literal.txt" "$FIXTURES/input-clean-submit.txt")
assert_eq "box clears on a later poll tick -> NO retry" "2" "$r"
export PANE_SEND_VERIFY_TICKS=1

# A zero budget must mean "no verification", not "resend blind". Getting this
# backwards would make the cheap setting the most wasteful one AND fire a ghost
# Enter into an idle pane on every send.
export PANE_SEND_VERIFY_TICKS=0
r=$(run_send '/claudna:session resume --auto' "$FIXTURES/input-stuck-literal.txt")
assert_eq "PANE_SEND_VERIFY_TICKS=0 disables the verify (no blind resend)" "2" "$r"
export PANE_SEND_VERIFY_TICKS=1

echo "=== the retry is observable (a silent retry is how the old one hid) ==="

# emit_fleet_event writes to $BOT_DIR/data/events when BOT_DIR resolves. Count
# without an unmatched glob or a zero-match grep aborting the suite under
# pipefail — a missing event must report FAIL, not kill the run.
count_events() { cat "$BOT_DIR"/data/events/*.jsonl 2>/dev/null | grep -c "$1" || true; }
# Zero the ledger: earlier run_send calls already emitted retries into it and
# the counts below assert exact totals. emit_fleet_event re-mkdirs on write.
rm -rf "$BOT_DIR/data/events"
run_send '/claudna:session resume --auto' "$FIXTURES/input-stuck-literal.txt" >/dev/null
r=$(count_events '"type":"send_retry"')
assert_eq "a fired retry emits a send_retry event" "1" "$r"
r=$(count_events '"reason":"enter-swallowed"')
assert_eq "the event names the reason" "1" "$r"

# A clean submit must stay silent — otherwise the ledger fills with non-events.
run_send 'PROBE763TRANSCRIPT reply ok' "$FIXTURES/input-clean-submit.txt" >/dev/null
r=$(count_events '"type":"send_retry"')
assert_eq "a clean submit emits NO send_retry event" "1" "$r"

echo "=== probe cap: a wrapped payload is still detected ==="

# The STARTUP_PROMPT shape — a payload too long for one rendered line, wrapped
# across the input box but not long enough to collapse into a placeholder. The
# probe cap is what makes this detectable: the full text spans a wrap point and
# matches no single line, so a verify probing the whole payload sees nothing.
wrapped='set +H; PROBE763WRAP You just started up. Read your CLAUDE.md, then post a brief ready message and wait for task assignments.'
wpane=$(cat "$FIXTURES/input-stuck-wrapped.txt")
r=$(printf '%s\n' "$wpane" | grep -qF "$wrapped" && echo yes || echo no)
assert_eq "the full payload matches no single rendered line (it is wrapped)" "no" "$r"
r=$(printf '%s\n' "$wpane" | grep -qF "${wrapped:0:$_PANE_PROBE_MAX_CHARS}" && echo yes || echo no)
assert_eq "the capped probe DOES match a rendered line" "yes" "$r"
r=$(run_send "$wrapped" "$FIXTURES/input-stuck-wrapped.txt")
assert_eq "wrapped payload stuck at the input line -> Enter resent" "3" "$r"

echo ""
echo "=== $PASS/$TOTAL passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
