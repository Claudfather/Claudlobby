#!/usr/bin/env bash
# tests/test_selfstart_snapshot.sh — selfstart-snapshot classification contract.
#
# A real crash cannot be staged, so the whole surface is driven against
# synthetic transcript fixtures through the script real logic: real awk, real
# grep, real ls -t, real denominator parse. Only the boot instant and the
# journal boot record are injected, because those are the two facts a test
# cannot manufacture.
#
# What is under test is WHICH bucket each bot lands in, because that is the
# entire deliverable — a misclassified bot moves N, and N gets compared against
# the 6-of-21 baseline and believed.
#
# Hermetic: scratch CLAUDLOBBY_ROOT and CLAUDE_CONFIG_DIR, so it can neither
# read the real estate nor be perturbed by it.
#
# Standalone bash (also driven by tests/test_selfstart_snapshot.py so CI
# collects it); runs under macOS /bin/bash (3.2).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SNAP="$SCRIPT_DIR/../lib/selfstart-snapshot.sh"
PASS=0; FAIL=0; TOTAL=0

assert_eq() {
    TOTAL=$((TOTAL + 1)); local d="$1" e="$2" a="$3"
    if [ "$e" = "$a" ]; then
        echo "  PASS: $d"; PASS=$((PASS + 1))
    else
        echo "  FAIL: $d (expected '$e', got '$a')"; FAIL=$((FAIL + 1))
    fi
}

assert_contains() {
    TOTAL=$((TOTAL + 1)); local d="$1" needle="$2" hay="$3"
    case "$hay" in
        *"$needle"*) echo "  PASS: $d"; PASS=$((PASS + 1)) ;;
        *) echo "  FAIL: $d (missing '$needle')"; FAIL=$((FAIL + 1)) ;;
    esac
}

assert_absent() {
    TOTAL=$((TOTAL + 1)); local d="$1" needle="$2" hay="$3"
    case "$hay" in
        *"$needle"*) echo "  FAIL: $d (unexpectedly present: '$needle')"; FAIL=$((FAIL + 1)) ;;
        *) echo "  PASS: $d"; PASS=$((PASS + 1)) ;;
    esac
}

T="$(mktemp -d "${TMPDIR:-/tmp}/selfstart-test.XXXXXX")" || exit 1
trap 'rm -rf "$T"' EXIT
ROOT="$T/root"; CFG="$T/cfg"

# Boot instant the fixtures are built around.
BOOT=1786020000              # 2026-08-06T12:40:00Z
SANE_JOURNAL=1786020007      # 7s after  -> clock SANE
STALE_JOURNAL=1786016400     # 1h before -> clock STALE
PRE_TS="2026-08-06T10:00:00.000Z"
POST_TS="2026-08-06T13:00:00.000Z"

# ── Fixture builders ────────────────────────────────────────────────────────
declare_fleet() {  # declare_fleet <fleet> <bot>...
    local fleet="$1"; shift
    local d="$ROOT/local/home/$fleet"
    mkdir -p "$d"
    {
        echo "fleet:"
        echo "  name: $fleet"
        echo "  bots:"
        echo "    # a comment at bot indent must not be read as a bot"
        for b in "$@"; do
            echo "    $b:"
            echo "      expertise: [x]"
        done
        echo "  plugins:"
        echo "    additional: []"
    } > "$d/fleet.yaml"
}

bot_dir()   { printf '%s/local/home/%s/runtime/bots/%s\n' "$ROOT" "$1" "$2"; }
proj_dir()  { printf '%s/projects/%s\n' "$CFG" "$(bot_dir "$1" "$2" | tr '/' '-')"; }

mk_dir()    { mkdir -p "$(bot_dir "$1" "$2")"; }

mk_transcript() {  # mk_transcript <fleet> <bot> <name> <ts> <n_assistant> [mtime]
    local fleet="$1" bot="$2" name="$3" ts="$4" n="$5" mtime="${6:-}"
    local p; p="$(proj_dir "$fleet" "$bot")"
    mkdir -p "$p"
    local f="$p/$name.jsonl"
    # Leading records with no timestamp, exactly as Claude Code writes them.
    printf '{"type":"last-prompt","leafUuid":"x","sessionId":"%s"}\n' "$name" >  "$f"
    printf '{"type":"custom-title","customTitle":"T","sessionId":"%s"}\n' "$name" >> "$f"
    printf '{"type":"user","timestamp":"%s","sessionId":"%s"}\n' "$ts" "$name" >> "$f"
    local i=0
    while [ "$i" -lt "$n" ]; do
        printf '{"type":"assistant","timestamp":"%s","sessionId":"%s"}\n' "$ts" "$name" >> "$f"
        i=$((i + 1))
    done
    [ -n "$mtime" ] && touch -t "$mtime" "$f"
    return 0
}

run_snapshot() {  # run_snapshot <journal_epoch> [extra env assignments...]
    local j="$1"; shift
    env CLAUDLOBBY_ROOT="$ROOT" CLAUDE_CONFIG_DIR="$CFG" \
        SELFSTART_BOOT_EPOCH="$BOOT" SELFSTART_JOURNAL_BOOT_EPOCH="$j" \
        "$@" bash "$SNAP" 2>&1
}

# Which section a bot printed under.
section_of() {  # section_of "<output>" <bot>
    printf '%s\n' "$1" | awk -v b="$2" '
        /^SELF-STARTED \(/     { s="SELF-STARTED"; next }
        /^STRANDED \(/         { s="STRANDED";     next }
        /^ADJUDICATE /         { s="ADJUDICATE";   next }
        /^DISAGREEMENT DETAIL/ { s="";             next }
        /^NOTE:/               { s="";             next }
        s != "" && $1 == b { print s; exit }
    '
}

field_of() {  # field_of "<output>" <bot> <raw|filtered>
    printf '%s\n' "$1" | awk -v b="$2" -v k="$3" '
        $1 == b {
            for (i = 1; i <= NF; i++) {
                if (index($i, k "=") == 1) { sub(/^[a-z]*=/, "", $i); print $i; exit }
            }
        }
    '
}

# ── The estate under test ───────────────────────────────────────────────────
# alpha carries every classification shape; beta exists to prove the
# denominator is a UNION across manifests rather than one fleet.
declare_fleet alpha clean zeroturn deadsvc staleclock nodir notranscript mtimeinv
declare_fleet beta  solo

for b in clean zeroturn deadsvc staleclock notranscript mtimeinv; do mk_dir alpha "$b"; done
mk_dir beta solo
# `nodir` is declared but has NO directory at all — the naive walk drops it.
# `ghost` has a directory but is declared nowhere — it must not inflate N.
mk_dir alpha ghost

# clean self-starter: fresh file, post-boot records.
mk_transcript alpha clean fresh "$POST_TS" 3
# zero-turn strand: fresh file, post-boot, but the bot never spoke.
mk_transcript alpha zeroturn fresh "$POST_TS" 0
# service-failed: only a pre-crash file survives, hundreds of turns. RAW says
# self-started, FILTERED says no. The RAW false positive.
mk_transcript alpha deadsvc precrash "$PRE_TS" 400
# stale-clock self-starter: genuinely fresh session whose records carry pre-boot
# timestamps because fake-hwclock had not been corrected yet. Same on-disk shape
# as deadsvc — only the global clock verdict tells them apart.
mk_transcript alpha staleclock fresh "$PRE_TS" 5
# notranscript: directory exists, no transcript ever written.
# mtime inversion: the fresh post-boot file carries an OLDER mtime than the
# pre-crash file, so `ls -t` picks the wrong one. RAW is fooled; FILTERED, which
# reads every file, is not.
mk_transcript alpha mtimeinv precrash "$PRE_TS" 400 202608060900
mk_transcript alpha mtimeinv fresh    "$POST_TS" 2  202608060800
mk_transcript beta  solo  fresh "$POST_TS" 4

# ── Case 1: sane clock ──────────────────────────────────────────────────────
echo "== sane clock =="
OUT="$(run_snapshot "$SANE_JOURNAL")"
RC=$?

assert_eq "exit 0 on a clean run" "0" "$RC"
assert_contains "clock reported SANE" "clock at boot : SANE" "$OUT"

assert_eq "denominator is the UNION across both manifests (8, not 7)" \
    "8" "$(printf '%s\n' "$OUT" | sed -n 's/.*denominator   : \([0-9]*\) declared.*/\1/p')"

assert_eq "clean self-starter"                "SELF-STARTED" "$(section_of "$OUT" clean)"
assert_eq "clean raw"                         "3"            "$(field_of "$OUT" clean raw)"
assert_eq "clean filtered"                    "3"            "$(field_of "$OUT" clean filtered)"

assert_eq "zero-turn is a strand"             "STRANDED"     "$(section_of "$OUT" zeroturn)"
assert_eq "zero-turn raw is 0"                "0"            "$(field_of "$OUT" zeroturn raw)"

assert_eq "service-failed is a strand"        "STRANDED"     "$(section_of "$OUT" deadsvc)"
assert_eq "service-failed RAW is inflated"    "400"          "$(field_of "$OUT" deadsvc raw)"
assert_eq "service-failed FILTERED is 0"      "0"            "$(field_of "$OUT" deadsvc filtered)"

assert_eq "declared bot with NO directory appears, as a strand" \
    "STRANDED" "$(section_of "$OUT" nodir)"
assert_eq "declared bot with no transcript appears, as a strand" \
    "STRANDED" "$(section_of "$OUT" notranscript)"

assert_eq "mtime inversion: RAW fooled by pre-crash file" "400" "$(field_of "$OUT" mtimeinv raw)"
assert_eq "mtime inversion: FILTERED finds the fresh file" "2"  "$(field_of "$OUT" mtimeinv filtered)"
assert_eq "mtime inversion: classified self-started"  "SELF-STARTED" "$(section_of "$OUT" mtimeinv)"

assert_eq "second fleet bot counted"          "SELF-STARTED" "$(section_of "$OUT" solo)"

assert_contains "disagreement is surfaced, not silently resolved" \
    "counts DISAGREE on" "$OUT"
assert_contains "disagreement detail cites the evidence" "DISAGREEMENT DETAIL" "$OUT"

assert_contains "undeclared leftover dir is reported" "ghost" "$OUT"
assert_eq "undeclared leftover dir is NOT classified" "" "$(section_of "$OUT" ghost)"

assert_contains "headline leads with the count" "SELF-START SNAPSHOT:  3 of 8" "$OUT"
assert_contains "baseline printed next to it" "baseline to beat" "$OUT"

# The headline must be the first thing on the page — a stressed reader at 3am
# should not have to scroll past lists to reach it.
assert_contains "headline is in the first 4 lines" "SELF-START SNAPSHOT" \
    "$(printf '%s\n' "$OUT" | head -4)"

# ── Case 2: stale clock ─────────────────────────────────────────────────────
# Same fixtures, only the journal boot record moves. FILTERED is no longer
# trustworthy, so every RAW-vs-FILTERED disagreement becomes ADJUDICATE rather
# than being silently called either way.
echo "== stale clock =="
OUT2="$(run_snapshot "$STALE_JOURNAL")"

assert_contains "clock reported STALE" "clock at boot : STALE" "$OUT2"
assert_eq "stale-clock self-starter is flagged, not dropped" \
    "ADJUDICATE" "$(section_of "$OUT2" staleclock)"
assert_contains "stale-clock case is labelled provisional" \
    "provisionally SELF-STARTED" "$OUT2"
# Honest limit: with a stale clock the dead bot and the stale-clock self-starter
# are the same shape on disk, so the dead bot lands in ADJUDICATE too. That is
# fail-closed by design — the script must not pick a side it cannot see.
assert_eq "service-failed also needs adjudication under a stale clock" \
    "ADJUDICATE" "$(section_of "$OUT2" deadsvc)"
assert_contains "headline shows a range when bots are unresolved" "range 3-5" "$OUT2"
assert_eq "unambiguous strand is still a strand under a stale clock" \
    "STRANDED" "$(section_of "$OUT2" nodir)"
assert_eq "unambiguous self-starter is unaffected by the clock verdict" \
    "SELF-STARTED" "$(section_of "$OUT2" clean)"

# ── Case 3: the denominator fails loud ──────────────────────────────────────
# A manifest that cannot be parsed must never be soft-skipped. Soft-skipping
# reintroduces the silent-denominator bug one layer up: N shrinks by a whole
# fleet and the page still reads as complete.
echo "== unparseable manifest =="
printf 'fleet:\n  name: gamma\n  # no bots block at all\n' > "$ROOT/local/home/beta/fleet.yaml"
OUT3="$(run_snapshot "$SANE_JOURNAL")"; RC3=$?

assert_eq "unparseable manifest exits non-zero" "2" "$RC3"
assert_contains "banner is unmissable" "DENOMINATOR NOT TRUSTED" "$OUT3"
assert_contains "banner names the offending file" "beta/fleet.yaml" "$OUT3"
assert_absent "no counts are printed alongside the refusal" "SELF-START SNAPSHOT:" "$OUT3"

echo "== unparseable manifest, partial override =="
OUT4="$(run_snapshot "$SANE_JOURNAL" SELFSTART_ALLOW_PARTIAL=1)"; RC4=$?
assert_eq "override proceeds" "0" "$RC4"
assert_contains "override still shouts in the headline" "PARTIAL DENOMINATOR" "$OUT4"
assert_eq "override denominator drops to the parseable fleet only" \
    "7" "$(printf '%s\n' "$OUT4" | sed -n 's/.*denominator   : \([0-9]*\) declared.*/\1/p')"

# ── Case 4: duplicate bot name across fleets ────────────────────────────────
echo "== duplicate bot name across fleets =="
declare_fleet beta clean
OUT5="$(run_snapshot "$SANE_JOURNAL")"; RC5=$?
assert_eq "duplicate name exits non-zero" "2" "$RC5"
assert_contains "duplicate name is named in the banner" "clean" "$OUT5"

echo
echo "  ---- $PASS/$TOTAL passed, $FAIL failed ----"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
