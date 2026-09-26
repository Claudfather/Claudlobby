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

# Independent of GNU/BSD date syntax. Bracket the real helper call so crossing a
# second, minute, midnight or zone transition cannot make a valid result fail.
# Local subtraction is seven calendar days; UTC subtraction is seven UTC days.
check_relative() {
    local desc="$1" zone="$2" oracle_zone="$3" fmt="${4:-%Y-%m-%d}"
    local before after got matches
    before=$(python3 -c 'import time; print(time.time())')
    if [ "$#" -eq 3 ]; then
        got=$(TZ="$zone" date_relative "-7 days")
    else
        got=$(TZ="$zone" date_relative "-7 days" "$fmt")
    fi
    after=$(python3 -c 'import time; print(time.time())')
    matches=$(python3 - "$before" "$after" "$oracle_zone" "$fmt" "$got" <<'PY'
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import sys

before, after = (int(float(value)) for value in sys.argv[1:3])
assert after >= before, "wall clock moved backwards during the check"
zone = ZoneInfo(sys.argv[3])
expected = {
    (datetime.fromtimestamp(second, zone) - timedelta(days=7)).strftime(sys.argv[4])
    for second in range(before, after + 1)
}
print("yes" if sys.argv[5] in expected else f"expected one of {sorted(expected)!r}; got {sys.argv[5]!r}")
PY
)
    assert_eq "$desc" "yes" "$matches"
}

# Zones deliberately on BOTH sides of UTC. West-only would have passed against
# the bug in the direction that merely over-retains.
ZONES="Pacific/Auckland UTC America/New_York"

echo "=== a Z format resolves to real UTC in every zone ==="
for z in $ZONES; do
    check_relative "TZ=$z Z-format equals true UTC" "$z" UTC "%Y-%m-%dT%H:%M:%SZ"
done

echo "=== and the answer is the SAME instant regardless of zone ==="
# The load-bearing property: retention must not depend on where the host sits.
# Compare each call to its own UTC window, including a possible minute rollover.
for z in $ZONES; do
    check_relative "TZ=$z agrees with UTC to the minute" "$z" UTC "%Y-%m-%dT%H:%MZ"
done

echo "=== a format WITHOUT Z is still local, unchanged ==="
# The fix must not silently move every caller to UTC. finance-presync.sh asks
# for a local calendar date and must keep getting one.
for z in $ZONES; do
    check_relative "TZ=$z no-Z format stays local" "$z" "$z"
done

echo "=== %Z is the zone NAME directive, not a UTC request ==="
# The discriminator that stops the detection being a naive substring test.
check_relative "%Z stays the local zone name" America/New_York America/New_York "%Z"

# (rotate_jsonl_by_ts — the consumer that carried the defect into production —
# went with the ledgers in the F18 closure; date_relative itself is pinned above.)

echo
echo "=== $PASS passed, $FAIL failed, $TOTAL total ==="
[ "$FAIL" -eq 0 ]
