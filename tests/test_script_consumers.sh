#!/usr/bin/env bash
# tests/test_script_consumers.sh — verify every lib/ script has a consumer reference
#
# Checks that each lib/*.sh is referenced in at least one of:
#   - CLAUDE.md (root)
#   - library/protocols/*.md
#   - documentation/**/*.md
#
# Scripts may be internal helpers (sourced by other scripts, not standalone).
# These are allowlisted below. Everything else must have documentation.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=0; FAIL=0; TOTAL=0

# Internal helpers that are sourced, not invoked standalone.
# These don't need consumer docs — they're implementation details.
ALLOWLIST=(
    "lib-common.sh"
)

is_allowlisted() {
    local name="$1"
    for a in "${ALLOWLIST[@]}"; do
        [ "$name" = "$a" ] && return 0
    done
    return 1
}

echo "=== Script-consumer drift test ==="
echo "Checking that every lib/*.sh has a documentation reference..."
echo ""

orphans=""
for script in "$REPO_ROOT"/lib/*.sh; do
    [ -f "$script" ] || continue
    name=$(basename "$script")

    # Skip allowlisted internal helpers
    if is_allowlisted "$name"; then
        continue
    fi

    TOTAL=$((TOTAL + 1))

    # Search for the script name in consumer locations
    found=0
    # 1. Root CLAUDE.md
    grep -q "$name" "$REPO_ROOT/CLAUDE.md" 2>/dev/null && found=1
    # 2. Protocols
    if [ "$found" -eq 0 ]; then
        grep -rl "$name" "$REPO_ROOT/library/protocols/" 2>/dev/null | head -1 | grep -q . && found=1
    fi
    # 3. Documentation
    if [ "$found" -eq 0 ]; then
        grep -rl "$name" "$REPO_ROOT/documentation/" 2>/dev/null | head -1 | grep -q . && found=1
    fi

    if [ "$found" -eq 1 ]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        orphans="$orphans  ORPHAN: $name — no reference in CLAUDE.md, protocols/, or documentation/\n"
    fi
done

echo "Scanned $TOTAL scripts (excluding ${#ALLOWLIST[@]} allowlisted internal helpers)"
echo ""
if [ -n "$orphans" ]; then
    printf "%b" "$orphans"
    echo ""
fi
echo "=== Results: $PASS/$TOTAL referenced, $FAIL orphans ==="
[ "$FAIL" -eq 0 ] || { echo "FAIL: orphan scripts found — add them to CLAUDE.md or documentation"; exit 1; }
