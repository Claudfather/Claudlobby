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
# Asks pane_is_held, or the predicate named in PREDICATE.
check() {
    local label="$1" want="$2" pane="$3" loc="${4:-}" rc
    local predicate="${PREDICATE:-pane_is_held}"
    total=$((total + 1))
    if [ -n "$loc" ]; then
        LC_ALL="$loc" "$predicate" "$pane"; rc=$?
    else
        "$predicate" "$pane"; rc=$?
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
    # empty box's suggestion, the resend line after an API error), a menu where
    # an Enter would choose, empty boxes, and frames with no box at all.
    for f in input-queued-hint idle-placeholder menu-option usage-limit-menu input-clean-submit \
             idle-resend-placeholder idle-prompt idle-chevron idle-remote-control idle-permission \
             unknown-output unknown-blank verb-no-esc predraw-empty busy-spinner; do
        check "$f (not held)" 1 "$(fixture "$f.txt")" "$loc"
    done
done

# The box Claude Code draws after an API error (#2986): idle-placeholder's empty
# box with the faint line "Resend your message to try again" where its
# suggestion sits. idle-resend-placeholder is that frame rebuilt with the
# reported text, not a capture. The same box with text typed after the glyph is
# held, and so is typed text that begins with the line's words: the line is
# known by its whole text, as the queued-message hint is.
resend_box=$(fixture idle-resend-placeholder.txt)
typed_box=$(printf '%s\n' "$resend_box" | sed 's/Resend your message to try again/continue the review/')
opening_box=$(printf '%s\n' "$resend_box" | sed 's/Resend your message to try again/&, then continue/')
if [ "$typed_box" = "$resend_box" ] || [ "$opening_box" = "$resend_box" ]; then
    echo "  FAIL  the typed-text frames were not built"; failed=$((failed + 1))
fi
for loc in "" C; do
    check "typed text in the resend box (held)" 0 "$typed_box" "$loc"
    check "text opening with the resend line (held)" 0 "$opening_box" "$loc"
done

# Shapes no fixture covers.
check "empty pane" 1 ""
check "glyph and a no-break space only" 1 "$(printf '\342\235\257\302\240\n')"
for loc in "" C; do
    check "glyph, no-break space, resend line" 1 \
        "$(printf '\342\235\257\302\240Resend your message to try again\n')" "$loc"
    check "glyph, no-break space, typed text" 0 \
        "$(printf '\342\235\257\302\240continue the review\n')" "$loc"
done
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
echo "=== pane_is_menu: a menu holds the screen, so a send types nothing ==="
# The menu half of pane_is_held's evidence, asked on its own: every frame
# pane_is_held refuses as a menu is one, and no held box, empty box or
# hint is. Whole frames too, since pane_send_verified reads the whole pane.
credits_first=$(sed -e 's/❯ 1\. Stop and wait for limit to reset/❯ 1. Switch to usage credits/' \
    -e 's/^\(  *\)3\. Switch to usage credits/\13. Stop and wait for limit to reset/' \
    "$FIXTURE_DIR/usage-limit-menu.txt")
case "$credits_first" in
    *'❯ 1. Switch to usage credits'*'3. Stop and wait for limit to reset'*) ;;
    *) echo "  FAIL  the credits-first frame was not built"; failed=$((failed + 1)) ;;
esac
for loc in "" C; do
    for f in menu-option usage-limit-menu; do
        PREDICATE=pane_is_menu check "$f (menu)" 0 "$(fixture "$f.txt")" "$loc"
        PREDICATE=pane_is_menu check "$f, whole frame (menu)" 0 "$(cat "$FIXTURE_DIR/$f.txt")" "$loc"
    done
    PREDICATE=pane_is_menu check "usage credits listed first (menu)" 0 "$credits_first" "$loc"
    PREDICATE=pane_is_menu check "a second menu option selected (menu)" 0 \
        "$(printf '    1. Yes\n  \342\235\257 2. No, and tell Claude what to do differently\n\n  Esc to cancel\n')" "$loc"
    PREDICATE=pane_is_menu check "exit offered outside the box (menu)" 0 \
        "$(printf '> selected control\n────────────────────\n  Esc to cancel\n')" "$loc"
    for f in input-stuck-literal input-stuck-wrapped input-held-cr input-queued-hint \
             idle-placeholder input-clean-submit idle-prompt idle-chevron \
             idle-remote-control usage-limit-held usage-limit-menu-closed \
             usage-limit-native-armed unknown-blank busy-spinner; do
        PREDICATE=pane_is_menu check "$f (no menu)" 1 "$(fixture "$f.txt")" "$loc"
    done
    PREDICATE=pane_is_menu check "a draft naming the exit (no menu)" 1 \
        "$(printf '> Please explain Esc to cancel\n')" "$loc"
done
PREDICATE=pane_is_menu check "empty pane (no menu)" 1 ""

echo ""
echo "=== $passed/$total passed, $failed failed ==="

[ "$failed" -eq 0 ]
