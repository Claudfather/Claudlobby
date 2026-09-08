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
#
# RUN IT UNDER `/bin/bash`. tests/test_sh_suites.py drives every suite through
# `shutil.which("bash")`, which is the right default for Linux; on macOS the
# fleet's own doors run under `/bin/bash`, which is 3.2, and that is the shell
# whose parameter expansion and locale behaviour the byte-splitter below
# depends on. `/bin/bash tests/test_pane_send_verified.sh`.
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
# The inter-chunk settle (#1493) too. In production it is 0.15s between chunks,
# sized to let the pty reader drain; here every send is a stub, so the wall clock
# buys nothing — and setting it to 0 exercises the knob's read path while keeping
# the suite inside test_sh_suites.py's 120s bound.
export PANE_SEND_CHUNK_SETTLE_S=0

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
# F18 closure R1: emit_fleet_event writes no per-bot event file any more — every
# fleet event goes through lib/plane-emit.sh to the plane. The suite stays
# hermetic (no plane, no daemon) by pointing the shim at a CAPTURING cold rung:
# the socket rung fails on a path nothing listens on, the stub below receives
# the finalized batch file as the shim's LAST argument and appends its contents
# to CAPTURE, and the assertions grep that file exactly as they grepped the
# ledger. FLEET_NAME anchors the rows on the synthetic bot (a door records
# nothing without a fleet).
export FLEET_NAME="synthetic-fleet"
export PLANE_SOCKET="$TMPD/no-daemon.sock"
CAPTURE="$TMPD/plane-capture.jsonl"
: > "$CAPTURE"
PLANE_EMIT_CLI="$TMPD/capture-cli"
export PLANE_EMIT_CLI
printf '%s\n' '#!/bin/bash' 'f="${@: -1}"' 'cat "$f" >> "'"$CAPTURE"'"' 'echo >> "'"$CAPTURE"'"' > "$PLANE_EMIT_CLI"
chmod +x "$PLANE_EMIT_CLI"
# The finalized batch is re-serialized by the shim (json.dumps: a space after
# each colon), so every reason/event match below tolerates either spacing.

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

# #1493: every send-keys INVOCATION, verbatim, and every keystroke chunk as its
# own file so a byte count and a first-byte inspection are exact. SENT_LOG keeps
# recording the payload alone — the `-l --` prefix is stripped below — so every
# assertion written before the chunking still reads what it always read, and the
# chunk-shaped assertions read CHUNK_DIR instead of re-parsing a text log.
RAW_LOG="$TMPD/raw.log"
CHUNK_DIR="$TMPD/chunks"
mkdir -p "$CHUNK_DIR"
CHUNK_N=0

bot_tmux() {
    shift  # socket
    case "${1:-}" in
        send-keys)
            printf '%s\n' "$*" >> "$RAW_LOG"
            shift 2  # send-keys -t
            shift    # session
            # The chunked keystroke form. Recorded raw (printf %s, no newline)
            # so a chunk's byte count is the file's byte count.
            if [ "${1:-}" = "-l" ] && [ "${2:-}" = "--" ]; then
                shift 2
                CHUNK_N=$((CHUNK_N + 1))
                printf '%s' "${1:-}" > "$CHUNK_DIR/$(printf '%03d' "$CHUNK_N")"
            fi
            printf '%s\n' "$*" >> "$SENT_LOG"
            printf 'send\n' >> "$ORDER_LOG"
            # A tmux that fails MID-PAYLOAD (chunk O fold, F6). Recorded first,
            # so the failing chunk is still visible in the log; the caller sees
            # the non-zero exit a dying pane would give it.
            if [ -n "${FAIL_ON_CHUNK:-}" ] && [ "$CHUNK_N" = "$FAIL_ON_CHUNK" ]; then
                return 1
            fi
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
    : > "$SENT_LOG"; : > "$ORDER_LOG"; : > "$RAW_LOG"
    rm -f "$CHUNK_DIR"/*; CHUNK_N=0
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
# Zero the capture first — earlier glyph-less cases in this file exhaust the same
# budget and emit too, and this assertion counts an exact total.
: > "$CAPTURE"
r=$(run_send 'NEVERDRAWN860' "$FIXTURES/predraw-empty.txt")
assert_eq "box never drawn: payload is still sent (best-effort, not dropped)" "2" "$r"
r=$(grep -cE '"reason": ?"input-box-never-drawn"' "$CAPTURE" || true)
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
: > "$CAPTURE"
r=$(run_send 'LOSTPAYLOAD860' \
    $(rep "$PANE_READY_TICKS" "$FIXTURES/predraw-empty.txt") "$FIXTURES/idle-prompt.txt")
assert_eq "never-drawn then a box appears empty -> full payload resent" "4" "$r"
r=$(grep -cE '"reason": ?"resent-after-box-drew"' "$CAPTURE" || true)
assert_eq "the recovery is on the plane (an invisible repair is how this hid)" "1" "$r"

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
: > "$CAPTURE"
r=$(run_send 'NEVERAPPEARS860' "$FIXTURES/predraw-empty.txt")
assert_eq "box never appears -> no phantom Enter retry" "2" "$r"
r=$(grep -cE '"reason": ?"enter-swallowed"' "$CAPTURE" || true)
assert_eq "box never appears -> no send_retry misattribution" "0" "$r"
r=$(grep -cE '"reason": ?"input-box-never-drawn"' "$CAPTURE" || true)
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

# emit_fleet_event lands every event on the plane through the shim, whose cold
# rung is the capturing stub above. Count without a zero-match grep aborting the
# suite under pipefail — a missing event must report FAIL, not kill the run.
# The batch carries the event name as payload.event and the caller's data
# verbatim inside payload.data.data, spacing per the re-serialization.
count_events() { grep -cE "$1" "$CAPTURE" || true; }
# Zero the capture: earlier run_send calls already emitted retries into it and
# the counts below assert exact totals.
: > "$CAPTURE"
run_send '/claudna:session resume --auto' "$FIXTURES/input-stuck-literal.txt" >/dev/null
r=$(count_events '"event": ?"send_retry"')
assert_eq "a fired retry emits a send_retry event" "1" "$r"
r=$(count_events '"reason": ?"enter-swallowed"')
assert_eq "the event names the reason" "1" "$r"

# A clean submit must stay silent — otherwise the plane fills with non-events.
run_send 'PROBE763TRANSCRIPT reply ok' "$FIXTURES/input-clean-submit.txt" >/dev/null
r=$(count_events '"event": ?"send_retry"')
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

echo "=== the payload crosses the pty in chunks, never in one write (#1493) ==="

# THE defect, in one property. A single send-keys hands the whole payload to the
# pane's pty in one go; the macOS input queue holds 1024 bytes and, with IMAXBEL
# cleared by cfmakeraw, FLUSHES on overflow rather than dropping the incoming
# byte — so a reader that has not drained the first 1 KB loses it. Measured on
# the estate: under 1 KB, 182 of 182 sends arrived whole; over 1 KB, 86 of 180,
# with 85 arriving TAIL ONLY. Every one recorded pane_submitted.
#
# What is asserted is the SHAPE of the crossing, because that is the only half a
# stub can see. That the shape fixes the loss is measured by
# lib/send-size-probe.sh against a real `claude` on a real pty; a hermetic suite
# cannot reproduce a tty race and must not pretend to.

# Byte counts under LC_ALL=C throughout: the cap is a BYTE cap, and a character
# count would agree with it only for ASCII.
chunk_bytes() { LC_ALL=C wc -c < "$1" | tr -d ' '; }
chunk_count() { ls "$CHUNK_DIR" 2>/dev/null | wc -l | tr -d ' '; }
# First byte of a chunk as a decimal, for the character-boundary assertion.
chunk_first_byte() { od -An -tu1 -N1 < "$1" | tr -d ' '; }

payload2500=$(printf 'x%.0s' $(seq 1 2500))
r=$(run_send "$payload2500" "$FIXTURES/input-clean-submit.txt")
# ceil(2500/900) = 3 chunks, then exactly one Enter.
assert_eq "a 2500-byte payload becomes 3 keystroke chunks" "3" "$(chunk_count)"
assert_eq "a 2500-byte payload is 3 chunks + 1 Enter, no more" "4" "$r"
r=$(grep -c '^Enter$' "$SENT_LOG" || true)
assert_eq "exactly one Enter, after the last chunk (not one per chunk)" "1" "$r"
# Every chunk goes as `-l --`: -l because a chunk that spells a tmux key name
# would otherwise be sent as that key, -- because one starting with `-` would be
# read as a flag.
r=$(grep -c -- '-l --' "$RAW_LOG" || true)
assert_eq "every keystroke chunk is sent literally (-l --)" "3" "$r"
r=$(grep -c -- '-l' "$RAW_LOG" || true)
assert_eq "the Enter is NOT sent with -l (it must stay a key name)" "3" "$r"

over=0
for f in "$CHUNK_DIR"/*; do
    [ "$(chunk_bytes "$f")" -le 900 ] || over=$((over + 1))
done
assert_eq "no chunk exceeds the 900-byte cap" "0" "$over"

# The property that makes the whole thing safe: the pane receives exactly what
# the caller passed, byte for byte. A chunker that drops or duplicates a byte
# trades a truncation for a corruption.
cat "$CHUNK_DIR"/* > "$TMPD/rejoined"
printf '%s' "$payload2500" > "$TMPD/original"
r=$(cmp -s "$TMPD/original" "$TMPD/rejoined" && echo same || echo differs)
assert_eq "the chunks concatenate back to the payload, byte for byte" "same" "$r"

echo "=== a multibyte character is never split across chunks (#1493) ==="

# Dispatch bodies carry em dashes, arrows and check marks routinely, and a byte
# cap lands mid-character whenever the boundary is not lucky. 899 ASCII bytes
# then em dashes puts the 900th byte on the SECOND byte of a 3-byte character —
# the case a naive byte slice corrupts.
pad899=$(printf 'x%.0s' $(seq 1 899))
dashes=""
i=0; while [ $i -lt 800 ]; do dashes="${dashes}—"; i=$((i + 1)); done
mbpayload="${pad899}${dashes}"
r=$(run_send "$mbpayload" "$FIXTURES/input-clean-submit.txt")

# A continuation byte is 0x80-0xBF (128-191). No chunk may START with one: given
# the byte-exact rejoin below, that is exactly "no chunk ENDS mid-character".
split=0
for f in "$CHUNK_DIR"/*; do
    b=$(chunk_first_byte "$f")
    if [ "$b" -ge 128 ] && [ "$b" -le 191 ]; then split=$((split + 1)); fi
done
assert_eq "no chunk begins on a UTF-8 continuation byte" "0" "$split"

# And the back-off actually fired rather than the cap happening to align: the
# first chunk is 899, one short of the cap, because byte 900 was mid-character.
# Without this the assertion above would also pass on a splitter that never backs
# off and was simply handed an aligned payload.
r=$(chunk_bytes "$CHUNK_DIR/001")
assert_eq "the boundary backs off the partial character (899, not 900)" "899" "$r"

# THE CAP, RE-ASSERTED ON MULTIBYTE (chunk O fold, F5). The cap loop above runs
# on the ASCII payload only, and the two are not the same test: the cap is a
# BYTE cap, and everything that could make it a character cap by mistake —
# dropping `local LC_ALL=C` from the splitter, comparing with ${#s} outside it —
# is invisible on ASCII and doubles or triples the chunk on multibyte. Measured:
# with `local LC_ALL=C` removed, an all-em-dash payload comes out as 2 chunks of
# 2700 bytes, which the assertions above catch only by accident.
over=0
for f in "$CHUNK_DIR"/*; do
    [ "$(chunk_bytes "$f")" -le 900 ] || over=$((over + 1))
done
assert_eq "no chunk exceeds the cap on a MULTIBYTE payload either" "0" "$over"

cat "$CHUNK_DIR"/* > "$TMPD/rejoined"
printf '%s' "$mbpayload" > "$TMPD/original"
r=$(cmp -s "$TMPD/original" "$TMPD/rejoined" && echo same || echo differs)
assert_eq "the multibyte payload rejoins byte for byte" "same" "$r"

echo "=== PANE_SEND_CHUNK_BYTES=0 restores the legacy single send (#1493) ==="

# The probe's control arm, and nothing else. It must reproduce the pre-fix shape
# EXACTLY — one send-keys, no -l — or the A/B measures two things at once and
# attributes the difference to the wrong one.
export PANE_SEND_CHUNK_BYTES=0
r=$(run_send "$payload2500" "$FIXTURES/input-clean-submit.txt")
assert_eq "unchunked arm: one payload send + one Enter" "2" "$r"
assert_eq "unchunked arm: no -l chunks recorded at all" "0" "$(chunk_count)"
r=$(grep -c -- '-l' "$RAW_LOG" || true)
assert_eq "unchunked arm: the legacy shape carries no -l" "0" "$r"
unset PANE_SEND_CHUNK_BYTES

# A malformed knob must fall back to the default, not abort a send: this runs
# inside startup and watchdog paths and a typo in an env file must not strand a
# bot.
export PANE_SEND_CHUNK_BYTES=notanumber
r=$(run_send "$payload2500" "$FIXTURES/input-clean-submit.txt")
assert_eq "a malformed cap falls back to the default (still 3 chunks)" "3" "$(chunk_count)"
unset PANE_SEND_CHUNK_BYTES

# A malformed SETTLE must not be able to strand a bot either, and this one is
# sharper than the cap: `sleep` rejects its argument with a non-zero status, and
# `[ "$idx" -eq 0 ] || sleep "$settle"` is a compound whose failure ABORTS the
# caller under set -e — half a payload delivered, no Enter, no error anybody
# reads. Asserted through a real multi-chunk send, since a single-chunk one
# never reaches the sleep at all and would pass on a broken guard.
export PANE_SEND_CHUNK_SETTLE_S=not-a-number
r=$(run_send "$payload2500" "$FIXTURES/input-clean-submit.txt")
assert_eq "a malformed settle falls back to the default (send completes)" "4" "$r"
export PANE_SEND_CHUNK_SETTLE_S=0

echo "=== the pre-draw repair resends CHUNKED too (#1493) ==="

# _pane_recover_unconfirmed_send resends the whole payload when the box was
# never confirmed and appears empty. Sending that one unchunked would repair a
# pre-draw loss by committing a 1 KB one — and it is the path that carries the
# BIGGEST payloads, since start-bot's STARTUP_PROMPT is what arms the wait.
: > "$CAPTURE"
r=$(run_send "$payload2500" \
    $(rep "$PANE_READY_TICKS" "$FIXTURES/predraw-empty.txt") "$FIXTURES/idle-prompt.txt")
assert_eq "repair path: 3 chunks + Enter, twice over" "8" "$r"
assert_eq "repair path: six keystroke chunks in total, all -l" "6" "$(chunk_count)"
r=$(grep -c '^Enter$' "$SENT_LOG" || true)
assert_eq "repair path: one Enter per send, never per chunk" "2" "$r"
r=$(grep -cE '"reason": ?"resent-after-box-drew"' "$CAPTURE" || true)
assert_eq "repair path: still recorded on the plane" "1" "$r"

echo "=== a trailing ';' survives tmux (chunk O fold, F1) ==="

# THE defect. tmux parses its argv as a COMMAND LIST, so a `;` that ENDS an
# argument is a separator rather than a character: `send-keys -l -- 'A;'` types
# `A` and exits 0. Measured on tmux 3.6a against a real pane running `cat` —
# `A;` -> `A`, `B;;` -> `B;`, a lone `;` -> nothing — and reproduced end to end,
# a 2100-byte payload whose byte 900 is a `;` arriving 2099 bytes long.
#
# The single-send era risked only the payload's LAST byte. Chunking puts one
# boundary every 900 bytes, so the exposure is per boundary now.

_pane_send_keys_arg 'ends-with-one;'
assert_eq "a trailing ';' is escaped for tmux" 'ends-with-one\;' "$_PANE_SEND_ARG"
_pane_send_keys_arg 'ends-with-two;;'
assert_eq "only the LAST ';' is escaped (the others are already literal)" \
    'ends-with-two;\;' "$_PANE_SEND_ARG"
_pane_send_keys_arg ';'
assert_eq "a lone ';' would vanish entirely without the escape" '\;' "$_PANE_SEND_ARG"
# The shape the RAW send already corrupted: tmux unescapes a trailing `\;`, so a
# payload genuinely ending in backslash-semicolon arrived as a bare `;`. Doubling
# the backslash round-trips it, which the pre-fix send never did.
_pane_send_keys_arg 'ends-with-esc\;'
assert_eq "a payload ending in a literal backslash-';' round-trips too" \
    'ends-with-esc\\;' "$_PANE_SEND_ARG"
_pane_send_keys_arg 'mid;string'
assert_eq "a ';' anywhere else is untouched" 'mid;string' "$_PANE_SEND_ARG"
_pane_send_keys_arg 'no semicolon at all'
assert_eq "a chunk with no ';' is passed through byte for byte" \
    'no semicolon at all' "$_PANE_SEND_ARG"

# ...and the door actually uses it: the ARGV tmux receives, not just the helper.
r=$(run_send 'a payload that ends in a semicolon;' "$FIXTURES/input-clean-submit.txt")
assert_eq "the door sends the ESCAPED argument, not the raw chunk" \
    'a payload that ends in a semicolon\;' "$(cat "$CHUNK_DIR/001")"

echo "=== ...and a real tmux agrees (F1, live) ==="

# A stub can only show the shape. Whether tmux delivers what the shape claims is
# a property of tmux, so this leg drives the REAL binary — a throwaway server, a
# pane reading in raw mode (canonical mode line-buffers and caps a line at
# MAX_CANON, which is a different mechanism and would swallow the answer).
if command -v tmux >/dev/null 2>&1; then
    RT_SOCK="panefoldrt$$"
    RT_DIR="$TMPD/roundtrip"
    mkdir -p "$RT_DIR"
    # kill-server leaves the SOCKET FILE behind, and a directory of dead
    # harness sockets is litter an operator then has to tell apart from a live
    # bot's — send-size-probe.sh's probe_cleanup rule, same reason.
    rt_reap() {
        tmux -L "$RT_SOCK" kill-server 2>/dev/null || true
        rm -f "${TMUX_TMPDIR:-/tmp}/tmux-$(id -u)/$RT_SOCK" 2>/dev/null || true
    }
    trap 'rt_reap; rm -rf "$TMPD"' EXIT
    rt_out="$RT_DIR/received"
    tmux -L "$RT_SOCK" new-session -d -s rt -x 200 -y 50 "stty raw -echo; cat > $rt_out"
    sleep 1
    for probe in 'x;' 'x;;' ';' 'plain'; do
        : > "$rt_out"
        _pane_send_keys_arg "$probe"
        tmux -L "$RT_SOCK" send-keys -t rt -l -- "$_PANE_SEND_ARG"
        sleep 0.7
        assert_eq "real tmux: '$probe' arrives byte-exact" "$probe" "$(cat "$rt_out")"
    done
    rt_reap
    trap 'rm -rf "$TMPD"' EXIT
else
    echo "  SKIP: no tmux on PATH — the live round trip needs one"
fi

echo "=== no two adjacent chunks are ever byte-identical (F2) ==="

# Measured on claude 2.1.263 / macOS: two identical 900-byte blocks of ORDINARY
# numbered-line text arrived 900 bytes SHORT, one block gone from the middle;
# 1200 identical bytes plus a varied tail arrived whole; thirty identical 60-byte
# lines whose phase did not align with the cap arrived whole. So the trigger is
# `chunk[i] == chunk[i-1]` and nothing about how long the identical run is — the
# probe's old note ("needs ~1800 identical bytes") described one instance of it.
# 31 identical 60-byte log lines starting on a boundary is enough, which a
# dispatch quoting a log reaches without trying.
split_report() {   # <payload> <cap> -> "<n> <adjacent-equal> <over-cap> <total>"
    local LC_ALL=C
    _pane_split_bytes "$1" "$2"
    local k=0 prev="" dup=0 over=0 total=0 c
    while [ "$k" -lt "$_PANE_CHUNK_N" ]; do
        c=${_PANE_CHUNKS[$k]}
        [ "$c" = "$prev" ] && dup=$((dup + 1))
        [ "${#c}" -le "$2" ] || over=$((over + 1))
        total=$((total + ${#c}))
        prev=$c
        k=$((k + 1))
    done
    printf '%s %s %s %s' "$_PANE_CHUNK_N" "$dup" "$over" "$total"
}

ident3600=$(printf 'a%.0s' $(seq 1 3600))
r=$(split_report "$ident3600" 900)
assert_eq "3600 identical bytes: no adjacent pair equal, none over cap, all bytes kept" \
    "0 0 3600" "$(printf '%s' "$r" | cut -d' ' -f2-4)"

# The realistic shape: 60-byte lines, phase-aligned with the 900 cap so that
# chunk boundaries land on line boundaries and consecutive chunks are identical.
period60=""
i=0; while [ $i -lt 80 ]; do period60="${period60}$(printf 'L%.0s' $(seq 1 59))
"; i=$((i + 1)); done
r=$(split_report "$period60" 900)
assert_eq "a 60-byte-period payload: no adjacent pair equal, none over cap" \
    "0 0" "$(printf '%s' "$r" | cut -d' ' -f2-3)"
LC_ALL=C p60len=${#period60}
assert_eq "the 60-byte-period payload rejoins to its full length" \
    "$p60len" "$(printf '%s' "$r" | cut -d' ' -f4)"

# The degenerate cap keeps its STATED bound rather than looping: at cap 1 there
# is no shorter chunk to take, so adjacent 1-byte chunks may repeat and progress
# is what matters. Asserted so the floor is a decision, not an accident.
r=$(split_report "aaaa" 1)
assert_eq "cap 1: still terminates and keeps every byte (adjacent dups allowed)" \
    "4 0 4" "$(printf '%s' "$r" | cut -d' ' -f1,3,4)"

echo "=== the inter-chunk settle is real (F3) ==="

# A mutant replacing `[ "$idx" -eq 0 ] || sleep "$settle"` with `:` passed the
# whole suite. The settle is half the mechanism — the cap stops a chunk FILLING
# the 1 KB pty queue, and this is what gives the reader time to empty it — so
# the calls are counted, not assumed.
SLEEP_LOG="$TMPD/sleeps.log"
: > "$SLEEP_LOG"
# Stubbed rather than shortened: the values are what identify which sleep is
# which, and the real thing would only cost wall clock.
sleep() { printf '%s\n' "${1:-}" >> "$SLEEP_LOG"; }
count_sleeps() { grep -cx "$1" "$SLEEP_LOG" || true; }

export PANE_SEND_CHUNK_SETTLE_S=0.3
export PANE_SEND_SETTLE_S=0.7          # distinct, so the pre-Enter settle is countable
: > "$SLEEP_LOG"
r=$(run_send "$payload2500" "$FIXTURES/input-clean-submit.txt")
assert_eq "a 3-chunk payload still sends 3 chunks + 1 Enter" "4" "$r"
assert_eq "...and settles exactly twice BETWEEN the three chunks" "2" "$(count_sleeps 0.3)"
assert_eq "...at the configured inter-chunk value, not the default" "0" "$(count_sleeps 0.15)"
assert_eq "...and the pre-Enter settle is its own, separate, single sleep" \
    "1" "$(count_sleeps 0.7)"

# One chunk, no boundary, no settle: the common send pays nothing for this.
: > "$SLEEP_LOG"
run_send 'short payload' "$FIXTURES/input-clean-submit.txt" >/dev/null
assert_eq "a single-chunk payload sleeps between no chunks at all" "0" "$(count_sleeps 0.3)"

# A malformed value falls back to the DEFAULT, and the fallback is what runs —
# the existing pin proves the send completes, this one proves it still settles.
export PANE_SEND_CHUNK_SETTLE_S=not-a-number
: > "$SLEEP_LOG"
run_send "$payload2500" "$FIXTURES/input-clean-submit.txt" >/dev/null
assert_eq "a malformed settle still settles, at the default value" \
    "2" "$(count_sleeps 0.15)"

unset -f sleep count_sleeps
export PANE_SEND_CHUNK_SETTLE_S=0
export PANE_SEND_SETTLE_S=0

echo "=== a mid-payload chunk failure is DISCLOSED (F6) ==="

# When send-keys fails at chunk k>0 the door returns 1 with k chunks already
# typed and no Enter — so a partial payload is sitting in the input box and the
# NEXT send concatenates onto it. It is not repaired here on purpose: the
# obvious clear is a C-c, and a second Ctrl-C in Claude Code exits the session,
# which is a failure path that can kill a bot. So it is said, and recorded.
run_send_failing() {
    local text="$1"; shift
    : > "$SENT_LOG"; : > "$ORDER_LOG"; : > "$RAW_LOG"
    rm -f "$CHUNK_DIR"/*; CHUNK_N=0
    printf '%s\n' "$@" > "$PANE_SCRIPT"
    local rc=0
    pane_send_verified sock "$SYNTH_ID" "$text" 2>"$TMPD/send-stderr.log" || rc=$?
    printf '%s' "$rc"
}

: > "$CAPTURE"
r=$(FAIL_ON_CHUNK=2 run_send_failing "$payload2500" "$FIXTURES/input-clean-submit.txt")
assert_eq "a chunk that fails mid-payload fails the send (never a silent partial)" "1" "$r"
r=$(grep -c 'chunk 2 of 3 failed' "$TMPD/send-stderr.log" || true)
assert_eq "the door says WHICH chunk failed" "1" "$r"
r=$(grep -c '900 bytes left unsubmitted in the box' "$TMPD/send-stderr.log" || true)
assert_eq "...and how many bytes it left in the box for the next send to run into" "1" "$r"
r=$(grep -cE '"event": ?"send_miss"' "$CAPTURE" || true)
assert_eq "the partial is on the plane as a send_miss (the send did NOT land)" "1" "$r"
r=$(grep -cE '"partial": ?"2/3"' "$CAPTURE" || true)
assert_eq "the event carries which chunk of how many" "1" "$r"
r=$(grep -cE '"reason": ?"chunk-send-failed"' "$CAPTURE" || true)
assert_eq "...named apart from an enter-swallowed retry, which routes differently" "1" "$r"
# No Enter went out: there is nothing submitted to verify, and firing one would
# submit the truncated head of a dispatch as if it were the whole thing.
r=$(grep -c '^Enter$' "$SENT_LOG" || true)
assert_eq "a failed partial never submits what did arrive" "0" "$r"

echo "=== chunking off is a NAMED, LOUD switch (F8) ==="

# PANE_SEND_CHUNK_BYTES=0 reaches a bot through `fleet.yaml env:` -> bot.conf
# like any other session knob, so it can restore the pre-fix send on a live
# fleet. It is registered in claudlobby/switches.py (pinned in
# tests/test_switches.py) and the door says so every time it runs unchunked: a
# silent no-op is indistinguishable from a working send.
export PANE_SEND_CHUNK_BYTES=0
run_send_failing "$payload2500" "$FIXTURES/input-clean-submit.txt" >/dev/null
r=$(grep -c 'chunking OFF (PANE_SEND_CHUNK_BYTES=0)' "$TMPD/send-stderr.log" || true)
assert_eq "the unchunked door names itself and its variable on stderr" "1" "$r"
r=$(grep -c 'lose their head' "$TMPD/send-stderr.log" || true)
assert_eq "...and says what it costs" "1" "$r"
# The trailing-';' guard applies to the legacy shape too: the pre-fix primitive
# had the same defect on the payload's last byte, and leaving it in would make
# the probe's control arm measure a ';' as well as the pty queue.
run_send_failing 'legacy shape with a trailing semicolon;' \
    "$FIXTURES/input-clean-submit.txt" >/dev/null
r=$(grep -c -- 'legacy shape with a trailing semicolon\\;' "$RAW_LOG" || true)
assert_eq "the unchunked path escapes a trailing ';' as well" "1" "$r"
unset PANE_SEND_CHUNK_BYTES

echo "=== the split does not stay resident after the send (F7) ==="

# _PANE_CHUNKS has to be a global — bash 3.2 cannot return an array — so without
# a reset the bytes of the last thing a bot sent live for the whole life of the
# shell, and keepalive's runs for the life of the host.
_pane_send_payload sock "$SYNTH_ID" "$payload2500" >/dev/null 2>&1
assert_eq "the chunk count is reset after a send" "0" "$_PANE_CHUNK_N"
assert_eq "the payload is not still resident in the chunk array" "" "${_PANE_CHUNKS[0]:-}"

echo ""
echo "=== $PASS/$TOTAL passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
