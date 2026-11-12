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
LIB_DIR="$SCRIPT_DIR/../claudlobby/_runtime_scripts"
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

# The expected instant uses the host date implementation directly with an
# explicit zone. The helper under test decides whether the format requests UTC.
expected_date() {
    local zone="$1" mode="$2" fmt="$3"
    if [ "$_OS" = "Darwin" ]; then
        if [ "$mode" = "utc" ]; then
            TZ="$zone" date -u -v-7d +"$fmt"
        else
            TZ="$zone" date -v-7d +"$fmt"
        fi
    elif [ "$mode" = "utc" ]; then
        TZ="$zone" date -u -d "-7 days" +"$fmt"
    else
        TZ="$zone" date -d "-7 days" +"$fmt"
    fi
}

# in_zone ZONE ARGS...: date_relative ARGS..., with TZ=ZONE for that call only.
in_zone() {
    local zone="$1"
    shift
    TZ="$zone" date_relative "$@"
}

# Each check compares two reads of the clock, and the clock can turn between
# them: the second in the first block, the minute in the second, the date or
# the zone name in the last two (#2180). So the expected value is read on BOTH
# sides of the value under test, and all three reads are taken again while
# those two differ. Once they agree, the clock did not turn across the value
# under test, which must then equal them exactly.
#   assert_settled DESC WANT_CMD... -- GOT_CMD...
SETTLE_TRIES=5
assert_settled() {
    local desc="$1" want got after try want_cmd
    shift
    want_cmd=()
    while [ "$1" != "--" ]; do
        want_cmd+=("$1")
        shift
    done
    shift
    for ((try = 1; try <= SETTLE_TRIES; try++)); do
        want=$("${want_cmd[@]}")
        got=$("$@")
        after=$("${want_cmd[@]}")
        if [ "$want" = "$after" ]; then
            assert_eq "$desc" "$want" "$got"
            return 0
        fi
        echo "  NOTE: $desc: the clock turned during the reads ('$want', then '$after'); reading again"
    done
    TOTAL=$((TOTAL + 1))
    FAIL=$((FAIL + 1))
    echo "  FAIL: $desc (the clock turned during each of $SETTLE_TRIES tries)"
}

echo "=== a Z format resolves to real UTC in every zone ==="
for z in $ZONES; do
    assert_settled "TZ=$z Z-format equals true UTC" \
        expected_date "$z" utc "%Y-%m-%dT%H:%M:%SZ" -- \
        in_zone "$z" "-7 days" "%Y-%m-%dT%H:%M:%SZ"
done

echo "=== and the answer is the SAME instant regardless of zone ==="
# The load-bearing property: retention must not depend on where the host sits.
# Compared to the minute, against a read in UTC on each side of the zone's.
for z in $ZONES; do
    assert_settled "TZ=$z agrees with UTC to the minute" \
        in_zone UTC "-7 days" "%Y-%m-%dT%H:%MZ" -- \
        in_zone "$z" "-7 days" "%Y-%m-%dT%H:%MZ"
done

echo "=== a format WITHOUT Z is still local, unchanged ==="
# The fix must not silently move every caller to UTC. finance-presync.sh asks
# for a local calendar date and must keep getting one.
for z in $ZONES; do
    assert_settled "TZ=$z no-Z format stays local" \
        expected_date "$z" local "%Y-%m-%d" -- \
        in_zone "$z" "-7 days"
done

echo "=== %Z is the zone NAME directive, not a UTC request ==="
# The discriminator that stops the detection being a naive substring test.
assert_settled "%Z stays the local zone name" \
    expected_date America/New_York local "%Z" -- \
    in_zone America/New_York "-7 days" "%Z"

# (rotate_jsonl_by_ts — the consumer that carried the defect into production —
# went with the ledgers in the F18 closure; date_relative itself is pinned above.)

echo
echo "=== $PASS passed, $FAIL failed, $TOTAL total ==="
[ "$FAIL" -eq 0 ]
