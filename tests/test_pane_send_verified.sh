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
# ORDER_LOG records the INTERLEAVING of captures and sends, which counting
# send-keys cannot express. #860 is an ordering defect — the payload was sent
# into a pane whose input box did not exist yet — so the property under test is
# "no send precedes a drawn capture", not "how many sends happened".
ORDER_LOG="$TMPD/order.log"
: > "$ORDER_LOG"

# Compress the #860 readiness budget suite-wide. In production it is 45s (0.5s x
# 90), sized off the 10-19s measured box draw; here every capture is a stub, so
# the wall-clock wait buys nothing and any glyph-less fixture would otherwise
# make the suite sit out the full budget before its assertion runs.
export PANE_READY_POLL_S=0.02 PANE_READY_TICKS=6
# And the recovery budget for a box that never drew (production 60 x 0.2s = 12s).
export PANE_RECOVER_TICKS=2

bot_tmux() {
    shift  # socket
    case "${1:-}" in
        send-keys)
            shift 2  # send-keys -t
            shift    # session
            printf '%s\n' "$*" >> "$SENT_LOG"
            printf 'send\n' >> "$ORDER_LOG"
            ;;
        capture-pane)
            local remaining fixture
            remaining=$(cat "$PANE_SCRIPT")
            fixture=$(printf '%s\n' "$remaining" | head -1)
            printf '%s\n' "$remaining" | tail -n +2 > "$PANE_SCRIPT.tmp"
            [ -s "$PANE_SCRIPT.tmp" ] && mv "$PANE_SCRIPT.tmp" "$PANE_SCRIPT" || rm -f "$PANE_SCRIPT.tmp"
            # Classify for the order log by the same signal the gate uses, so the
            # log cannot disagree with the code about what "drawn" means.
            if [ -n "$(pane_input_region "$(cat "$fixture")")" ]; then
                printf 'capture:drawn\n' >> "$ORDER_LOG"
            else
                printf 'capture:predraw\n' >> "$ORDER_LOG"
            fi
            cat "$fixture"
            ;;
    esac
}

# run_send <text> <fixture...> -> echoes the number of send-keys calls made.
# 2 = text + Enter (clean submit). 3 = text + Enter + retry Enter.
run_send() {
    local text="$1"; shift
    : > "$SENT_LOG"; : > "$ORDER_LOG"
    printf '%s\n' "$@" > "$PANE_SCRIPT"
    pane_send_verified sock "$SYNTH_ID" "$text"
    wc -l < "$SENT_LOG" | tr -d ' '
}

# Did any send happen before the first capture that showed a drawn input box?
# "none" is the healthy answer; "sent-blind" is #860.
send_before_draw() {
    awk '/^send$/ { print "sent-blind"; exit }
         /^capture:drawn$/ { print "none"; exit }
         END { if (!NR) print "none" }' "$ORDER_LOG"
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

echo "=== pane_send_verified: never sends into a pane with no input box (#860) ==="

# #837 closed the POST-draw swallow and left the PRE-draw loss uncovered. The
# two are not the same failure: post-draw the text IS in the box and only Enter
# was eaten, so resending Enter repairs it; pre-draw the text never arrived at
# all, and no amount of Enter helps. Worse, the verify REPORTS SUCCESS —
# pane_holds_unsubmitted reads a glyph-less pane as "nothing unsubmitted", so
# the poll returns 0 on its first tick and the boot looks clean.
#
# The discriminator is the input box itself; the alternatives were measured and
# rejected beside the verdict constants in lib-common.sh. predraw-empty.txt is
# suite-owned rather than reusing unknown-blank.txt, which belongs to
# test_keepalive_classify's UNKNOWN cases — an edit made to serve classify_pane
# would silently change what these assertions mean.
r=$(run_send 'STARTUP860 payload' \
    "$FIXTURES/predraw-empty.txt" "$FIXTURES/predraw-empty.txt" \
    "$FIXTURES/idle-prompt.txt" "$FIXTURES/input-clean-submit.txt")
assert_eq "pre-draw pane: payload is NOT sent before the box is drawn" "none" "$(send_before_draw)"
assert_eq "pre-draw pane: payload still lands once the box appears" "2" "$r"

# A drawn pane must not pay for the gate: one capture, then send.
r=$(run_send 'PROBE763TRANSCRIPT reply ok' "$FIXTURES/input-clean-submit.txt")
assert_eq "already-drawn pane: no send precedes the draw check" "none" "$(send_before_draw)"
assert_eq "already-drawn pane: still exactly two sends" "2" "$r"

# The gate is best-effort, never a block: a pane that never draws must still get
# the payload rather than hanging start-bot or silently dropping it.
# Zero the ledger first — earlier glyph-less cases in this file exhaust the same
# budget and emit too, and this assertion counts an exact total.
rm -rf "$BOT_DIR/data/events"
r=$(run_send 'NEVERDRAWN860' "$FIXTURES/predraw-empty.txt")
assert_eq "box never drawn: payload is still sent (best-effort, not dropped)" "2" "$r"
r=$(cat "$BOT_DIR"/data/events/*.jsonl 2>/dev/null | grep -c '"reason":"input-box-never-drawn"' || true)
assert_eq "box never drawn: emits evidence rather than failing silently" "1" "$r"

# The wait is OPT-IN, which splits the contract in two and both halves need
# pinning. Default-off keeps it off the paths where it is a hazard rather than a
# safeguard: defaulting it ON put a 45s block on report-back.sh (via
# bot_tmux_send) and blew through pre-stop-handoff's documented 30s bound,
# serially, on a fleet-wide restart. And an opt-in that a caller must REMEMBER is
# the failure mode #844 was, so the one caller that needs it is asserted here
# rather than trusted — a cold-boot injector that silently stops arming this is
# #860 all over again, and nothing else in the suite would notice.
# Ask the FUNCTION, not the expansion. The earlier form here echoed
# "${PANE_READY_TICKS:-0}" from a subshell, which is 0 by definition — it never
# called pane_await_input_box, so it could not have noticed the default flipping
# inside it, which is the only thing that matters.
r=$(env -u PANE_READY_TICKS bash -c '
    . "$1"/lib-common.sh
    printf "%s\n" "$(pane_await_input_box sock nosuchsession)"' _ "$LIB_DIR")
assert_eq "unarmed: the wait is off inside the function, not just in the env" "unwaited" "$r"

# And the arming is SCOPED. A bare `export` would outlive the two cold-boot sends
# and hand the 45s budget to bridge_bringup_verify's failure alert, which targets
# the MANAGER pane through bot_tmux_send — a 45s block plus a recovery poll inside
# ExecStart, for an alert about this bot being unreachable. Assert the property
# (per-call prefix, no process-wide export) rather than one literal line, so a
# requote or rename does not redden a behaviourally identical change.
r=$(grep -cE '^[[:space:]]*PANE_READY_TICKS="\$_PANE_READY_TICKS_BOOT"[[:space:]]*\\?$' \
    "$SCRIPT_DIR/../lib/start-bot.sh" || true)
assert_eq "start-bot arms the wait per call, once for each cold-boot send" "2" "$r"
r=$(grep -cE '^[[:space:]]*export[[:space:]]+PANE_READY_TICKS' "$SCRIPT_DIR/../lib/start-bot.sh" || true)
assert_eq "start-bot never exports it process-wide (it would leak past the sends)" "0" "$r"

echo "=== the readiness verdict: what was observed, not just pass/fail (#860) ==="

# The gate above is a PRE-condition, and a pre-condition can only sidestep the
# ambiguity on the one path that arms it. The verify downstream still has to
# classify a glyph-less pane, and that is what these assertions cover.
#
# Why a verdict at all: "box present" and "budget expired" and "looked and could
# not tell" and "never looked" are four different observations, and the original
# gate collapsed them into rc 0/1. A pass/fail cannot carry which — so the verify
# had nothing to read, and fell back to the assumption that an empty box means a
# submitted payload. Assert the classifier directly rather than only its side
# effects: an oracle whose output is never inspected is how the mid-turn
# assumption survived two fix attempts wearing a passing test.
rep() { local n="$1" f="$2"; while [ "$n" -gt 0 ]; do printf '%s\n' "$f"; n=$((n - 1)); done; }
verdict() { printf '%s\n' "$@" > "$PANE_SCRIPT"; pane_await_input_box sock "$SYNTH_ID"; }

assert_eq "a drawn box reports 'drawn'" "drawn" "$(verdict "$FIXTURES/idle-prompt.txt")"
assert_eq "an empty pane through the whole budget reports 'never-drawn'" \
    "never-drawn" "$(verdict "$FIXTURES/predraw-empty.txt")"
# Content without a glyph is NOT pre-draw and NOT confirmed-drawn. A TUI caught
# mid-paint looks like this, and so does a dead shell; one capture cannot tell
# them apart, so the verdict says so instead of guessing.
assert_eq "content but no glyph reports 'unverified'" \
    "unverified" "$(verdict "$FIXTURES/busy-spinner.txt")"
assert_eq "an unarmed caller reports 'unwaited' (no observation, no opinion)" \
    "unwaited" "$(PANE_READY_TICKS=0 verdict "$FIXTURES/predraw-empty.txt")"

echo "=== glyph-less at verify: the latch decides, not the frame (#860) ==="

# THE defect, stated as a pair. Both runs below hand the verify a pane with no
# input glyph. Pre-fix they were indistinguishable — pane_holds_unsubmitted reads
# a glyph-less pane as "nothing unsubmitted" and the poll returns SUCCESS on its
# first tick — so the code took the mid-turn reading in both cases, and the suite
# asserted that reading as correct ("no prompt glyph (mid-turn) -> NO spurious
# Enter"). That assertion is true. It is also what locked the bug in, which is why
# more coverage of it could never have found this.
#
# The two causes have opposite correct responses, so no single predicate over the
# current frame can serve. What separates them is a second signal with the
# opposite blind spot: the frame knows only the present, the latch knows only
# whether a box was EVER confirmed.

# (a) Box confirmed, then glyph-less at verify -> mid-turn. The payload went into
# a box that demonstrably existed, so its absence means submitted. No resend.
r=$(run_send 'MIDTURN860 payload' \
    "$FIXTURES/idle-prompt.txt" "$FIXTURES/busy-spinner.txt")
assert_eq "drawn box then glyph-less verify -> submitted, no resend" "2" "$r"

# (b) Box never drawn, then a box appears holding nothing -> the keystrokes were
# typed at a TUI that did not exist and are gone. Resending Enter repairs nothing
# (there is no text in the box to submit), so the PAYLOAD goes again.
# Pre-fix this returned success on tick 1 and the prompt was lost silently.
rm -rf "$BOT_DIR/data/events"
r=$(run_send 'LOSTPAYLOAD860' \
    $(rep "$PANE_READY_TICKS" "$FIXTURES/predraw-empty.txt") "$FIXTURES/idle-prompt.txt")
assert_eq "never-drawn then a box appears empty -> full payload resent" "4" "$r"
r=$(cat "$BOT_DIR"/data/events/*.jsonl 2>/dev/null | grep -c '"reason":"resent-after-box-drew"' || true)
assert_eq "the recovery is on the ledger (an invisible repair is how this hid)" "1" "$r"

# The resend must be the payload, not a bare Enter: a lost send has nothing in the
# box for an Enter to submit. Distinguishes this repair from #837's.
r=$(grep -c '^LOSTPAYLOAD860$' "$SENT_LOG" || true)
assert_eq "the resend carries the payload itself, twice in total" "2" "$r"

echo "=== the recovery needs positive evidence too (#860) ==="

# Symmetric discipline to pane_holds_unsubmitted: never act on an absence. If the
# payload is visible ANYWHERE in the frame it did arrive, so resending would
# double-deliver a startup prompt. The transcript echo is the evidence — a
# submitted payload leaves the input box and is rendered above it.
r=$(run_send 'PROBE763TRANSCRIPT reply ok' \
    $(rep "$PANE_READY_TICKS" "$FIXTURES/predraw-empty.txt") "$FIXTURES/input-clean-submit.txt")
assert_eq "never-drawn but the payload shows in the transcript -> NOT resent" "2" "$r"

# A payload past the paste threshold renders as [Pasted text #N], so its literal
# text is nowhere in the pane even when it landed perfectly. Matching on text
# alone would read every landed paste as a vanished one and resend it.
#
# This case also keeps the two repairs from blurring. The paste DID arrive and is
# sitting in the box unsubmitted, so the correct repair is #837's — one more Enter
# — even though the box was never confirmed before the send. Three sends, not
# four: the Enter fires, the payload does not go again.
big="set +H; [BOTCOMMAND] ari | task | $(printf 'filler %.0s' $(seq 1 60))"
r=$(run_send "$big" \
    $(rep "$PANE_READY_TICKS" "$FIXTURES/predraw-empty.txt") "$FIXTURES/input-stuck-collapsed-paste.txt")
assert_eq "never-drawn but a collapsed paste landed -> Enter resent, not the payload" "3" "$r"
r=$(grep -cF "$big" "$SENT_LOG" || true)
assert_eq "the collapsed payload is sent exactly once (no double-delivery)" "1" "$r"

# A box that never appears at all: nothing to recover and nothing to submit. The
# post-budget Enter must NOT fire — it would spend a send on a pane that cannot
# receive it and file a send_retry, misattributing a pre-draw loss as a post-draw
# swallow. fleet-pulse reads those rows; the two must not blur.
rm -rf "$BOT_DIR/data/events"
r=$(run_send 'NEVERAPPEARS860' "$FIXTURES/predraw-empty.txt")
assert_eq "box never appears -> no phantom Enter retry" "2" "$r"
r=$(cat "$BOT_DIR"/data/events/*.jsonl 2>/dev/null | grep -c '"reason":"enter-swallowed"' || true)
assert_eq "box never appears -> no send_retry misattribution" "0" "$r"
r=$(cat "$BOT_DIR"/data/events/*.jsonl 2>/dev/null | grep -c '"reason":"input-box-never-drawn"' || true)
assert_eq "box never appears -> the loss IS recorded as send_blind" "1" "$r"

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

echo "=== wrapped payloads are detected regardless of WHERE the box breaks (#1082) ==="

# The STARTUP_PROMPT shape — too long for one rendered line, wrapped across the
# input box but not long enough to collapse into a placeholder.
wrapped='set +H; PROBE763WRAP You just started up. Read your CLAUDE.md, then post a brief ready message and wait for task assignments.'
wpane=$(cat "$FIXTURES/input-stuck-wrapped.txt")
r=$(printf '%s\n' "$wpane" | grep -qF "$wrapped" && echo yes || echo no)
assert_eq "the full payload matches no single rendered line (it is wrapped)" "no" "$r"
r=$(pane_holds_unsubmitted "$wpane" "$wrapped" && echo yes || echo no)
assert_eq "reversed containment still detects it (late wrap)" "yes" "$r"
r=$(run_send "$wrapped" "$FIXTURES/input-stuck-wrapped.txt")
assert_eq "wrapped payload stuck at the input line -> Enter resent" "3" "$r"

# THE REGRESSION THIS FILE PREVIOUSLY MISSED, and the reason it missed it.
# The block above passed under the retired 60-char prefix probe — but only
# because its fixture happens to wrap LATE: that glyph line carries 76 chars, so
# a 60-char prefix fits on it. The box WORD-wraps, so the break point is a
# property of the text, not a constant. This fixture wraps at 47 chars, which is
# the ordinary case for a real dispatch, and the prefix probe cannot see it.
#
# A test named for the right property, exercising the right mechanism, on a
# fixture structurally incapable of exhibiting the failure. Keep BOTH fixtures:
# the pair is the evidence that the wrap point moves.
# The payload carries an EM-DASH (U+2014), deliberately. Every fixture em-dash
# before this one sat in pane chrome, never on a payload line, and no test
# payload contained one at all — so the matcher's handling of a multibyte
# character INSIDE the string it compares was entirely unexercised. This code
# territory is exactly where that bites: the predicate strips U+276F and U+00A0
# and compares bytes, and a detector was corrupted this week by gsub-ing the
# box-drawing U+2500 while the live payload carried U+2014. ASCII-only fixtures
# cannot catch that class. Keep the em-dash.
early='set +H; PROBE1082EARLY Confirm the counts — then report DEAD/ALIVE/UNDECIDED with evidence classes attached'
epane=$(cat "$FIXTURES/input-stuck-wrapped-early.txt")
r=$(printf '%s\n' "$epane" | grep -qF "${early:0:60}" && echo yes || echo no)
assert_eq "a 60-char prefix probe does NOT match an early wrap (the bug)" "no" "$r"
r=$(pane_holds_unsubmitted "$epane" "$early" && echo yes || echo no)
assert_eq "reversed containment DOES detect it (early wrap)" "yes" "$r"
r=$(run_send "$early" "$FIXTURES/input-stuck-wrapped-early.txt")
assert_eq "early-wrapped payload stuck -> Enter resent" "3" "$r"

# The direction that must never regress: an EMPTY box is not evidence of a held
# payload. The empty string is a substring of everything, so reversed
# containment without a floor would fire a ghost Enter into an idle pane.
r=$(pane_holds_unsubmitted "$(printf '❯ \n────\n  auto mode on\n')" "$early" && echo yes || echo no)
assert_eq "an EMPTY box is NOT held (no ghost Enter)" "no" "$r"

echo ""
echo "=== $PASS/$TOTAL passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
