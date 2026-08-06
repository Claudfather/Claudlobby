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

# A composed systemd unit carrying this bot's rung on the boot ladder. The
# decoy comment is deliberate: the real composed unit carries an explanatory
# line mentioning ExecStartPre, and an unanchored match reads it as the
# directive and picks up the wrong number (or none).
mk_unit() {  # mk_unit <fleet> <bot> <rung_seconds>
    local d; d="$(bot_dir "$1" "$2")"
    mkdir -p "$d"
    {
        echo "[Unit]"
        echo "#   activating      ExecStartPre — the boot stagger sleep"
        echo "[Service]"
        echo "ExecStartPre=/bin/sleep $3"
    } > "$d/com.test.$2.service"
}

iso_now() { date -u +%Y-%m-%dT%H:%M:%S.000Z; }

# Which section a bot printed under.
# The leading unindented-line rule CLEARS the section before the specific rules
# can set it, so an unrecognised block yields "" rather than inheriting the
# previous one. That matters: this helper used to fail OPEN, and when the
# NOT-YET-DUE section was added it silently reported those bots as STRANDED —
# the previous section label leaking across a header it did not match. A test
# helper that reports a wrong classification as a right one is worse than one
# that reports nothing, so unknown sections now fail closed.
section_of() {  # section_of "<output>" <bot>
    printf '%s\n' "$1" | awk -v b="$2" '
        /^[^ ]/                { s="" }
        /^SELF-STARTED \(/     { s="SELF-STARTED"; next }
        /^STRANDED \(/         { s="STRANDED";     next }
        /^NOT YET DUE/         { s="NOT-YET-DUE";  next }
        /^ADJUDICATE /         { s="ADJUDICATE";   next }
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

# Positive control for the completeness assertion below: a run that DID cover
# every declared bot must carry no stamp and exit 0. Without this, the
# incomplete-run test could pass against a script that stamps unconditionally.
assert_absent "healthy run carries no INCOMPLETE stamp" "INCOMPLETE" "$OUT"

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

# REGRESSION (#1045 review, found by vera). SELFSTART_ALLOW_PARTIAL used to
# waive this check as well as the manifest-parse one it was built for, and it
# did so in total silence: no banner, no PARTIAL stamp, exit 0, a page that read
# as an ordinary trustworthy snapshot. An override advertised for one condition
# must never be honoured by a second.
OUT5b="$(run_snapshot "$SANE_JOURNAL" SELFSTART_ALLOW_PARTIAL=1)"; RC5b=$?
assert_eq "ALLOW_PARTIAL does NOT waive the duplicate check" "2" "$RC5b"
assert_contains "refusal states the flag does not apply here" \
    "SELFSTART_ALLOW_PARTIAL does NOT waive this" "$OUT5b"
assert_absent "no snapshot page is printed under the bypass attempt" \
    "SELF-START SNAPSHOT:" "$OUT5b"

# The duplicate has its own override, and unlike the old bypass it is loud.
OUT5c="$(run_snapshot "$SANE_JOURNAL" SELFSTART_ALLOW_DUPLICATE_NAMES=1)"; RC5c=$?
assert_eq "dedicated duplicate override proceeds" "0" "$RC5c"
assert_contains "duplicate override stamps the headline" \
    "DUPLICATE BOT NAMES ACCEPTED" "$OUT5c"
assert_contains "duplicate override explains the ambiguity" \
    "identify two different bots each" "$OUT5c"
# The symmetric half: scoping has to hold in BOTH directions, or the new
# override just reintroduces the same bug with the operands swapped.
mkdir -p "$ROOT/local/home/gamma"
printf 'fleet:\n  name: gamma\n  # no bots block at all\n' > "$ROOT/local/home/gamma/fleet.yaml"
OUT5d="$(run_snapshot "$SANE_JOURNAL" SELFSTART_ALLOW_DUPLICATE_NAMES=1)"; RC5d=$?
assert_eq "duplicate override does NOT waive an unparseable manifest" "2" "$RC5d"
assert_contains "and the refusal is about the manifest, not the duplicate" \
    "could not be parsed" "$OUT5d"
rm -rf "$ROOT/local/home/gamma"

# ── Case 5: the run must prove it covered every declared bot ────────────────
# With `set -e` deliberately absent, "did it finish" cannot be inferred from the
# absence of a crash, so it is asserted positively. Fault-injected via a PATH
# stub for `wc` that inflates the declared-bot total by one — the shape of a bot
# that was declared but never produced a row. Content-keyed on the 3-field
# declared file, so the row and manifest counts are left untouched.
echo "== completeness assertion =="
declare_fleet beta solo
mkdir -p "$T/bin"
cat > "$T/bin/wc" <<'STUB'
#!/bin/bash
tmp=$(mktemp); cat > "$tmp"
n=$(/usr/bin/wc -l < "$tmp" | tr -d ' ')
if [ "$(head -1 "$tmp" | awk -F'\t' '{print NF}')" = "3" ]; then n=$((n + 1)); fi
rm -f "$tmp"; echo "$n"
STUB
chmod +x "$T/bin/wc"

OUT6="$(run_snapshot "$SANE_JOURNAL" "PATH=$T/bin:$PATH")"; RC6=$?
assert_eq "an incomplete run exits non-zero" "4" "$RC6"
assert_contains "incomplete banner is unmissable" "INCOMPLETE SNAPSHOT" "$OUT6"
assert_contains "incomplete banner shows the arithmetic" "Rows produced" "$OUT6"
assert_contains "incomplete headline is stamped, not just the banner" \
    "*** INCOMPLETE" "$OUT6"
assert_contains "incomplete page says N must not be used" \
    "must NOT be compared against the baseline" "$OUT6"

# ── Case 6: the boot ladder gate ────────────────────────────────────────────
# A bot whose ExecStartPre rung has not elapsed has not been launched by
# systemd, so it cannot have written anything and is NOT a strand. Counting it
# as one measures the elapsed clock rather than self-starting, and at boot+20s
# on a 21-bot host that renders as "0 of 21" against a 6-of-21 baseline — a
# catastrophe that has not happened, read during an incident, by someone
# deciding whether to intervene (#1050).
echo "== boot ladder gate, mid-ladder =="
ROOT="$T/root2"; CFG="$T/cfg2"
BOOT=$(( $(date +%s) - 20 ))
declare_fleet ladder early late norung_bot
for b in early late norung_bot; do mk_dir ladder "$b"; done
mk_unit ladder early 3     # rung elapsed: it has had its chance
mk_unit ladder late 60     # rung NOT elapsed: systemd is still sleeping on it
# norung_bot deliberately gets no unit, so the gate cannot be applied to it.
mk_transcript ladder early fresh "$(iso_now)" 2
# `late` also carries a fat pre-crash transcript, which is the RAW false
# positive shape. Not-yet-launched must dominate that verdict too.
mk_transcript ladder late precrash "$PRE_TS" 400

OUT7="$(run_snapshot "$BOOT")"; RC7=$?

assert_contains "too-early banner fires" "TOO EARLY" "$OUT7"
assert_contains "headline refuses to state a result" "NOT A RESULT" "$OUT7"
assert_contains "banner says how many were never launched" \
    "of 3 bots have NOT BEEN LAUNCHED yet" "$OUT7"
assert_contains "banner names the re-run instant" "Re-run at" "$OUT7"
assert_contains "banner offers the rescue-anyway escape" \
    "rescue" "$OUT7"
assert_contains "first-turn allowance is printed with its provenance" \
    "observed at load 31" "$OUT7"

assert_eq "unlaunched bot is NOT-YET-DUE, not stranded" \
    "NOT-YET-DUE" "$(section_of "$OUT7" late)"
assert_eq "not-yet-due dominates the pre-crash RAW false positive" \
    "400" "$(field_of "$OUT7" late raw)"
assert_eq "a bot past its rung that DID start is still self-started" \
    "SELF-STARTED" "$(section_of "$OUT7" early)"
assert_contains "bot with no readable rung is disclosed, not silently gated" \
    "no boot rung readable" "$OUT7"
assert_contains "and it is named" "norung_bot" "$OUT7"

# Positive control on the gate: past the window the same fixtures must produce
# a real result, and the unlaunched bot must become a genuine strand. Without
# this, the test above would pass against a script that gates unconditionally.
echo "== boot ladder gate, past the window =="
BOOT=$(( $(date +%s) - 300 ))
OUT8="$(run_snapshot "$BOOT")"; RC8=$?

assert_eq "past the window it exits 0" "0" "$RC8"
assert_absent "no too-early banner once the ladder has finished" "TOO EARLY" "$OUT8"
assert_contains "headline states a real result" "SELF-START SNAPSHOT:  1 of 3" "$OUT8"
assert_contains "result is marked valid" "result valid  : yes" "$OUT8"
assert_eq "the unlaunched bot is now a genuine strand" \
    "STRANDED" "$(section_of "$OUT8" late)"
assert_contains "the not-yet-due section is empty past the window" \
    "they are NOT strands (0)" "$OUT8"

echo
echo "  ---- $PASS/$TOTAL passed, $FAIL failed ----"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
