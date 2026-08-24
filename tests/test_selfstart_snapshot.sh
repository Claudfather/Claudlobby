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
# GNU form first, BSD second — the same two-form fallback epoch_to_iso_utc uses,
# because the harness runs on both hosts.
iso_at() {  # iso_at <epoch>
    date -u -d "@$1" +%Y-%m-%dT%H:%M:%S.000Z 2>/dev/null \
        || date -u -r "$1" +%Y-%m-%dT%H:%M:%S.000Z 2>/dev/null
}

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
        /^SELF-STARTED \(/     { s="SELF-STARTED";  next }
        /^STRANDED \(/         { s="STRANDED";      next }
        /^NOT YET DUE/         { s="NOT-YET-DUE";   next }
        /^ADJUDICATE /         { s="ADJUDICATE";    next }
        /^RESCUED /            { s="RESCUED";       next }
        /^HALF-BOOTED/         { s="PARTIAL";       next }
        /^LATE, UNEXPLAINED/   { s="LATE-UNEXPLAINED"; next }
        /^STRANDED ON BOOT/    { s="INBOUND-WOKEN"; next }
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
# The prior figure is printed as NOT COMPARABLE rather than as a target. It was
# measured by presence-of-record, which cannot see an inbound-woken or
# half-submitted boot, so it counts bots this classifier does not. Printing the
# two side by side as a target is how "flat" gets claimed without support.
assert_contains "prior figure is printed" "prior figure:" "$OUT"
assert_contains "and is labelled not comparable" "NOT COMPARABLE" "$OUT"
assert_absent "it is NOT offered as a target to beat" "baseline to beat" "$OUT"

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

# The exit code, which was the one RC in this file with no assertion on it
# (#1051 review, vera). A refusal that exits 0 is a caveat: the banner is
# advisory to a human and invisible to everything else, so the code is where
# refuse-rather-than-caveat either holds or quietly does not.
assert_eq "too-early run exits non-zero, and with its own code" "5" "$RC7"
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
# `early`'s record was stamped at creation time, which is now BOOT+300s — past
# the lateness bound (ladder 60s + first turn 60s). Re-stamped inside it, because
# this arm is the positive control for the LADDER gate specifically: a bot that
# fell foul of the LATENESS bound instead would satisfy nothing it is asked to.
mk_transcript ladder early fresh "$(iso_at $(( BOOT + 90 )))" 2
OUT8="$(run_snapshot "$BOOT")"; RC8=$?

assert_eq "past the window it exits 0" "0" "$RC8"
assert_absent "no too-early banner once the ladder has finished" "TOO EARLY" "$OUT8"
assert_contains "headline states a real result" "SELF-START SNAPSHOT:  1 of 3" "$OUT8"
assert_contains "result is marked valid" "result valid  : yes" "$OUT8"
assert_eq "the unlaunched bot is now a genuine strand" \
    "STRANDED" "$(section_of "$OUT8" late)"
assert_contains "the not-yet-due section is empty past the window" \
    "they are NOT strands (0)" "$OUT8"

# ── Case 7: exit-code precedence when both refusals hold ────────────────────
# TOO_EARLY (5) and INCOMPLETE (4) are independent, so both can fire at once and
# only one number reaches a caller. 4 must win: early is a property of WHEN the
# run happened and re-running at the stated instant fixes it; incomplete is a
# property of the run itself and re-running need not. The operator who sees one
# code should get the one that does not resolve on its own.
echo "== both refusals at once =="
BOOT=$(( $(date +%s) - 20 ))
OUT9="$(run_snapshot "$BOOT" "PATH=$T/bin:$PATH")"; RC9=$?

assert_eq "incomplete outranks too-early in the exit code" "4" "$RC9"
assert_contains "but both banners still print — early" "TOO EARLY" "$OUT9"
assert_contains "but both banners still print — incomplete" "INCOMPLETE SNAPSHOT" "$OUT9"

# ── Case 8: how the session came to life ────────────────────────────────────
# Presence of a post-boot record cannot tell a bot that woke up from one that
# was carried, or from one a human messaged. So the first post-boot USER record
# is typed before any instant is compared, and the typing is a DENYLIST: the
# channel injection and the tool_result record are the only shapes that do not
# vary, so those are matched and "payload" is whatever is left. Every detector
# that instead tried to RECOGNISE a startup payload failed, because payloads are
# authored per bot — one is prose, the next a bare slash command with no prose
# at all, and a rescuer types an approximation of neither.
echo "== boot classification: payload vs inbound vs nothing =="
ROOT="$T/root3"; CFG="$T/cfg3"
BOOT=1786020000
BOUNDARY="2026-08-06T12:50:00Z"        # BOOT+600s
PRE_B="2026-08-06T12:45:00.000Z"       # before the boundary
POST_B="2026-08-06T12:55:00.000Z"      # after it

declare_fleet rescue selfstarter rescuedbot contradictor inboundbot toolfirst nopayload
for b in selfstarter rescuedbot contradictor inboundbot toolfirst nopayload; do
    mk_dir rescue "$b"
done

# add_rec <fleet> <bot> <file> <ts> <raw-json-body-after-timestamp>
add_rec() {
    local p; p="$(proj_dir "$1" "$2")"; mkdir -p "$p"
    printf '{"type":"user","timestamp":"%s",%s}\n' "$4" "$5" >> "$p/$3.jsonl"
}
add_asst() {  # add_asst <fleet> <bot> <file> <ts> <n>
    local p; p="$(proj_dir "$1" "$2")"; mkdir -p "$p"
    local i=0
    while [ "$i" -lt "$5" ]; do
        printf '{"type":"assistant","timestamp":"%s","sessionId":"%s"}\n' "$4" "$3" >> "$p/$3.jsonl"
        i=$((i + 1))
    done
}
PAYLOAD='"message":{"role":"user","content":"set +H; Welcome back. Read your CLAUDE.md."}'
# The bridge injection carries isMeta — which is exactly why isMeta must NOT be
# filtered as system noise. Doing so drops the only evidence of this class.
CHANNEL='"isMeta":true,"message":{"role":"user","content":"<channel source=\"plugin:telegram:telegram\" chat_id=\"-100123\">\nping"}'
TOOLRES='"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t1"}]}'

add_rec  rescue selfstarter  s "$PRE_B"  "$PAYLOAD";  add_asst rescue selfstarter  s "$PRE_B"  3
add_rec  rescue rescuedbot   s "$POST_B" "$PAYLOAD";  add_asst rescue rescuedbot   s "$POST_B" 3
add_rec  rescue contradictor s "$PRE_B"  "$PAYLOAD";  add_asst rescue contradictor s "$PRE_B"  3
# The case that matters: woken by a human message, then fully alive. Session up,
# work being done, reporting normally — and it never self-started.
add_rec  rescue inboundbot   s "$PRE_B"  "$CHANNEL";  add_asst rescue inboundbot   s "$PRE_B"  9
# A tool_result lands BEFORE the channel record. If tool_result records were not
# excluded, this bot would type as "payload" (no channel marker in it) and read
# as a self-starter — the exclusion is load-bearing, not tidiness.
add_rec  rescue toolfirst    s "2026-08-06T12:44:00.000Z" "$TOOLRES"
add_rec  rescue toolfirst    s "$PRE_B"  "$CHANNEL";  add_asst rescue toolfirst    s "$PRE_B"  4
# Assistant records but no post-boot user record at all.
add_asst rescue nopayload    s "$PRE_B"  5

mk_receipt() {  # mk_receipt <file-date> <row-json>
    mkdir -p "$ROOT/state/events"
    printf '%s\n' "$2" >> "$ROOT/state/events/fleet-$1.jsonl"
}
RECEIPT_BASE='"ts":"2026-08-06T08:50:00-04:00","bot":"fleet","type":"fleet_rescue","source":"manual"'

# ---- 8a: no receipt at all. The classes still hold; contamination does not. --
OUT10="$(run_snapshot "$BOOT")"; RC10=$?
assert_eq "no receipt: exits 0" "0" "$RC10"
assert_eq "payload before any rescue is a self-start" \
    "SELF-STARTED" "$(section_of "$OUT10" selfstarter)"
assert_eq "inbound-woken is NOT a self-start" \
    "INBOUND-WOKEN" "$(section_of "$OUT10" inboundbot)"
assert_eq "tool_result records are excluded from the typing" \
    "INBOUND-WOKEN" "$(section_of "$OUT10" toolfirst)"
# Something spoke with nothing submitted. That is the two instruments
# disagreeing, and it is refused per bot rather than resolved either way —
# calling it a self-start would credit a boot nobody can evidence, and calling
# it a strand would deny one that is visibly running.
assert_eq "assistant records with NO user record are refused, not guessed" \
    "ADJUDICATE" "$(section_of "$OUT10" nopayload)"
assert_contains "inbound-woken section says liveness is not self-start" \
    "It never started on its own" "$OUT10"
assert_contains "absence of a receipt is disclosed, not read as absence of rescue" \
    "contamination CANNOT be ruled out" "$OUT10"
assert_eq "headline counts only genuine self-starts" \
    "3" "$(printf '%s\n' "$OUT10" | sed -n 's/.*SNAPSHOT:  \([0-9]*\) of 6.*/\1/p')"

# ---- 8b: a usable receipt splits payload into self-start vs carried ---------
mk_receipt 2026-08-06 "{$RECEIPT_BASE,\"data\":{\"actor\":\"tester\",\"bots_rescued\":[\"rescuedbot\",\"contradictor\"],\"selfstart_measurement_valid_before\":\"$BOUNDARY\"}}"
OUT11="$(run_snapshot "$BOOT")"; RC11=$?

assert_eq "a contaminated page refuses with its own exit code" "6" "$RC11"
assert_contains "contamination banner fires" "CONTAMINATED" "$OUT11"
assert_contains "headline stops claiming a result" "NOT A RESULT — a rescue covers this boot" "$OUT11"
assert_contains "validity line says no" "result valid  : NO" "$OUT11"
assert_eq "payload after the boundary is RESCUED, not self-started" \
    "RESCUED" "$(section_of "$OUT11" rescuedbot)"
assert_eq "payload before the boundary is still a self-start" \
    "SELF-STARTED" "$(section_of "$OUT11" selfstarter)"
# Positive control on the asymmetry: where the boundary IS usable, an unnamed
# bot falls through to the timestamp rather than being refused. Absence never
# certifies, but it also must not block a comparison that can decide.
assert_contains "an unnamed bot is decided by the boundary, not by the list" \
    "unaided" "$OUT11"
# The amendment case: the receipt disagrees with itself. Never resolved toward
# either fact — refused, by name.
assert_eq "named-as-rescued yet predating the boundary is a CONTRADICTION" \
    "ADJUDICATE" "$(section_of "$OUT11" contradictor)"
assert_contains "and the contradiction is spelled out" "RECEIPT CONTRADICTS ITSELF" "$OUT11"
assert_contains "the contradicting bot is named" "contradictor" "$OUT11"
assert_absent "no honest-limit note when a receipt IS present" \
    "contamination CANNOT be ruled out" "$OUT11"

# ---- 8c: a retroactive receipt — names stand, the stamp does not ------------
# `recorded` is the receipt disclosing that its stamp was TYPED, not read. The
# comparison is suppressed entirely; the name list survives, because a list of
# who was touched is not reconstructed by being written down late.
rm -f "$ROOT/state/events/fleet-2026-08-06.jsonl"
mk_receipt 2026-08-06 "{$RECEIPT_BASE,\"data\":{\"actor\":\"tester\",\"recorded\":\"after the fact, not at repair time\",\"bots_rescued\":[\"rescuedbot\",\"contradictor\"],\"selfstart_measurement_valid_before\":\"$BOUNDARY\"}}"
OUT12="$(run_snapshot "$BOOT")"; RC12=$?

assert_eq "a retroactive receipt still refuses" "6" "$RC12"
assert_contains "and says why the boundary is unusable" "recorded after the fact" "$OUT12"
assert_eq "a named bot is still RESCUED without any comparison" \
    "RESCUED" "$(section_of "$OUT12" rescuedbot)"
# The contradiction cannot arise here: with no comparable boundary there is
# nothing for the name list to disagree WITH, so the name is simply believed.
assert_eq "the named bot that predates is now believed, not adjudicated" \
    "RESCUED" "$(section_of "$OUT12" contradictor)"
# THE ASYMMETRY THAT MATTERS. The name list is NON-EXHAUSTIVE — presence means
# rescued definitively, absence means UNKNOWN. That is not theoretical: a second
# rescue went unrecorded hours after the receipt was designed, by the person who
# proposed it. So with the boundary suppressed there is nothing left that could
# certify this bot, and it is refused rather than promoted. Reading "not on the
# list" as "started on its own" is how an unrecorded rescue becomes a self-start.
assert_eq "absence from a non-exhaustive list is NOT evidence of self-start" \
    "ADJUDICATE" "$(section_of "$OUT12" selfstarter)"
assert_contains "and the refusal says why nothing can certify it" \
    "name list is not exhaustive" "$OUT12"

# ---- 8d: a correction row is not a receipt ---------------------------------
# The closing quote in the type match is load-bearing: a prefix match reads
# fleet_rescue_correction as a receipt with no boundary and refuses the page.
rm -f "$ROOT/state/events/fleet-2026-08-06.jsonl"
mk_receipt 2026-08-06 '{"ts":"2026-08-06T08:50:00-04:00","bot":"fleet","type":"fleet_rescue_correction","source":"manual","data":{"corrects":"a prior row"}}'
OUT13="$(run_snapshot "$BOOT")"; RC13=$?
assert_eq "a correction row alone does not contaminate" "0" "$RC13"
assert_absent "and raises no contamination banner" "CONTAMINATED" "$OUT13"

# ---- 8e: a receipt for an EARLIER boot must not bleed forward ---------------
rm -f "$ROOT/state/events/fleet-2026-08-06.jsonl"
mk_receipt 2026-08-06 "{$RECEIPT_BASE,\"data\":{\"actor\":\"tester\",\"bots_rescued\":[\"rescuedbot\"],\"selfstart_measurement_valid_before\":\"2026-08-06T11:00:00Z\"}}"
OUT14="$(run_snapshot "$BOOT")"; RC14=$?
assert_eq "a boundary predating this boot belongs to an earlier one" "0" "$RC14"
assert_eq "so its named bot is not marked rescued" \
    "SELF-STARTED" "$(section_of "$OUT14" rescuedbot)"

# ---- 8f: sub-second boundary arithmetic ------------------------------------
# A fractionless boundary compared against a fractional record: without padding,
# "12:50:00.500Z" sorts BEFORE "12:50:00Z" because "." is below "Z", and a bot
# half a second the wrong side silently flips class.
rm -f "$ROOT/state/events/fleet-2026-08-06.jsonl"
rm -rf "$(proj_dir rescue rescuedbot)"
add_rec  rescue rescuedbot s "2026-08-06T12:50:00.500Z" "$PAYLOAD"
add_asst rescue rescuedbot s "2026-08-06T12:50:00.500Z" 2
mk_receipt 2026-08-06 "{$RECEIPT_BASE,\"data\":{\"actor\":\"tester\",\"bots_rescued\":[],\"selfstart_measurement_valid_before\":\"$BOUNDARY\"}}"
OUT15="$(run_snapshot "$BOOT")"
assert_eq "half a second past a fractionless boundary is still RESCUED" \
    "RESCUED" "$(section_of "$OUT15" rescuedbot)"

# ---- 8g: precedence against the other two refusals -------------------------
# Contaminated outranks too-early: re-running fixes early and can never
# un-contaminate a boot. Incomplete outranks both.
BOOT=$(( $(date +%s) - 20 ))
rm -f "$ROOT"/state/events/*.jsonl
mk_receipt "$(date -u +%Y-%m-%d)" "{$RECEIPT_BASE,\"data\":{\"actor\":\"tester\",\"bots_rescued\":[\"rescuedbot\"],\"selfstart_measurement_valid_before\":\"$(date -u +%Y-%m-%dT%H:%M:%S)Z\"}}"
mk_unit rescue selfstarter 60
OUT16="$(run_snapshot "$BOOT")"; RC16=$?
assert_eq "contaminated outranks too-early in the exit code" "6" "$RC16"
assert_contains "but the too-early banner still prints" "TOO EARLY" "$OUT16"
assert_contains "and the contaminated banner still prints" "CONTAMINATED" "$OUT16"

OUT17="$(run_snapshot "$BOOT" "PATH=$T/bin:$PATH")"; RC17=$?
assert_eq "incomplete outranks contaminated" "4" "$RC17"

# ---- 8h-8k: the three ways a boundary fails to be one ----------------------
# 8b-8g drive the paths where the receipt HAS a comparable boundary. These four
# drive the paths where it does not, which is where the script has to refuse
# rather than guess — the property the whole of #1043 rests on.
#
# All three failures land in the same CLASS (NAMES-ONLY, boundary suppressed),
# so the class cannot tell them apart and asserting on it alone would pass
# against any of the three arms being wired to any of the others. The reason
# string is the only discriminator, and it is the operator-facing half: "the
# writer omitted the field", "the writer emitted two", and "the writer emitted
# something uncomparable" are fixed in three different places.
#
# 8k is the positive control. Without it, 8i is equally satisfied by a helper
# that refuses ANY repetition, which is an over-refusal that throws away a
# perfectly good boundary.
#
# 8g left the clock 20s past a synthetic boot and a receipt dated today; both
# are reset here. The 60s rung it composed onto `selfstarter` is REMOVED rather
# than left to be inert: it was inert for the not-yet-due gate, which compares a
# rung against elapsed time, but it is not inert for the lateness bound, which
# derives its instant from the ladder END. Leaving it would put the bound at
# BOOT+120s and make `selfstarter`'s BOOT+300s payload LATE-UNEXPLAINED in every
# case below — cases about receipt boundaries, decided by something else
# entirely. The bound has its own case set; these keep testing one thing.
rm -f "$(bot_dir rescue selfstarter)"/*.service
BOOT=1786020000

# ---- 8h: the boundary field is ABSENT (row_field rc=1) ---------------------
# A receipt from a writer that never stamped the field. The name list still
# stands — a rescue is not undone by being written down without a boundary —
# but nothing is comparable, so an unnamed bot cannot be certified.
#
# WOULD FAIL IF: row_field stopped distinguishing absent from present-and-empty
# (an empty value falls through to 8j's message instead), the rc=1 arm were
# dropped, or absence stopped suppressing the boundary at all.
rm -f "$ROOT"/state/events/*.jsonl
mk_receipt 2026-08-06 "{$RECEIPT_BASE,\"data\":{\"actor\":\"tester\",\"bots_rescued\":[\"rescuedbot\"]}}"
OUT20="$(run_snapshot "$BOOT")"; RC20=$?

assert_eq "a receipt with no boundary field still refuses the page" "6" "$RC20"
assert_contains "and reports the field as ABSENT" \
    "receipt carries no selfstart_measurement_valid_before boundary" "$OUT20"
assert_absent "absence is not reported as a present-but-unusable stamp" \
    "not a comparable UTC instant" "$OUT20"
assert_eq "the name list still applies with no boundary at all" \
    "RESCUED" "$(section_of "$OUT20" rescuedbot)"
assert_eq "and an unnamed bot is refused rather than promoted" \
    "ADJUDICATE" "$(section_of "$OUT20" selfstarter)"

# ---- 8i: two distinct boundaries in ONE row (row_field rc=2) ---------------
# Refusing on ambiguity is doing real work here. Measured, not assumed: with the
# distinct-count arm gone the two values survive as a two-line string that
# `iso_utc_shaped` ACCEPTS — its trailing-Z glob spans the newline — so the run
# adopts a garbage boundary, marks it USABLE, and compares every bot against it.
# Confident and wrong, which is the exact failure this branch exists to prevent.
#
# WOULD FAIL IF: the distinct-count arm were dropped (state goes USABLE and the
# ambiguity line never prints), or rc=2 were folded into rc=1's message.
rm -f "$ROOT"/state/events/*.jsonl
mk_receipt 2026-08-06 "{$RECEIPT_BASE,\"selfstart_measurement_valid_before\":\"2026-08-06T13:30:00Z\",\"data\":{\"actor\":\"tester\",\"bots_rescued\":[\"rescuedbot\"],\"selfstart_measurement_valid_before\":\"$BOUNDARY\"}}"
OUT21="$(run_snapshot "$BOOT")"; RC21=$?

assert_eq "an ambiguous boundary refuses the page" "6" "$RC21"
assert_contains "and names ambiguity as the reason" \
    "more than one distinct measurement boundary" "$OUT21"
assert_absent "ambiguity is not reported as absence" \
    "receipt carries no selfstart_measurement_valid_before boundary" "$OUT21"
assert_contains "and neither of the two candidates is adopted" \
    "Rescue boundary   : UNUSABLE" "$OUT21"
assert_eq "the name list survives the ambiguity" \
    "RESCUED" "$(section_of "$OUT21" rescuedbot)"

# ---- 8j: a present, unambiguous, uncomparable stamp (iso_utc_shaped false) --
# The realistic shape is the first one: the writer copied the row's own `ts`,
# which is a LOCAL offset. Same instant, four hours out as a string — and every
# comparison here is a string comparison, so adopting it moves every bot sitting
# in that window to the wrong side. The bare date is the second shape and is
# rejected for a different reason (no time at all, rather than no Z), so both
# halves of the glob are exercised rather than just the tail.
#
# WOULD FAIL IF: iso_utc_shaped stopped requiring the trailing Z (the offset
# stamp is adopted) or stopped requiring the T hh:mm:ss middle (the bare date
# is adopted). Either weakening flips the state to USABLE.
rm -f "$ROOT"/state/events/*.jsonl
mk_receipt 2026-08-06 "{$RECEIPT_BASE,\"data\":{\"actor\":\"tester\",\"bots_rescued\":[\"rescuedbot\"],\"selfstart_measurement_valid_before\":\"2026-08-06T08:50:00-04:00\"}}"
OUT22="$(run_snapshot "$BOOT")"; RC22=$?

assert_eq "a local-offset boundary refuses the page" "6" "$RC22"
assert_contains "and echoes the stamp so it can be repaired" \
    "not a comparable UTC instant: 2026-08-06T08:50:00-04:00" "$OUT22"
assert_absent "a present stamp is not reported as absent" \
    "receipt carries no selfstart_measurement_valid_before boundary" "$OUT22"
assert_contains "and the uncomparable stamp is never adopted" \
    "Rescue boundary   : UNUSABLE" "$OUT22"

rm -f "$ROOT"/state/events/*.jsonl
mk_receipt 2026-08-06 "{$RECEIPT_BASE,\"data\":{\"actor\":\"tester\",\"bots_rescued\":[\"rescuedbot\"],\"selfstart_measurement_valid_before\":\"2026-08-06\"}}"
OUT23="$(run_snapshot "$BOOT")"; RC23=$?

assert_eq "a date with no time is equally uncomparable" "6" "$RC23"
assert_contains "and is refused as a non-instant" \
    "not a comparable UTC instant: 2026-08-06" "$OUT23"

# ---- 8k: the SAME boundary twice is NOT ambiguity (positive control) -------
# 8i alone is satisfied by a helper that refuses any repetition at all, and that
# would be an over-refusal with a real cost: a verbose-but-consistent receipt
# gets thrown away and every unnamed bot drops to ADJUDICATE for nothing. The
# count is over DISTINCT values, and this is the case that proves it still is.
#
# The two per-bot assertions are the point. The name list is deliberately EMPTY,
# so nothing but a working boundary comparison can separate these two bots —
# without it, "USABLE" would be a label with no behaviour behind it.
#
# WOULD FAIL IF: -u were dropped from row_field's sort, or the count moved from
# distinct values to raw match count. Either turns this row into 8i.
rm -f "$ROOT"/state/events/*.jsonl
mk_receipt 2026-08-06 "{$RECEIPT_BASE,\"selfstart_measurement_valid_before\":\"$BOUNDARY\",\"data\":{\"actor\":\"tester\",\"bots_rescued\":[],\"selfstart_measurement_valid_before\":\"$BOUNDARY\"}}"
OUT24="$(run_snapshot "$BOOT")"; RC24=$?

assert_eq "a repeated but consistent boundary still refuses — a rescue covers the boot" "6" "$RC24"
assert_contains "yet the boundary is ADOPTED, not refused as ambiguous" \
    "Rescue boundary   : $BOUNDARY" "$OUT24"
assert_absent "and ambiguity is not claimed" \
    "more than one distinct measurement boundary" "$OUT24"
assert_eq "a payload after the adopted boundary is RESCUED" \
    "RESCUED" "$(section_of "$OUT24" rescuedbot)"
assert_eq "a payload before it is still a self-start" \
    "SELF-STARTED" "$(section_of "$OUT24" selfstarter)"

# ── Case 9: the WHOLE boot injection, not merely something startup-shaped ───
# A boot is TWO sends: a bare `/claudna:session resume --auto` and then
# `set +H; $STARTUP_PROMPT`. Asserting that something startup-shaped arrived
# passes a bot whose injection only half landed — measured on real bots, one
# composed prompt was still unsubmitted 39 minutes after boot and another never
# arrived at all, and both read as clean self-starters under the earlier
# contract. Those are not self-starts: the bot is running without the
# instructions it was composed with.
echo "== whole-injection assertion =="
ROOT="$T/root4"; CFG="$T/cfg4"
BOOT=1786020000
WHOLE_TS="2026-08-06T12:45:00.000Z"

declare_fleet inject bothparts slashonly noconf
for b in bothparts slashonly noconf; do mk_dir inject "$b"; done

mk_botconf() {  # mk_botconf <fleet> <bot> <prompt>
    printf 'export BOT_NAME="%s"\nexport STARTUP_PROMPT="%s"\n' "$2" "$3" \
        > "$(bot_dir "$1" "$2")/bot.conf"
}
PROMPT='Welcome back. Read your CLAUDE.md. Idle and await Telegram messages.'
SLASH='"message":{"role":"user","content":"<command-message>claudna:session</command-message>\n<command-name>/claudna:session</command-name>\n<command-args>resume --auto</command-args>"}'
PROSE="\"message\":{\"role\":\"user\",\"content\":\"set +H; $PROMPT\"}"

mk_botconf inject bothparts "$PROMPT"
mk_botconf inject slashonly "$PROMPT"
# noconf deliberately gets no bot.conf: the assertion cannot run for it.

# Both halves landed, slash first exactly as start-bot.sh sends them.
add_rec  inject bothparts s "2026-08-06T12:44:50.000Z" "$SLASH"
add_rec  inject bothparts s "$WHOLE_TS" "$PROSE"
add_asst inject bothparts s "$WHOLE_TS" 3
# Only the slash half. This is the shape that used to read as a clean boot.
add_rec  inject slashonly s "2026-08-06T12:44:50.000Z" "$SLASH"
add_asst inject slashonly s "2026-08-06T12:44:50.000Z" 3
add_rec  inject noconf    s "$WHOLE_TS" "$PROSE"
add_asst inject noconf    s "$WHOLE_TS" 3

OUT18="$(run_snapshot "$BOOT")"

assert_eq "both halves submitted is a genuine self-start" \
    "SELF-STARTED" "$(section_of "$OUT18" bothparts)"
assert_eq "only the slash half submitted is PARTIAL, not a self-start" \
    "PARTIAL" "$(section_of "$OUT18" slashonly)"
assert_contains "and the half-boot is spelled out" "only HALF submitted" "$OUT18"
assert_contains "the section says what it means operationally" \
    "Running without the instructions it was composed with" "$OUT18"
# The count is the point: a half-booted bot must not be inside N.
assert_eq "a half-booted bot is excluded from the headline count" \
    "2" "$(printf '%s\n' "$OUT18" | sed -n 's/.*SNAPSHOT:  \([0-9]*\) of 3.*/\1/p')"
# A bot whose composed prompt cannot be read cannot have the assertion applied,
# so it is disclosed rather than silently credited with a clean boot.
assert_contains "a bot with no composed prompt is disclosed" \
    "no composed STARTUP_PROMPT readable" "$OUT18"
assert_contains "and it is named" "noconf" "$OUT18"

# The assertion is against the bot's OWN composed value, so a resend of some
# OTHER bot's prompt does not satisfy it.
rm -rf "$(proj_dir inject slashonly)"
add_rec  inject slashonly s "$WHOLE_TS" '"message":{"role":"user","content":"set +H; You just started up. Read your CLAUDE.md."}'
add_asst inject slashonly s "$WHOLE_TS" 3
OUT19="$(run_snapshot "$BOOT")"
assert_eq "a prompt that is not THIS bot composed one does not satisfy it" \
    "PARTIAL" "$(section_of "$OUT19" slashonly)"

# ── Case 10: the lateness bound on SELF-STARTED (#1203) ────────────────────
# SELF-STARTED is reached by ELIMINATION — a payload no receipt covers and that
# is not channel-shaped falls through to it. Compose that with the receipt list
# being known-incomplete (#1110: a real rescue went unrecorded) and every wake
# nobody wrote down is reported as good news.
#
# Measured, on the 2026-08-13 19:17:18Z boot: ari's payload landed at 19:43:30Z,
# 26 minutes after boot, predating the only applicable receipt boundary, and was
# printed "unaided". The ladder runs in seconds. The honest figure was 2 of 21;
# the script printed 3.
#
# So arrival is bounded, and past the bound with nothing to account for it the
# bot is LATE-UNEXPLAINED — its own answer, deliberately not RESCUED, because
# nothing evidences a rescue. It reads as a gap, which is what it is.
echo "== lateness bound on SELF-STARTED =="
ROOT="$T/root5"; CFG="$T/cfg5"
BOOT=1786020000                          # 2026-08-06T12:40:00Z
# Ladder end 60s + first-turn allowance 60s => bound at BOOT+120 = 12:42:00Z.
IN_TS="2026-08-06T12:41:30.000Z"         # BOOT+90   inside
AT_TS="2026-08-06T12:42:00.000Z"         # BOOT+120  exactly ON the bound
PAST_TS="2026-08-06T12:42:01.000Z"       # BOOT+121  one second past
SUB_TS="2026-08-06T12:42:00.500Z"        # BOOT+120.5  half a second past
ARI_TS="2026-08-06T13:06:12.000Z"        # BOOT+1572 the live exemplar

declare_fleet bound intime atbound pastbound subsecond ladderend arishape
for b in intime atbound pastbound subsecond ladderend arishape; do mk_dir bound "$b"; done

# Rungs live in ONE place, and the teardown GLOBS rather than re-listing the
# bots. Enumerating them twice is how case 10g first shipped: a bot added to the
# fleet kept its unit through the no-ladder arm, the ladder stayed readable, and
# the suppression under test never fired.
bound_units() {  # bound_units <on|off>
    if [ "$1" = "off" ]; then
        rm -f "$ROOT"/local/home/bound/runtime/bots/*/*.service
        return 0
    fi
    mk_unit bound intime 12;    mk_unit bound atbound 6
    mk_unit bound pastbound 6;  mk_unit bound subsecond 6
    mk_unit bound ladderend 60; mk_unit bound arishape 6
}
bound_units on

# `intime` is the otis/ravi shape and the reason the bound is fleet-wide rather
# than per-bot: a 12s rung with a payload at BOOT+90s is 78s past its OWN rung,
# so a per-bot bound (own rung + allowance) would flag it. Both real self-starts
# on the 2026-08-13 boot had exactly this shape — otis rung 15s at boot+97s,
# ravi rung 12s at boot+96s — so a per-bot bound would have flagged the only two
# genuine self-starts the estate has ever recorded.
mk_transcript bound intime    fresh "$IN_TS"   3
mk_transcript bound atbound   fresh "$AT_TS"   3
mk_transcript bound pastbound fresh "$PAST_TS" 3
# Records carry sub-second precision; the derived bound does not. Whole-second
# fixtures alone leave the padding untested — found by mutation, not by reading:
# stripping `.000` from the bound left the whole case set green. "." sorts BELOW
# "Z", so against an unpadded 12:42:00Z a record at 12:42:00.500Z compares as
# EARLIER and a bot half a second late is silently credited as a self-start.
mk_transcript bound subsecond fresh "$SUB_TS"  3
mk_transcript bound ladderend fresh "$IN_TS"   3
mk_transcript bound arishape  fresh "$ARI_TS"  3

OUT25="$(run_snapshot "$SANE_JOURNAL")"; RC25=$?

# ---- 10a: the bucket exists, is reached, and is not any of the others ------
assert_eq "a payload past the bound is LATE-UNEXPLAINED" \
    "LATE-UNEXPLAINED" "$(section_of "$OUT25" arishape)"
assert_eq "one second past the bound is enough" \
    "LATE-UNEXPLAINED" "$(section_of "$OUT25" pastbound)"
# Not folded into RESCUED — nothing here evidences a rescue, and saying one
# happened would be the same fabrication as saying none did.
assert_contains "late is NOT folded into RESCUED" \
    "RESCUED — carried by a rescuer, NOT self-started (0)" "$OUT25"
assert_contains "the section reads as a gap, not a pass" \
    "This is a GAP, not a pass" "$OUT25"
assert_contains "and says nothing recorded the wake" \
    "something woke these bots and nothing recorded what" "$OUT25"
# The per-bot line has to carry the instant AND its derivation, or an operator
# cannot tell a bound that moved from one that was wrong.
assert_contains "the row names the bound it failed" \
    "past the self-start bound 2026-08-06T12:42:00Z" "$OUT25"
assert_contains "and names what the bound was derived from" \
    "(ladder 60s + first turn 60s)" "$OUT25"

# ---- 10b: POSITIVE CONTROL — the bound must not fire, and lands where claimed
# Without these the whole case is satisfied by a script that downgrades every
# self-start, which would read as a fix and measure nothing.
assert_eq "a payload inside the bound is still a self-start" \
    "SELF-STARTED" "$(section_of "$OUT25" intime)"
assert_eq "the bot that SETS the ladder end is still a self-start" \
    "SELF-STARTED" "$(section_of "$OUT25" ladderend)"
# Exactly ON the bound is not past it. This is the pair that proves the boundary
# sits where the page says it does: 12:42:00.000Z passes and 12:42:01.000Z does
# not, one second apart. "Far away fails" would pass against a bound anywhere.
assert_eq "a payload exactly ON the bound is not past it" \
    "SELF-STARTED" "$(section_of "$OUT25" atbound)"
# ...and half a second past it IS. Together these two pin the boundary to within
# 500ms, which is the resolution the records actually carry.
assert_eq "half a second past the bound is late, not rounded down" \
    "LATE-UNEXPLAINED" "$(section_of "$OUT25" subsecond)"
# The otis regression, stated as its own assertion: `intime` is 78s past its own
# rung and must survive anyway.
assert_contains "a bot far past its OWN rung but inside the ladder bound survives" \
    "intime" "$(printf '%s\n' "$OUT25" | awk '/^SELF-STARTED \(/,/^$/')"

# ---- 10c: it is a row verdict, not a page refusal -------------------------
# 4/5/6 say THE PAGE is not a result. A late bot is a per-bot verdict on a page
# that is otherwise fine, exactly like ADJUDICATE, so it must not invent a code.
assert_eq "a late bot does not make the page refuse" "0" "$RC25"
assert_absent "and does not trip the completeness assertion" "INCOMPLETE" "$OUT25"
assert_contains "the headline excludes late bots from N" \
    "SELF-START SNAPSHOT:  3 of 6" "$OUT25"
# Counted OUT of the headline and INTO its upper range: a late payload might
# still be a very slow self-start, so folding it in would overclaim and dropping
# it would underclaim.
assert_contains "but keeps them in the upper range" \
    "range 3-6 — 0 unresolved (ADJUDICATE), 3 late (LATE-UNEXPLAINED)" "$OUT25"
assert_contains "the bound is printed with its derivation" \
    "self-start bound: 2026-08-06T12:42:00Z (ladder 60s + first turn 60s)" "$OUT25"

# ---- 10d: DERIVED, not hardcoded ------------------------------------------
# The bound must track the composed ladder. Raise the ladder end and the same
# record stops being late — a hardcoded constant cannot do that, so this is what
# separates "derived" from "a number that happens to work on this fixture".
mk_unit bound ladderend 600
OUT26="$(run_snapshot "$SANE_JOURNAL")"
assert_eq "raising the composed ladder moves the bound with it" \
    "SELF-STARTED" "$(section_of "$OUT26" pastbound)"
assert_contains "and the printed bound moves too" \
    "self-start bound: 2026-08-06T12:51:00Z (ladder 600s + first turn 60s)" "$OUT26"
# ...and the exemplar 26 minutes out is STILL late, so the move is a shift
# rather than the bound being switched off.
assert_eq "a bot 26 minutes out is late even against the longer ladder" \
    "LATE-UNEXPLAINED" "$(section_of "$OUT26" arishape)"
mk_unit bound ladderend 60

# ---- 10e: the first-turn allowance is the stated, overridable half ---------
# The ladder end is derived; the allowance is a MEASUREMENT (17-34s at load 31)
# and is therefore a knob. An operator who knows the boot was pathological must
# be able to widen it rather than argue with the page.
OUT27="$(run_snapshot "$SANE_JOURNAL" SELFSTART_FIRST_TURN_ALLOWANCE_S=3600)"
assert_eq "widening the allowance widens the bound" \
    "SELF-STARTED" "$(section_of "$OUT27" arishape)"

# ---- 10f: a receipt still decides — evidence beats absence of evidence -----
# THE LIVE CASE. On the real boot ari's payload predated the only receipt
# boundary, so the receipt did not cover it and it fell through to SELF-STARTED.
# A receipt that DOES cover a late payload must still win: "a human carried it"
# is a recorded fact, where late-unexplained is only the absence of one.
# mk_receipt reads $ROOT at call time, so the definition from case 8 writes into
# this case's scratch root without redefinition.
mk_receipt 2026-08-06 "{$RECEIPT_BASE,\"data\":{\"actor\":\"tester\",\"bots_rescued\":[],\"selfstart_measurement_valid_before\":\"2026-08-06T12:56:40Z\"}}"
OUT28="$(run_snapshot "$SANE_JOURNAL")"; RC28=$?
assert_eq "a receipt covering this boot still refuses the page" "6" "$RC28"
assert_eq "a late payload AFTER the rescue boundary is RESCUED, not late" \
    "RESCUED" "$(section_of "$OUT28" arishape)"
# And the half that is the actual #1203 defect: a payload BEFORE the boundary is
# not covered by the receipt, and must not be laundered into a self-start by
# that absence. This is ari on 2026-08-13, exactly.
assert_eq "a late payload the receipt does NOT cover is still late" \
    "LATE-UNEXPLAINED" "$(section_of "$OUT28" pastbound)"
assert_contains "and the contaminated headline reports late separately" \
    "late-unexplained" "$OUT28"
rm -f "$ROOT"/state/events/*.jsonl

# ---- 10g: no readable ladder — SUPPRESSED and disclosed -------------------
# MAX_RUNG=0 would ASSERT a zero-length ladder rather than measure one, putting
# the bound at boot+60s. On a launchd host, where no .service exists at all,
# that flags the entire fleet. boot_rung_for already draws this distinction by
# returning -1 rather than 0; the bound has to draw it too.
#
# Not firing is a silent pass unless it is said out loud, so the suppression is
# disclosed twice: in the header, and by NAME for every bot credited under it.
bound_units off
OUT29="$(run_snapshot "$SANE_JOURNAL")"; RC29=$?
assert_eq "with no ladder the bound is not applied" \
    "SELF-STARTED" "$(section_of "$OUT29" arishape)"
assert_contains "and the header says so rather than staying silent" \
    "self-start bound: NOT APPLIED" "$OUT29"
assert_contains "and says why" "the ladder end is not derived" "$OUT29"
assert_contains "and warns the count is unbounded above" \
    "unbounded above" "$OUT29"
assert_contains "every bot credited under the suppression is NAMED" \
    "the lateness bound could not be applied" "$OUT29"
assert_contains "and the exemplar is in that list" \
    "arishape" "$(printf '%s\n' "$OUT29" | awk '/lateness bound could not be applied/,/^$/')"
assert_eq "suppression is not a refusal — the page still stands" "0" "$RC29"
# The no-rung note lists EVERY bot in this arm, and it must not tell them the
# bound still covers them. A disclosure keyed on the wrong condition is longest
# and loudest exactly where it is false.
assert_absent "the no-rung note does not claim the bound still applies" \
    "The lateness bound still applies to them" "$OUT29"
assert_contains "it says the opposite, and points at the reason" \
    "The lateness bound is not applied to them either" "$OUT29"
bound_units on

# ---- 10i: an unformattable bound instant is REFUSED, never adopted ---------
# epoch_to_iso_utc yields an empty string when `date` cannot format the epoch,
# and an EMPTY boundary sorts BEFORE every record — so adopting it flags every
# self-starter at once, silently, on a page that otherwise reads normally. Same
# hazard pad_iso_frac names, in a second place, and the same one the padding
# mutant exposed. `date` is stubbed for THIS epoch only (BOOT+120), in both the
# GNU `@N` and BSD bare-`N` forms, so the boot instant and every other call still
# resolve and only the branch under test is driven.
mkdir -p "$T/bin3"
cat > "$T/bin3/date" <<'STUB'
#!/usr/bin/env bash
for a in "$@"; do
    case "$a" in "@1786020120"|"1786020120") exit 1 ;; esac
done
exec /usr/bin/date "$@"
STUB
chmod +x "$T/bin3/date"
OUT31="$(run_snapshot "$SANE_JOURNAL" "PATH=$T/bin3:$PATH")"; RC31=$?
assert_contains "an unformattable bound instant suppresses the bound" \
    "self-start bound: NOT APPLIED" "$OUT31"
assert_contains "and says the boundary would have sorted before everything" \
    "sorts before every record" "$OUT31"
# The property that matters: NOT every bot flagged.
assert_eq "no bot is flagged late off an empty boundary" \
    "0" "$(printf '%s\n' "$OUT31" | sed -n 's/^LATE, UNEXPLAINED.*(\([0-9]*\))$/\1/p')"
assert_eq "and the genuine self-starters are still credited" \
    "SELF-STARTED" "$(section_of "$OUT31" intime)"
assert_eq "the page still stands rather than refusing" "0" "$RC31"
# Positive control on the stub: without it the same fixtures DO produce late
# bots, so this arm is testing the guard and not a broken fixture.
assert_eq "and with a formattable bound the same fixtures still flag" \
    "LATE-UNEXPLAINED" "$(section_of "$(run_snapshot "$SANE_JOURNAL")" arishape)"

# ---- 10h: clock not SANE — SUPPRESSED and disclosed -----------------------
# The bound compares record timestamps against a derived instant, which is
# precisely the comparison a stale boot clock invalidates. Firing it there would
# manufacture a gap out of a clock fault. Positive control is 10a: the same
# fixtures under a SANE clock DO produce two late bots.
OUT30="$(run_snapshot "$STALE_JOURNAL")"
assert_contains "a stale boot clock suppresses the bound" \
    "self-start bound: NOT APPLIED" "$OUT30"
assert_contains "and names the clock as the reason" "the boot clock is STALE" "$OUT30"
assert_eq "so no bot is called late on an untrusted clock" \
    "0" "$(printf '%s\n' "$OUT30" | sed -n 's/^LATE, UNEXPLAINED.*(\([0-9]*\))$/\1/p')"

# ---- 11: a receipt written with json.dumps SPACING is still read -----------
# The #1202 class in a READER. `lib/selfstart-snapshot.sh` matched receipts with
# a compact-only pattern, while a rescuer writing via python `json.dumps` emits
# `"type": "fleet_rescue"` with a space. Measured live on the 2026-08-24 boot:
# two rescuers, one spaced and one compact, and the reader saw ONE of them —
# dropping a 17-name receipt whose boundary was also the EARLIER of the two. The
# page then printed 7 bots as LATE-UNEXPLAINED ("something woke these bots and
# nothing recorded what") about bots a receipt named explicitly.
#
# Three patterns had to move together, and this is the load-bearing part: the
# reader alone is NOT a fix. With only the reader widened, the row is admitted
# and then `row_field` / `row_rescued_names` — both compact-only — fail to parse
# it, so the boundary flips to UNUSABLE for EVERY receipt including the
# well-formed compact one. Verified on the live ledger: reader-only took
# SELF-STARTED from 3 to 0 and ADJUDICATE from 7 to 11. A partial fix here is
# worse than none, which is why all three are pinned in one test.
rm -f "$ROOT/state/events/fleet-2026-08-06.jsonl"
# CONTROL FIRST — with no receipt, `arishape` is LATE-UNEXPLAINED. Without this
# the arm below could pass by the fixture simply not being late, which is the
# way a spacing test most easily certifies nothing.
LATE_BEFORE="$(printf '%s\n' "$(run_snapshot "$BOOT")" | sed -n 's/^LATE, UNEXPLAINED.*(\([0-9]*\))$/\1/p')"
assert_eq "control: with no receipt the exemplar is an unexplained gap" \
    "LATE-UNEXPLAINED" "$(section_of "$(run_snapshot "$BOOT")" arishape)"

SPACED_BASE='"ts": "2026-08-06T08:50:00-04:00", "bot": "fleet", "type": "fleet_rescue", "source": "manual"'
mk_receipt 2026-08-06 "{$SPACED_BASE, \"data\": {\"actor\": \"tester\", \"bots_rescued\": [\"arishape\"], \"selfstart_measurement_valid_before\": \"$BOUNDARY\"}}"
OUT40="$(run_snapshot "$BOOT")"; RC40=$?

assert_eq "a spaced receipt is READ, so the page still refuses" "6" "$RC40"
assert_contains "and its boundary is usable, not UNUSABLE" "Rescue boundary   : $BOUNDARY" "$OUT40"
assert_contains "and its name list is parsed, not silently empty" \
    "Named as rescued  : 1 bot(s)" "$OUT40"
# The whole point of the bug, reproduced: a bot a receipt names EXPLICITLY was
# printed as "something woke these bots and nothing recorded what".
assert_eq "a receipt-named bot is RESCUED, not an unexplained gap" \
    "RESCUED" "$(section_of "$OUT40" arishape)"
assert_eq "so the unexplained-gap count drops by exactly that bot" \
    "$((LATE_BEFORE - 1))" "$(printf '%s\n' "$OUT40" | sed -n 's/^LATE, UNEXPLAINED.*(\([0-9]*\))$/\1/p')"

# ---- 11b: POSITIVE CONTROL — the correction exclusion survives both spacings
# The closing quote is what keeps `fleet_rescue_correction` rows out, and it is
# load-bearing: a correction carries none of a receipt's fields, so admitting
# one would refuse the whole page. Widening for an optional space must not
# widen into a prefix match. Both spellings are checked, because fixing the
# spacing is exactly the edit that could reintroduce this.
rm -f "$ROOT/state/events/fleet-2026-08-06.jsonl"
mk_receipt 2026-08-06 '{"ts":"2026-08-06T08:50:00-04:00","bot":"fleet","type":"fleet_rescue_correction","source":"manual","data":{"note":"compact correction"}}'
mk_receipt 2026-08-06 '{"ts": "2026-08-06T08:50:00-04:00", "bot": "fleet", "type": "fleet_rescue_correction", "source": "manual", "data": {"note": "spaced correction"}}'
OUT41="$(run_snapshot "$BOOT")"; RC41=$?
assert_eq "corrections alone are not receipts, so the page is a result again" "0" "$RC41"
assert_contains "and contamination is reported as unruled-out, not as covered" \
    "contamination CANNOT be ruled out" "$OUT41"

# ---- 11c: two rescuers, mixed spacing — EARLIEST boundary wins -------------
# Multiple rescuers per boot is the normal case, not the exception: the
# 2026-08-24 boot had two within 35 seconds. The loop already handles it
# (earliest wins, because a later rescue cannot un-suspect a bot an earlier one
# already covers) and that behaviour is pinned here so the spacing fix is not
# mistaken for a one-rescuer assumption. `row_field`'s rc=2 ambiguity is a
# WITHIN-ROW check — two rows with two boundaries is not ambiguous.
rm -f "$ROOT/state/events/fleet-2026-08-06.jsonl"
EARLIER="2026-08-06T12:45:00.000Z"
mk_receipt 2026-08-06 "{$SPACED_BASE, \"data\": {\"actor\": \"early\", \"bots_rescued\": [\"rescuedbot\"], \"selfstart_measurement_valid_before\": \"$EARLIER\"}}"
mk_receipt 2026-08-06 "{$RECEIPT_BASE,\"data\":{\"actor\":\"late\",\"bots_rescued\":[\"contradictor\"],\"selfstart_measurement_valid_before\":\"$BOUNDARY\"}}"
OUT42="$(run_snapshot "$BOOT")"
assert_contains "with two rescuers the EARLIEST boundary is adopted" \
    "Rescue boundary   : $EARLIER" "$OUT42"
assert_contains "and both name lists are unioned, not replaced" \
    "Named as rescued  : 2 bot(s)" "$OUT42"

echo
echo "  ---- $PASS/$TOTAL passed, $FAIL failed ----"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
