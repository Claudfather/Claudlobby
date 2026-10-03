#!/bin/bash
# held_delivery_match (#2105): may one repair Enter submit this held delivery?
#
# The messaging operation owner asks it before each of its at most two repair
# Enters, after a receipt wait found no receipt for the message. It must answer
# text or chip only for a box that holds exactly this one tracked delivery, in a
# pane with no turn running and no menu open, and refuse every other frame with a
# reason. The frames are built here in the layout of the captured fixtures
# (tests/fixtures/pane-states/input-held-cr.txt: a rule above and below the box,
# the glyph and its NBSP, wrapped lines indented), and asked under the suite's
# locale and under LC_ALL=C.
#
# Usage: bash tests/test_held_delivery_match.sh
#   Exit 0 = all pass, exit 1 = failures.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FIXTURE_DIR="$SCRIPT_DIR/fixtures/pane-states"

# shellcheck source=../claudlobby/_runtime_scripts/lib-common.sh
. "$REPO_DIR/claudlobby/_runtime_scripts/lib-common.sh"
set +e

passed=0
failed=0
total=0

MSG="msg_0123456789abcdef0123456789abcdef"
OTHER="msg_fedcba9876543210fedcba9876543210"
NBSP=$(printf '\302\240')
RULE="────────────────────────────────────────────────────────────"
STATUS="  ⏵⏵ auto mode on (shift+tab to cycle)"

# box <line>... -- a frame: earlier output, the rule, the box lines (the first
# behind the glyph, the rest indented as the TUI wraps them), the rule, status.
box() {
    local first="$1"; shift
    printf '%s\n' "● Done. The earlier turn's last line." "" "$RULE" "❯${NBSP}${first}"
    local l
    for l in "$@"; do printf '  %s\n' "$l"; done
    printf '%s\n' "$RULE" "$STATUS"
}

# check <label> <expected rc> <expected verdict> <pane text>
check() {
    local label="$1" want_rc="$2" want="$3" pane="$4" loc got rc
    for loc in "" C; do
        total=$((total + 1))
        if [ -n "$loc" ]; then
            got=$(LC_ALL="$loc" held_delivery_match "$pane" "$MSG"); rc=$?
        else
            got=$(held_delivery_match "$pane" "$MSG"); rc=$?
        fi
        if [ "$rc" = "$want_rc" ] && [ "$got" = "$want" ]; then
            passed=$((passed + 1))
            printf "  PASS  %-52s %s rc=%s%s\n" "$label" "$got" "$rc" "${loc:+ [LC_ALL=$loc]}"
        else
            failed=$((failed + 1))
            printf "  FAIL  %-52s got '%s' rc=%s, expected '%s' rc=%s%s\n" \
                "$label" "$got" "$rc" "$want" "$want_rc" "${loc:+ [LC_ALL=$loc]}"
        fi
    done
}

ENVELOPE="[Claudlobby ordinary message] Message: $MSG From: bot:f/dara To: bot:f/otis Kind: chat"
TRAILER="⟦plane:$MSG⟧"

echo "=== held_delivery_match (#2105) ==="

# --- may submit -------------------------------------------------------------------
check "text: this delivery, wrapped, its trailer last" 0 text \
    "$(box "$ENVELOPE The body goes on past the box's" "width and wraps onto a second line." "$TRAILER")"
check "text: an assignment delivery's envelope" 0 text \
    "$(box "[Claudlobby assignment delivery] Task: wi_0 Assignment: asg_0 Message: $MSG" "Fix it." "$TRAILER")"
check "chip: one paste chip, +1 line (the trailer's)" 0 chip "$(box "[Pasted text #1 +1 lines]")"
check "chip: one paste chip, +2 lines (a swallowed Enter's CR)" 0 chip "$(box "[Pasted text #3 +2 lines]")"

# --- a turn is running, or no message is held ------------------------------------
busy_frame="$(printf '%s\n' "✻ Cogitating… (12s · esc to interrupt)")
$(box "$ENVELOPE body" "$TRAILER")"
check "busy: a turn is running, the delivery queued" 1 busy "$busy_frame"
check "not-held: an empty box" 1 not-held "$(box "")"
check "not-held: a menu, where an Enter would choose" 1 not-held "$(tail -10 "$FIXTURE_DIR/menu-option.txt")"
check "not-held: the queued-message hint" 1 not-held "$(tail -10 "$FIXTURE_DIR/input-queued-hint.txt")"
check "not-held: Esc to cancel under the box" 1 not-held \
    "$(box "$ENVELOPE body" "$TRAILER"; printf '%s\n' "  Enter to confirm · Esc to cancel")"

# --- the box holds something else, or more than this message ---------------------
check "not-shown: another message's delivery" 1 not-shown \
    "$(box "[Claudlobby ordinary message] Message: $OTHER body" "⟦plane:$OTHER⟧")"
check "not-shown: someone's typed text" 1 not-shown "$(box "let me check the logs first")"
check "glued: text before this message's envelope" 1 glued \
    "$(box "notes I was typing $ENVELOPE body" "$TRAILER")"
check "glued: another delivery glued ahead of this one" 1 glued \
    "$(box "[Claudlobby ordinary message] Message: $OTHER first" "⟦plane:$OTHER⟧" "$ENVELOPE second" "$TRAILER")"
check "chips: two paste chips" 1 chips "$(box "[Pasted text #1 +2 lines][Pasted text #2 +1 lines]")"
check "chips: a chip beside typed text" 1 chips "$(box "and also [Pasted text #2 +1 lines]")"
check "chip-lines: a chip with no newline (not a tracked wire)" 1 chip-lines "$(box "[Pasted text #4]")"
check "chip-lines: a chip with +3 lines" 1 chip-lines "$(box "[Pasted text #5 +3 lines]")"

echo ""
echo "=== $passed/$total passed ==="
[ "$failed" -eq 0 ]
