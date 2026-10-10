#!/bin/bash
# pane_is_held (#2070): does the input box hold text that was never submitted?
#
# Positive evidence only, read through the shipped pane readers. The held
# frames and the negative controls are the pane fixtures that
# test_keepalive_classify.sh classifies, asked of the predicate directly: under
# the suite's own locale, and under LC_ALL=C, where grep and awk read the
# glyphs byte by byte.
#
# Usage: bash tests/test_pane_is_held.sh
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

# check <label> <expected rc> <pane text> [locale]
check() {
    local label="$1" want="$2" pane="$3" loc="${4:-}" rc
    total=$((total + 1))
    if [ -n "$loc" ]; then
        LC_ALL="$loc" pane_is_held "$pane"; rc=$?
    else
        pane_is_held "$pane"; rc=$?
    fi
    if [ "$rc" = "$want" ]; then
        passed=$((passed + 1))
        printf "  PASS  %-44s rc=%s%s\n" "$label" "$rc" "${loc:+ [LC_ALL=$loc]}"
    else
        failed=$((failed + 1))
        printf "  FAIL  %-44s rc=%s (expected %s)%s\n" "$label" "$rc" "$want" "${loc:+ [LC_ALL=$loc]}"
    fi
}

fixture() { tail -10 "$FIXTURE_DIR/$1"; }

echo "=== pane_is_held (#2070) ==="
for loc in "" C; do
    # Held: earlier stranded sends, and a live capture's held box before and
    # after a first Enter removed the swallowed Enter's character.
    for f in input-stuck-literal input-stuck-wrapped input-stuck-wrapped-early \
             input-stuck-collapsed-paste input-held-cr input-held-after-enter; do
        check "$f (held)" 0 "$(fixture "$f.txt")" "$loc"
    done
    # Not held: Claude Code's own text in the box (the queued-message hint, an
    # empty box's suggestion), a menu where an Enter would choose, empty
    # boxes, and frames with no box at all.
    for f in input-queued-hint idle-placeholder menu-option input-clean-submit \
             idle-prompt idle-chevron idle-remote-control idle-permission \
             unknown-output unknown-blank verb-no-esc predraw-empty busy-spinner; do
        check "$f (not held)" 1 "$(fixture "$f.txt")" "$loc"
    done
done

# Shapes no fixture covers.
check "empty pane" 1 ""
check "glyph and a no-break space only" 1 "$(printf '\342\235\257\302\240\n')"
check "ascii glyph with text" 0 "$(printf '> continue the review\n')"
check "a second menu option selected" 1 \
    "$(printf '    1. Yes\n  \342\235\257 2. No, and tell Claude what to do differently\n\n  Esc to cancel\n')"

# Authored words must not masquerade as a menu footer, including a wrapped
# draft whose entire next line happens to be the exit label.
for loc in "" C; do
    for phrase in 'Esc to cancel' 'Esc to go back'; do
        check "draft mentions $phrase" 0 "$(printf '> Please explain %s\n' "$phrase")" "$loc"
        check "wrapped draft contains $phrase" 0 "$(printf '> Draft reason\n  %s\n' "$phrase")" "$loc"
        check "draft is exactly $phrase" 0 "$(printf '> %s\n' "$phrase")" "$loc"
        check "bordered draft contains $phrase" 0 \
            "$(printf '> Draft reason\n  %s\n────────────────────\n  auto mode on\n' "$phrase")" "$loc"
        check "actual footer outside box: $phrase" 1 \
            "$(printf '> selected control\n────────────────────\n  %s\n' "$phrase")" "$loc"
    done
done

echo ""
echo "=== $passed/$total passed, $failed failed ==="

[ "$failed" -eq 0 ]
