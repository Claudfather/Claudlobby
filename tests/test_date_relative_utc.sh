#!/usr/bin/env bash
# tests/test_date_relative_utc.sh — date_relative must honour a UTC format (#918)
#
# THE GAP THIS CLOSES IS LOCAL, NOT CI. CI runs UTC, where a local-time cutoff
# stamped Z is accidentally correct, so CI could never see the defect. Every
# developer host west of UTC could not see it either, because there the skew
# only RETAINS rows longer. The bug therefore sat filed and unfixed from
# 2026-07-30 until an unrelated fixture aged past the retention window and
# turned five tests red on every branch in the repo at once.
#
# So these assertions PIN THE ZONE explicitly rather than trusting the host to
# have one that reveals the problem. Each runs under a zone east of UTC, a zone
# west of it, and UTC itself: a rule that only holds in one zone is not a rule.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../lib"
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

. "$LIB_DIR/lib-common.sh"

# Zones deliberately on BOTH sides of UTC. West-only would have passed against
# the bug in the direction that merely over-retains.
ZONES="Pacific/Auckland UTC America/New_York"

echo "=== a Z format resolves to real UTC in every zone ==="
for z in $ZONES; do
    got=$(TZ="$z" date_relative "-7 days" "%Y-%m-%dT%H:%M:%SZ")
    want=$(TZ="$z" date -u -d "-7 days" +%Y-%m-%dT%H:%M:%SZ)
    assert_eq "TZ=$z Z-format equals true UTC" "$want" "$got"
done

echo "=== and the answer is the SAME instant regardless of zone ==="
# The load-bearing property: retention must not depend on where the host sits.
# Compared to the minute, since the three calls are seconds apart.
ref=$(TZ=UTC date_relative "-7 days" "%Y-%m-%dT%H:%MZ")
for z in $ZONES; do
    got=$(TZ="$z" date_relative "-7 days" "%Y-%m-%dT%H:%MZ")
    assert_eq "TZ=$z agrees with UTC to the minute" "$ref" "$got"
done

echo "=== a format WITHOUT Z is still local, unchanged ==="
# The fix must not silently move every caller to UTC. finance-presync.sh asks
# for a local calendar date and must keep getting one.
for z in $ZONES; do
    got=$(TZ="$z" date_relative "-7 days")
    want=$(TZ="$z" date -d "-7 days" +%Y-%m-%d)
    assert_eq "TZ=$z no-Z format stays local" "$want" "$got"
done

echo "=== %Z is the zone NAME directive, not a UTC request ==="
# The discriminator that stops the detection being a naive substring test.
got=$(TZ=America/New_York date_relative "-7 days" "%Z")
want=$(TZ=America/New_York date -d "-7 days" +%Z)
assert_eq "%Z stays the local zone name" "$want" "$got"

# (rotate_jsonl_by_ts — the consumer that carried the defect into production —
# went with the ledgers in the F18 closure; date_relative itself is pinned above.)

echo
echo "=== $PASS passed, $FAIL failed, $TOTAL total ==="
[ "$FAIL" -eq 0 ]
