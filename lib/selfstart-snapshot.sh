#!/usr/bin/env bash
# lib/selfstart-snapshot.sh — record how many bots SELF-STARTED after a host boot.
#
# The #1002 pre-registration asks one question at the next unplanned reboot:
# how many bots came up without help? Run this BEFORE rescuing anything, then
# rescue immediately — the snapshot is what removes the trade between a clean
# experiment and leaving bots stranded (issue #1043).
#
# The prior 6-of-21 figure (2026-08-06, load 31) is NOT a baseline this script's
# output can be compared against: it was produced by presence-of-record, which
# cannot see an inbound-woken session or a half-submitted boot injection, so it
# counted bots this classifier does not. Re-derive it before claiming better,
# worse or flat.
#
# ── Two counts, opposite blind spots ────────────────────────────────────────
# RAW      assistant records in the newest transcript file.
#          Blind spot: a bot whose service failed never wrote a new transcript,
#          so `ls -t` returns the PRE-CRASH file with hundreds of turns and the
#          dead bot scores as a self-starter. Silent false POSITIVE.
# FILTERED assistant records timestamped after the boot instant.
#          Blind spot: absolute clock correctness. This host has no RTC battery,
#          so fake-hwclock restores a stale clock at boot and NTP corrects
#          minutes later. A genuine self-starter writing inside that window
#          carries a pre-boot timestamp and is dropped. False NEGATIVE, which
#          makes the new ladder look worse than it is.
# They agree when the clock is sane, so a DISAGREEMENT is itself the signal.
# Neither is silently preferred; both are printed and disagreement is called out.
#
# ── The counts are independent by construction ──────────────────────────────
# RAW reads exactly one file (the newest by mtime). FILTERED reads every
# transcript file the bot has. That asymmetry is deliberate: if both read the
# same file they would share a file-selection failure mode and stop being two
# independent measurements. It also covers the nasty tail of the stale-clock
# case, where the fresh file can carry an OLDER mtime than the pre-crash file
# and `ls -t` picks the wrong one.
#
# Scanning every file is affordable because a transcript file cannot span a
# reboot: start-bot.sh passes no --continue/--resume/--session-id, so every boot
# opens a new transcript (verified in #1043 against the script and against a real
# transcript). One file therefore belongs to exactly one boot, and reading its
# FIRST timestamped record classifies the whole file. Only files that classify
# as post-boot are read in full, and those are small. So there is no sampling
# cap here to declare — coverage is complete.
#
# ── Presence of a record cannot see a RESCUE, and that is the bigger hole ───
# Both counts above classify on records EXISTING. A bot that was carried by a
# rescuer has records exactly like one that woke up, so both counts are blind to
# rescue by construction and no amount of care with them closes it. On
# 2026-08-08 this host read 7 of 21 before a rescue and 19-20 of 21 three
# minutes after one, both stamped `result valid: yes`. The defect was never the
# number; it was the validity claim printed over it.
#
# So the classification is not "did records appear" but HOW THE SESSION CAME TO
# LIFE, which is boot_start_class in lib-common.sh (see there for the design
# rule). It types the first post-boot user record as a submitted payload, an
# inbound channel message, or nothing — and only then is a timestamp compared.
# Typing first is what makes the comparison meaningful: on the 2026-08-08 data
# the first post-boot user record for two bots was an inbound Telegram message,
# so comparing THAT instant against anything answers a question nobody asked.
#
# INBOUND-WOKEN is the class that matters. A bot woken by a human messaging it
# runs real work, reports normally, and looks healthy to every liveness signal —
# while never having started on its own. On 2026-08-08 the bot that ran the
# measurement and repaired twelve others was itself in this class. Liveness is
# not self-start, and nothing else in the estate separates them.
#
# ── The boundary comes from OUTSIDE the data, never from a gap in it ────────
# Once a payload is typed, self-start and rescue are told apart by comparing its
# instant against a `fleet_rescue` receipt written by whoever did the rescuing.
# It is tempting instead to encode the gap the populations showed on 2026-08-08
# (the two bands were tens of seconds apart with zero overlap). Do not. That
# separation is an artifact of the rescue having landed ~500s after boot; a
# rescue at boot+90s closes it. The gap is evidence that the instrument
# separates, NOT a threshold — and the bands were labelled FROM the receipt, so
# their separation says the partition is separable, not that it is correct.
#
# A receipt carries TWO independent facts, an explicit list of bots touched and
# a stamp, and a real one disagreed with itself. Both are used, and when they
# contradict the bot is refused BY NAME rather than resolved toward either.
# A receipt that discloses it was written after the event is telling you its
# stamp was typed rather than read, so the comparison is suppressed entirely —
# but the NAME LIST still stands, because a list of who was touched is not
# reconstructed by being written down late.
#
# ── SELF-STARTED is defined NEGATIVELY, so it needs an upper bound ──────────
# A payload that no receipt covers and that is not channel-shaped falls through
# to SELF-STARTED by elimination. Compose that with the fact above — the name
# list is known-incomplete, because a real rescue went unrecorded — and every
# wake nobody wrote down is reported as good news. Measured on the 2026-08-13
# 19:17:18Z boot: ari submitted its payload at 19:43:30Z, TWENTY-SIX MINUTES
# after boot, predating the only applicable receipt boundary, and was printed as
# "unaided". The ladder is measured in seconds; +26min is not a self-start under
# any reading. The honest figure for that boot was 2 of 21, not the 3 printed.
#
# So arrival is bounded, and a payload past the bound with nothing to explain it
# is LATE-UNEXPLAINED — its own answer, deliberately NOT folded into RESCUED,
# because nothing evidences a rescue. It reads as a gap, which is what it is.
#
# THE BOUND IS THE INSTANT THE PAGE ALREADY CLAIMS TO BE A RESULT. It is not a
# new constant and not a second knob: VALID_AT (ladder end + first-turn
# allowance) is where the too-early gate stops refusing, i.e. where the script
# asserts every bot has had its chance to start. A payload arriving after that
# is, by the script's OWN claim, arriving after it should have. Deriving the two
# separately would let the script simultaneously say "the window has closed" and
# "this arrival is still on time", which is incoherent — so they are one value.
#
# NOT the bot's own rung. THE REFUTATION IS MEASURED: otis has a composed rung
# of 15s and produced its payload at boot+97s; ravi's rung is 12s and its payload
# landed at boot+96s. Both are confirmed genuine self-starts and both are ~83s
# past their OWN rungs, so a per-bot bound flags BOTH at any allowance under 85s.
# That alone settles it, and it does not depend on the paragraph below.
#
# The EXPLANATION, which is reasoning and is NOT measured — stated separately so
# it is not mistaken for the evidence: a bot on an early rung boots into a
# machine where twenty more are still piling on behind it, so it plausibly meets
# the most contention, while a per-bot bound would give it the least time. We
# have no late-rung self-start to compare against, so this is a mechanism story
# for why the measurement came out as it did, not a second finding.
#
# TWO STATES SUPPRESS THE BOUND ENTIRELY, and each is disclosed rather than
# silently skipped:
#   no readable rung anywhere — the ladder end is not derived, and MAX_RUNG=0
#     would ASSERT a zero-length ladder rather than measure one. That is the same
#     distinction boot_rung_for already makes by returning -1 instead of 0. On a
#     launchd host, where no .service exists at all, taking 0 would put the bound
#     at boot+60s and flag the whole fleet.
#   clock not SANE — the bound compares record timestamps against a derived
#     instant, and a stale boot clock is precisely the condition under which the
#     script already says those timestamps cannot be trusted.
# The too-early gate treats a missing ladder end as 0 and this does not, which
# is a deliberate asymmetry rather than drift: from the same missing fact the
# two err in opposite directions. There, 0 makes the gate FIRE LESS and risks
# calling a page a result early; here, 0 would make the bound FIRE MORE and
# manufacture gaps out of nothing. Each fails toward not inventing something.
#
# It sets no exit code, and that is the same page-versus-row line the rest of
# the script draws: 4/5/6 say THE PAGE is not a result, while LATE-UNEXPLAINED
# and ADJUDICATE are per-bot verdicts on a page that is otherwise fine.
#
# ── What this script depends on, and what it still refuses to depend on ─────
# It sources lib-common.sh for exactly two doors — the strict roster
# (declared_bots_strict) and the boot classifier (boot_start_class) — because
# both are contracts other callers need and a private copy here would be the
# second reader that eventually disagrees with the first. It still does NOT call
# the claudlobby CLI, touch the network, or require any bot to be up, and
# journalctl is used only for the clock assertion and degrades to UNKNOWN when
# absent. The one time this script runs is the one time the host is least
# healthy, so a missing lib-common.sh is a REFUSAL rather than a fallback: a
# measurement has no degraded mode, and a private reimplementation of a shared
# predicate is how a fleet-wide fact quietly forks.
#
# ── Why `set -e` is deliberately absent ─────────────────────────────────────
# House rule is `set -euo pipefail`. Not here, and the reason is the contract:
# under -e an unhandled failure at bot 14 of 21 aborts and prints a page that
# READS AS COMPLETE while covering two thirds of the estate. That is precisely
# the silent-denominator failure this script exists to prevent, so the failure
# mode is worse than the one -e protects against. Errors that matter are checked
# explicitly instead, and the denominator is validated up front and fails loud.
#
# ── Test seams ──────────────────────────────────────────────────────────────
# A real crash cannot be staged, so these env vars let the harness drive the
# real logic against synthetic fixtures. They are also useful operationally for
# re-deriving a snapshot against a known boot instant.
#   CLAUDLOBBY_ROOT               repo root (denominator source)
#   CLAUDE_CONFIG_DIR             transcript root (default ~/.claude)
#   SELFSTART_BOOT_EPOCH          override the boot instant
#   SELFSTART_JOURNAL_BOOT_EPOCH  override the journal boot record
#   SELFSTART_ALLOW_PARTIAL=1     proceed past an unparseable manifest, loudly
#   SELFSTART_ALLOW_DUPLICATE_NAMES=1
#                                 proceed past a bot name declared in two
#                                 fleets, loudly. Separate from ALLOW_PARTIAL on
#                                 purpose: one override per fatal condition, so
#                                 accepting one degradation never waives another
set -uo pipefail

# NOT "the baseline to beat" any more, and the wording is deliberate. The 6 of
# 21 was taken with an instrument that could not see an inbound-woken session or
# a half-submitted injection, so it counts bots this classifier does not. A
# number from here and that number are not on the same scale, and printing them
# side by side as though they were is how "flat", "better" or "worse" gets
# claimed without support. The baseline has to be RE-DERIVED with the typed
# classifier before any comparison is legitimate (#843).
BASELINE_TEXT="6 of 21 (2026-08-06) — NOT COMPARABLE, see below"
BASELINE_NOTE="the 6 of 21 was measured by presence-of-record, which cannot see
                        inbound-woken or half-submitted boots. Re-derive it with this
                        classifier before calling any new figure better or worse."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${CLAUDLOBBY_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
CFG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

# Exported because the shared doors resolve the estate from it, and the test
# seam sets it. Assigned back so an unset environment still gets the derived
# value rather than tripping the doors' own :? guards.
CLAUDLOBBY_ROOT="$ROOT"
export CLAUDLOBBY_ROOT

if [ ! -r "$SCRIPT_DIR/lib-common.sh" ]; then
    echo "selfstart-snapshot: cannot read $SCRIPT_DIR/lib-common.sh — refusing." >&2
    echo "  The roster and boot-classifier doors live there. Reimplementing them" >&2
    echo "  here would fork a fleet-wide predicate, and a measurement taken with" >&2
    echo "  a private copy of one is not comparable to the baseline." >&2
    exit 3
fi
# shellcheck source=lib-common.sh
. "$SCRIPT_DIR/lib-common.sh"

# lib-common.sh runs `set -euo pipefail` AT SOURCE TIME, so sourcing it re-arms
# errexit inside a script whose whole contract is not having it. That is not a
# style point: measured on the first run after wiring, the per-bot sweep aborted
# at the first bot with no transcript and the script exited having printed
# NOTHING. Under a smaller fault it would instead have printed a page covering
# part of the estate, which is the silent-denominator failure this file exists
# to prevent, arriving through the helper it just started trusting.
#
# So errexit is disabled again immediately, and deliberately: -u and pipefail
# are kept, only -e is dropped. Do not "tidy" this line away — see the `set -e`
# note in the header for what it buys.
set +e

# install_error_trap is likewise NOT armed, for the same reason: an ERR trap
# that aborts mid-sweep prints a two-thirds page.
#
# lib-common.sh also installs its own EXIT trap (_lc_cleanup), and bash keeps
# exactly one per signal, so the trap below would silently replace it and leak
# whatever lib-common had queued for cleanup. Chained rather than overwritten.
_selfstart_cleanup() {
    rm -rf "${TMP:-}"
    if type _lc_cleanup >/dev/null 2>&1; then _lc_cleanup; fi
}

TMP="$(mktemp -d "${TMPDIR:-/tmp}/selfstart.XXXXXX")" || {
    echo "selfstart-snapshot: cannot create temp dir" >&2; exit 3; }
trap '_selfstart_cleanup' EXIT

usage() {
    cat <<'EOF'
Usage: selfstart-snapshot.sh [--help]

Records how many declared bots self-started after the current host boot.
Run BEFORE rescuing anything. Prints the headline first, lists after.

Exit codes:
  0  snapshot printed
  2  denominator could not be trusted (unparseable fleet.yaml, no bots found,
     duplicate bot name across fleets). Nothing is printed but the banner.
  3  boot instant could not be determined, or no temp dir.
  4  snapshot ran but did not cover every declared bot. The page is printed
     and stamped INCOMPLETE; N is short and must not be used.
  5  ran too early to be a result — the boot ladder had not finished, or the
     first-turn allowance had not elapsed. The page is printed and stamped
     TOO EARLY; re-run at the instant it names. Takes 4 if both apply, since
     re-running fixes early and need not fix incomplete.
  6  ran too LATE to be a result — a fleet_rescue receipt covers this boot, so
     the page is a reconstruction rather than the measurement. Printed and
     stamped CONTAMINATED. Outranks 5 (re-running never un-contaminates a boot)
     and is outranked by 4.
  7  the rescue receipts could not be READ — the plane (where a fleet_rescue
     receipt lands) was unreachable for at least one declared fleet, so whether
     a rescue covers this boot is UNKNOWN. Printed and stamped RESCUE UNKNOWN.
     A receipt gate that fails OPEN is the one failure this measurement must
     never have, so an unreadable source refuses like a covering receipt would.
     Outranks 6 and 5, outranked by 4.
EOF
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    "") ;;
    *) echo "selfstart-snapshot: unknown argument: $1" >&2; usage >&2; exit 3 ;;
esac

# ── Loud failure ────────────────────────────────────────────────────────────
# The denominator has no degraded mode. See the note above list_manifests.
#
# ESCAPE_HINT names the override for THIS refusal and nothing else. Each fatal
# condition owns its own escape, because an override advertised on one banner
# and honoured by a second, unrelated check is how a silent denominator gets
# back in through the door built to make partiality loud (#1045 review).
ESCAPE_HINT="SELFSTART_ALLOW_PARTIAL=1 to accept a declared-partial denominator"
die_loud() {
    echo
    echo "############################################################"
    echo "##  SELF-START SNAPSHOT REFUSED — DENOMINATOR NOT TRUSTED  ##"
    echo "############################################################"
    echo "##"
    while [ $# -gt 0 ]; do echo "##  $1"; shift; done
    echo "##"
    echo "##  No counts are printed. A wrong N would be compared"
    echo "##  against the baseline ($BASELINE_TEXT)"
    echo "##  and believed. Fix the manifest, or re-run with"
    echo "##  $ESCAPE_HINT."
    echo "############################################################"
    echo
    exit 2
}

# ── Boot instant ────────────────────────────────────────────────────────────
# GOTCHA (#1043): never `date -u -d "$(uptime -s)"`. uptime -s prints LOCAL
# time; -u makes date re-read that local string AS UTC, landing one offset off —
# silently wrong rather than obviously wrong. Parse local -> epoch first, then
# format FROM the epoch with -u.
resolve_boot_epoch() {
    if [ -n "${SELFSTART_BOOT_EPOCH:-}" ]; then
        printf '%s\n' "$SELFSTART_BOOT_EPOCH"; return 0
    fi
    local s e
    s="$(uptime -s 2>/dev/null)"
    if [ -n "$s" ]; then
        e="$(date -d "$s" +%s 2>/dev/null)"
        if [ -n "$e" ]; then printf '%s\n' "$e"; return 0; fi
    fi
    # macOS has no `uptime -s`; kern.boottime prints  { sec = 1786..., usec = ... }
    s="$(sysctl -n kern.boottime 2>/dev/null)"
    if [ -n "$s" ]; then
        e="$(printf '%s\n' "$s" | sed -n 's/.*sec *= *\([0-9][0-9]*\).*/\1/p' | head -1)"
        if [ -n "$e" ]; then printf '%s\n' "$e"; return 0; fi
    fi
    # Linux without uptime(1): /proc/uptime is monotonic seconds since boot.
    if [ -r /proc/uptime ]; then
        local up now
        up="$(cut -d. -f1 < /proc/uptime 2>/dev/null)"
        now="$(date +%s 2>/dev/null)"
        if [ -n "$up" ] && [ -n "$now" ]; then printf '%s\n' "$((now - up))"; return 0; fi
    fi
    return 1
}

epoch_to_iso_utc() {
    date -u -d "@$1" +%Y-%m-%dT%H:%M:%S 2>/dev/null \
        || date -u -r "$1" +%Y-%m-%dT%H:%M:%S 2>/dev/null
}

# The journal boot record is what the clock SAID at boot; `uptime -s` is
# now-minus-monotonic-uptime, so it is the TRUE boot instant once NTP has
# corrected. A gap between them is the stale-clock signal, measured rather
# than assumed — which is the assumption the whole FILTERED count rests on.
journal_boot_epoch() {
    if [ -n "${SELFSTART_JOURNAL_BOOT_EPOCH:-}" ]; then
        printf '%s\n' "$SELFSTART_JOURNAL_BOOT_EPOCH"; return 0
    fi
    command -v journalctl >/dev/null 2>&1 || return 1
    local line e
    line="$(journalctl -b -o short-iso --no-pager 2>/dev/null | head -1 | awk '{print $1}')"
    [ -n "$line" ] || return 1
    e="$(date -d "$line" +%s 2>/dev/null)"
    [ -n "$e" ] || return 1
    printf '%s\n' "$e"
}

BOOT_EPOCH="$(resolve_boot_epoch)"
if [ -z "$BOOT_EPOCH" ]; then
    echo "selfstart-snapshot: cannot determine boot instant (no uptime -s, no kern.boottime, no /proc/uptime)" >&2
    exit 3
fi
BOOT_ISO="$(epoch_to_iso_utc "$BOOT_EPOCH")"
# Records carry sub-second precision (2026-08-06T13:01:12.939Z). Padding the
# boundary to .000 keeps the lexicographic compare apples-to-apples, so a record
# in the boot second is not mis-ordered by the missing fractional part.
BOOT_CMP="${BOOT_ISO}.000Z"
NOW_EPOCH="$(date +%s)"
NOW_ISO="$(epoch_to_iso_utc "$NOW_EPOCH")"
ELAPSED=$(( NOW_EPOCH - BOOT_EPOCH ))
[ "$ELAPSED" -lt 0 ] && ELAPSED=0

JOURNAL_EPOCH="$(journal_boot_epoch)"
if [ -z "$JOURNAL_EPOCH" ]; then
    CLOCK_VERDICT="UNKNOWN"
    CLOCK_DETAIL="journal boot record unavailable — stale-clock check could not run"
else
    CLOCK_DELTA=$(( BOOT_EPOCH - JOURNAL_EPOCH ))
    [ "$CLOCK_DELTA" -lt 0 ] && CLOCK_DELTA=$(( -CLOCK_DELTA ))
    if [ "$CLOCK_DELTA" -le 60 ]; then
        CLOCK_VERDICT="SANE"
        CLOCK_DETAIL="uptime -s and journal boot record agree within ${CLOCK_DELTA}s"
    else
        CLOCK_VERDICT="STALE"
        CLOCK_DETAIL="uptime -s and journal boot record differ by ${CLOCK_DELTA}s — clock was stale at boot, FILTERED under-counts"
    fi
fi

# ── Rescue receipt — the contamination gate ─────────────────────────────────
# States, and each is a different KIND of answer rather than a confidence level:
#   NONE        no receipt covers this boot. Contamination cannot be ruled out
#               at all — see the honest limit printed with the report.
#   USABLE      stamp and names both trustworthy: full comparison.
#   NAMES-ONLY  the stamp is reconstructed, ambiguous or not a comparable
#               instant. The comparison is suppressed; the name list still
#               applies, because it is not reconstructed by being written late.
RESCUE_STATE="NONE"
RESCUE_STAMP=""
RESCUE_STAMP_CMP=""
RESCUE_WHY=""
: > "$TMP/rescued_names"

# Pad a fractionless stamp, the same trick BOOT_CMP uses: without it
# 14:15:00.500Z sorts BEFORE 14:15:00Z, because "." is below "Z", and a bot half
# a second the wrong side of the boundary silently flips class.
pad_iso_frac() {
    case "$1" in
        *.*Z) printf '%s' "$1" ;;
        *Z)   printf '%s' "${1%Z}.000Z" ;;
        # Unreachable from the only call site, which is gated behind
        # iso_utc_shaped and therefore only ever hands over a stamp ending in Z.
        # Kept deliberately rather than deleted: a `case` that matches no arm
        # prints NOTHING and still returns 0, so removing this line does not
        # delete dead code — it turns a total function into one that silently
        # yields an empty string for the next caller that is not gated the same
        # way, and an empty boundary sorts BEFORE every instant, which is the
        # exact failure the padding exists to prevent. The reachability is a
        # property of the call site, not of the padder; leaving the identity arm
        # here keeps that precondition out of the callee.
        *)    printf '%s' "$1" ;;
    esac
}

# A boundary this script may compare against has to be an ISO instant in UTC.
# Anything else — a local offset, a date alone, prose — is not comparable, and
# guessing at one is how a typed value becomes a measured one.
iso_utc_shaped() {
    case "$1" in
        [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*Z) return 0 ;;
        *) return 1 ;;
    esac
}

# Receipts live on the PLANE (F18 closure R2b-2): a `fleet_rescue` receipt is a
# system event on the rescued FLEET's identity, landed by emit_fleet_event, so
# the plane is asked per declared fleet — every fleet.yaml on the host — from
# the day BEFORE the boot date (the same window the dated files once drew: a
# boot either side of midnight must not miss its own rescue). Rows print as the
# legacy JSON lines, `"type": "fleet_rescue"` only (a correction row is its own
# type and never admitted), into $TMP/receipt_rows for the parsers below.
#
# THE LOAD-BEARING RULE: a fleet the plane cannot answer for — no db, a schema
# the reader cannot use, a fleet it holds no identity for — is RECORDED as
# unreadable and the page refuses (exit 7), never read as "no receipt". Missing
# a receipt fails OPEN, the one direction this gate must never fail in, and an
# instrument that cannot be reached is not evidence of absence.
RECEIPTS_UNREACHABLE=0
RECEIPTS_WHY=""
receipts_from_plane() {  # receipts_from_plane <since-date YYYY-MM-DD>
    local since="$1" fname man n_before n_after _rc
    while IFS="$(printf '\t')" read -r fname man; do
        [ -n "$fname" ] || continue
        n_before="$(wc -l < "$TMP/receipt_rows" | tr -d ' ')"
        _rc=0
        # The window bounds the scan, nothing more: a since the clock could not
        # format (every date call failing) is dropped rather than sent as a
        # non-instant — the boundary compare below still decides applicability,
        # and an unbounded read of one fleet's receipts is cheap.
        python3 -S -E "$SCRIPT_DIR/plane-lookup.py" --root "$ROOT" --events --fleet "$fname" \
            --type fleet_rescue ${since:+--since "${since}T00:00:00Z"} >> "$TMP/receipt_rows" 2>"$TMP/receipt_err" || _rc=$?
        if [ "$_rc" -ne 0 ]; then
            RECEIPTS_UNREACHABLE=1
            RECEIPTS_WHY="${RECEIPTS_WHY:+$RECEIPTS_WHY; }fleet $fname (rc $_rc): $(tail -1 "$TMP/receipt_err" 2>/dev/null | cut -c1-140)"
            continue
        fi
        n_after="$(wc -l < "$TMP/receipt_rows" | tr -d ' ')"
        [ "$n_after" -gt "$n_before" ] && printf '%s\n' "$fname" >> "$TMP/receipt_fleets"
    done < "$TMP/fleet_pairs"
    return 0
}

# One JSON string field, and EMPTY when the row carries more than one distinct
# value for it. Field-name extraction without jq is a structural read, but it
# cannot tell a real key from the same text quoted inside a prose value.
# Refusing on ambiguity is cheaper than being subtly wrong about which instant
# bounds the measurement. rc 1 absent, rc 2 ambiguous.
row_field() {
    local row="$1" key="$2" vals n
    vals="$(printf '%s\n' "$row" | grep -oE "\"$key\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" \
            | sed -E "s/^\"$key\"[[:space:]]*:[[:space:]]*\"//; s/\"\$//" | sort -u)"
    [ -n "$vals" ] || return 1
    n="$(printf '%s\n' "$vals" | wc -l | tr -d ' ')"
    [ "$n" = "1" ] || return 2
    printf '%s' "$vals"
}

# Names out of data.bots_rescued. The key itself sits inside the captured
# segment, so it is dropped BY NAME rather than by position.
row_rescued_names() {
    printf '%s\n' "$1" \
        | grep -oE '"bots_rescued"[[:space:]]*:[[:space:]]*\[[^]]*\]' | head -1 \
        | grep -o '"[^"]*"' | sed 's/^"//; s/"$//' \
        | grep -v '^bots_rescued$'
}

# Every fleet.yaml on the host, enumerated HERE because the receipt read below
# asks the plane per declared fleet (the roster itself is built further down
# from the same list).
discover_fleet_manifests | sort -u > "$TMP/fleet_pairs"
cut -f2 < "$TMP/fleet_pairs" | sort -u > "$TMP/manifests"
if [ ! -s "$TMP/manifests" ]; then
    die_loud "No fleet.yaml found under $ROOT/local/" \
             "Looked at local/*/fleet.yaml and local/*/*/fleet.yaml"
fi

SCAN_SINCE="$(epoch_to_iso_utc "$(( BOOT_EPOCH - 86400 ))")"
SCAN_SINCE="${SCAN_SINCE%%T*}"
[ -n "$SCAN_SINCE" ] || SCAN_SINCE="${BOOT_ISO%%T*}"

: > "$TMP/receipt_rows"
: > "$TMP/receipt_fleets"
receipts_from_plane "$SCAN_SINCE"

while IFS= read -r row; do
    [ -n "$row" ] || continue
    r_stamp="$(row_field "$row" "selfstart_measurement_valid_before")"; r_rc=$?
    r_cmp=""
    if [ "$r_rc" -eq 0 ] && iso_utc_shaped "$r_stamp"; then
        r_cmp="$(pad_iso_frac "$r_stamp")"
    fi

    # A receipt whose boundary predates this boot belongs to an EARLIER one.
    # A receipt with no usable boundary cannot be placed, so the file-date
    # window is all that bounds it and it is treated as applicable — guessing
    # the other way fails open.
    if [ -n "$r_cmp" ] && [ ! "$r_cmp" \> "$BOOT_CMP" ]; then
        continue
    fi

    row_rescued_names "$row" >> "$TMP/rescued_names"

    if [ -z "$r_cmp" ]; then
        RESCUE_STATE="NAMES-ONLY"
        if [ "$r_rc" -eq 2 ]; then
            RESCUE_WHY="receipt carries more than one distinct measurement boundary — ambiguous"
        elif [ "$r_rc" -ne 0 ]; then
            RESCUE_WHY="receipt carries no selfstart_measurement_valid_before boundary"
        else
            RESCUE_WHY="receipt boundary is not a comparable UTC instant: $r_stamp"
        fi
        continue
    fi

    # A `recorded` field is DISCLOSURE: a receipt written in the same operation
    # that read the clock has nothing to disclose and omits it. Presence is
    # therefore the property, and the prose is deliberately NOT read — matching
    # on what it SAYS would be a pattern match standing in for a property check,
    # and this one is structural.
    #
    # RECEIPT-WRITER CONTRACT, the other half of this fix and not implemented
    # here: read the stamp in the same operation that writes the row, and omit
    # `recorded` when you do. This side only declines to trust what the writer
    # itself admits was reconstructed.
    if printf '%s\n' "$row" | grep -q '"recorded":'; then
        RESCUE_STATE="NAMES-ONLY"
        RESCUE_WHY="receipt declares itself recorded after the fact, so its boundary was typed rather than read"
        continue
    fi

    [ "$RESCUE_STATE" = "NAMES-ONLY" ] || RESCUE_STATE="USABLE"
    # Earliest boundary wins when several rescues cover one boot: it is the most
    # conservative, and a later rescue cannot un-suspect a bot an earlier one
    # already covers.
    if [ -z "$RESCUE_STAMP_CMP" ] || [ "$r_cmp" \< "$RESCUE_STAMP_CMP" ]; then
        RESCUE_STAMP="$r_stamp"; RESCUE_STAMP_CMP="$r_cmp"
    fi
done < "$TMP/receipt_rows"

# One place where a suppressed boundary is actually removed, so no later branch
# can reach a stamp the state above says must not be compared against.
if [ "$RESCUE_STATE" != "USABLE" ]; then
    RESCUE_STAMP=""; RESCUE_STAMP_CMP=""
fi
N_RESCUE_NAMED="$(sort -u "$TMP/rescued_names" | grep -c . || true)"
[ -n "$N_RESCUE_NAMED" ] || N_RESCUE_NAMED=0

# ── Denominator ─────────────────────────────────────────────────────────────
# Enumerated from the bots DECLARED in every fleet.yaml, as a union across
# fleets, then transcripts are joined onto it. Never from directories on disk.
# Two failure modes that a directory walk cannot see:
#   - a bot whose service failed so hard it never created a transcript is
#     skipped by a naive "for each transcript" loop and vanishes from BOTH
#     lists, so 6 of 21 silently becomes 6 of 19 and the baseline stops being
#     comparable. It must print as a strand, never be absent.
#   - a leftover directory for an undeclared bot would inflate the total and
#     read as a permanent strand forever.
#
# DO NOT copy claudlobby/composer.py::_fleet_bot_count() here. It reads a
# sibling manifest and returns 0 on ANY exception, and it is RIGHT to do so:
# one fleet broken config must never block another fleet generate, and the cost
# of that soft failure is a stale boot-ladder offset, which is the un-offset
# behaviour rather than a new failure mode. That reasoning does not transfer.
# `generate` produces an ACTION with a valid degraded mode; this script produces
# a MEASUREMENT, and a measurement has no degraded mode — a wrong N is worse
# than no N, because it will be compared against the baseline and believed.
# Same code shape, opposite correct answer. The discriminator is what the caller
# does with an EMPTY result, not action versus measurement (#1146 — the older
# framing endorsed a destructive prune in #1143): soft is right where empty means
# do nothing, and wrong here, where an empty roster licenses a WRONG N. Hence:
# parse failure is loud and fatal here.
# The roster comes from declared_bots_strict, the LOUD door — never
# parse_fleet_bots. See that function for why there are two doors and why
# swapping this one for the soft sibling silently reinstates the bug above.
# (the manifests were enumerated above, ahead of the receipt read — the plane
# is asked per declared fleet)

: > "$TMP/declared"
: > "$TMP/badman"
declared_bots_strict "$TMP/badman" > "$TMP/declared"

PARTIAL=0
if [ -s "$TMP/badman" ]; then
    if [ "${SELFSTART_ALLOW_PARTIAL:-0}" = "1" ]; then
        PARTIAL=1
    else
        # Build one banner line per broken manifest, then hand them to die_loud
        # as separate arguments. bash 3.2 has no arrays worth the noise here, so
        # the lines are collected in a file and read back positionally.
        set --
        while IFS="$(printf '\t')" read -r bad why; do
            set -- "$@" "$bad — $why"
        done < "$TMP/badman"
        die_loud "A fleet manifest could not be parsed, so the union of declared" \
                 "bots is incomplete and N would be short by a whole fleet:" \
                 "" "$@"
    fi
fi

if [ ! -s "$TMP/declared" ]; then
    die_loud "No bots declared in any fleet.yaml under $ROOT/local/"
fi

# A duplicate name across fleets makes every row keyed on bot name ambiguous:
# two distinct bots print under one identifier, and a page whose identifiers do
# not identify anything is not a measurement. Fatal, not a warning.
#
# SELFSTART_ALLOW_PARTIAL deliberately does NOT reach this check, and that is a
# fix rather than an oversight. It used to, which meant setting the flag for its
# real purpose — one manifest that will not parse — silently waived an unrelated
# fatal condition as well, with no banner and no PARTIAL stamp in the headline,
# exiting 0 on a page that read as entirely normal. That is the silent
# denominator this whole script exists to prevent, reachable through the door
# built to make partiality loud. Found by vera in review of #1045.
#
# A duplicate gets its own override because it needs its own banner: the two
# conditions degrade the page differently and an operator must be told which
# one they accepted.
DUPES="$(cut -f1 "$TMP/declared" | sort | uniq -d)"
DUP_TOLERATED=0
if [ -n "$DUPES" ]; then
    if [ "${SELFSTART_ALLOW_DUPLICATE_NAMES:-0}" = "1" ]; then
        DUP_TOLERATED=1
    else
        ESCAPE_HINT="SELFSTART_ALLOW_DUPLICATE_NAMES=1 to accept ambiguous rows"
        die_loud "Bot name declared in more than one fleet.yaml — two distinct" \
                 "bots would print under one identifier:" \
                 "$(printf '%s' "$DUPES" | tr '\n' ' ')" \
                 "" \
                 "SELFSTART_ALLOW_PARTIAL does NOT waive this. It is scoped to" \
                 "an unparseable manifest and nothing else."
    fi
fi

TOTAL="$(wc -l < "$TMP/declared" | tr -d ' ')"

# ── Per-bot snapshot ────────────────────────────────────────────────────────
# Transcript dir is the bot working directory with / replaced by -, under the
# Claude Code projects root.
transcript_dir_for() { printf '%s/projects/%s\n' "$CFG_DIR" "$(printf '%s' "$1" | tr '/' '-')"; }

# A bot cannot have self-started before systemd has launched it, and the boot
# ladder means most of them have not for the first minute. Each bot waits on an
# `ExecStartPre=/bin/sleep N` rung composed into its own unit (3s stagger,
# host-global, so a 21-bot host runs rungs 0..60). Read the rung rather than
# assume one: the stagger is a composer constant that will move, and hardcoding
# it here would silently decay.
#
# Anchored to start-of-line because the composed unit carries an explanatory
# COMMENT mentioning ExecStartPre, which an unanchored match would read as the
# directive. Returns -1 when no rung can be read (no unit, or a launchd plist,
# which staggers elsewhere) — never 0, because "no gate" and "gate at zero" must
# not be the same answer: an unreadable rung has to be reported as unknown
# rather than quietly asserting the bot was due.
boot_rung_for() {
    local d="$1" u r
    for u in "$d"/*.service; do
        [ -f "$u" ] || continue
        r="$(grep -E '^[[:space:]]*ExecStartPre=.*sleep[[:space:]]+[0-9]+' "$u" 2>/dev/null \
             | sed -n 's/.*sleep[[:space:]]*\([0-9][0-9]*\).*/\1/p' | tail -1)"
        if [ -n "$r" ]; then printf '%s\n' "$r"; return 0; fi
    done
    printf '%s\n' "-1"
}

# ── Rung pre-pass ───────────────────────────────────────────────────────────
# The ladder END bounds arrival for every bot, so it has to be known BEFORE the
# sweep that uses it — it cannot be accumulated by the same loop it gates. Rungs
# are read once here and carried into the sweep as a fourth field rather than
# boot_rung_for being called twice per bot, so there is still exactly one read
# of each composed unit and no second place for the two to disagree.
: > "$TMP/rungs"
: > "$TMP/norung"
: > "$TMP/declared_rung"
while IFS="$(printf '\t')" read -r bot fleet botdir; do
    [ -n "$bot" ] || continue
    rung="$(boot_rung_for "$botdir")"
    if [ "$rung" -ge 0 ]; then
        printf '%s\n' "$rung" >> "$TMP/rungs"
    else
        printf '%s\n' "$bot" >> "$TMP/norung"
    fi
    printf '%s\t%s\t%s\t%s\n' "$bot" "$fleet" "$botdir" "$rung" >> "$TMP/declared_rung"
done < "$TMP/declared"

# ── Is this reading a result yet? ───────────────────────────────────────────
# Two gates, and they are different kinds of claim (#1050).
#
# HARD, and derived: a bot whose ExecStartPre rung has not elapsed has not been
# launched, full stop. That is a fact about systemd, not an estimate, so those
# bots are NOT-YET-DUE rather than stranded.
#
# SOFT, and stated: a bot that HAS launched still needs time to reach a first
# turn. That latency is a measurement, not a derivation — 17-34s observed at
# load 31 on 2026-08-06, and longer under worse load, which is exactly when
# this gets run. The allowance is therefore printed with its provenance rather
# than applied silently, and it is overridable. On a 21-bot host the two gates
# together land at boot+120s, matching the operating rule.
FIRST_TURN_ALLOWANCE_S="${SELFSTART_FIRST_TURN_ALLOWANCE_S:-60}"
MAX_RUNG=0
if [ -s "$TMP/rungs" ]; then
    MAX_RUNG="$(sort -n "$TMP/rungs" | tail -1)"
fi
VALID_AT=$(( BOOT_EPOCH + MAX_RUNG + FIRST_TURN_ALLOWANCE_S ))
TOO_EARLY=0
[ "$NOW_EPOCH" -lt "$VALID_AT" ] && TOO_EARLY=1

# ── The lateness bound on SELF-STARTED ──────────────────────────────────────
# Same instant as VALID_AT, deliberately and not by coincidence — see the header
# note. Past it, a payload nothing accounts for is LATE-UNEXPLAINED rather than
# a self-start. Suppressed in the two states where the bound would be asserted
# rather than derived, and disclosed in both.
SELF_START_BOUND_ISO=""
SELF_START_BOUND_CMP=""
BOUND_STATE="APPLIED"
BOUND_WHY=""
if [ ! -s "$TMP/rungs" ]; then
    BOUND_STATE="NO-LADDER"
    BOUND_WHY="no boot rung readable for any declared bot, so the ladder end is not derived. Taking it as 0 would assert a zero-length ladder rather than measure one"
elif [ "$CLOCK_VERDICT" != "SANE" ]; then
    BOUND_STATE="CLOCK-$CLOCK_VERDICT"
    BOUND_WHY="the boot clock is $CLOCK_VERDICT, and the bound compares record timestamps against a derived instant — the very comparison a bad boot clock invalidates"
else
    SELF_START_BOUND_ISO="$(epoch_to_iso_utc "$VALID_AT")"
    # The formatted instant is CHECKED before it is adopted, through the same
    # helper the receipt boundary goes through. An unformattable epoch yields an
    # empty string, and an empty boundary sorts BEFORE every record — so the
    # comparison below would flag EVERY self-starter at once, silently, on a page
    # that otherwise reads normally. Exactly the hazard pad_iso_frac names, in a
    # second place. Refuse rather than adopt.
    if iso_utc_shaped "${SELF_START_BOUND_ISO}Z"; then
        # Same fractional padding as BOOT_CMP: records carry sub-second precision
        # and "." sorts below "Z", so an unpadded boundary flips a bot half a
        # second the wrong side of it.
        SELF_START_BOUND_CMP="${SELF_START_BOUND_ISO}.000Z"
    else
        SELF_START_BOUND_ISO=""
        BOUND_STATE="NO-INSTANT"
        BOUND_WHY="the bound instant could not be formatted from epoch $VALID_AT, and an unshaped boundary sorts before every record — adopting it would flag every self-starter at once"
    fi
fi

: > "$TMP/rows"
: > "$TMP/noprompt"
: > "$TMP/unbounded"
while IFS="$(printf '\t')" read -r bot fleet botdir rung; do
    [ -n "$bot" ] || continue
    tdir="$(transcript_dir_for "$botdir")"
    files="$(ls -1t "$tdir"/*.jsonl 2>/dev/null)"

    not_due=0
    [ "$rung" -ge 0 ] && [ "$rung" -gt "$ELAPSED" ] && not_due=1

    if [ -z "$files" ]; then
        # Declared, but no transcript at all — the third blind spot. A naive
        # transcript walk drops this bot from both lists entirely. Still not a
        # strand if its rung has not elapsed: systemd has not launched it, so
        # there is nothing it could have written.
        if [ "$not_due" -eq 1 ]; then
            printf '%s\t%s\tNOT-YET-DUE\t0\t0\t-\t-\tboot rung %ss, only %ss since boot — not launched yet\t-\n' \
                "$bot" "$fleet" "$rung" "$ELAPSED" >> "$TMP/rows"
        else
            printf '%s\t%s\tSTRAND\t0\t0\t-\t-\tno transcript file at all\t-\n' "$bot" "$fleet" >> "$TMP/rows"
        fi
        continue
    fi

    newest="$(printf '%s\n' "$files" | head -1)"
    raw="$(grep -c '"type":"assistant"' "$newest" 2>/dev/null)"
    [ -n "$raw" ] || raw=0

    # First timestamped record of the newest file — the ambiguous-case
    # discriminator: a fresh file postdates boot, a pre-crash file does not.
    first_ts="$(grep -m1 -o '"timestamp":"[^"]*"' "$newest" 2>/dev/null | sed 's/.*:"//; s/"$//')"
    [ -n "$first_ts" ] || first_ts="-"

    # FILTERED across every file. One file belongs to one boot, so the first
    # timestamped record classifies it; only post-boot files are read in full.
    filtered=0
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        fts="$(grep -m1 -o '"timestamp":"[^"]*"' "$f" 2>/dev/null | sed 's/.*:"//; s/"$//')"
        [ -n "$fts" ] || continue
        if [ "$fts" \> "$BOOT_CMP" ]; then
            n="$(grep -c '"type":"assistant"' "$f" 2>/dev/null)"
            [ -n "$n" ] || n=0
            filtered=$(( filtered + n ))
        fi
    done <<EOF
$files
EOF

    fresh_file=0
    [ "$first_ts" != "-" ] && [ "$first_ts" \> "$BOOT_CMP" ] && fresh_file=1

    # HOW the session came to life, which is the question presence-of-record
    # cannot answer. Typed before any instant is compared.
    #
    # The bot's own composed prompt is passed so the classifier can assert the
    # WHOLE injection landed rather than merely something startup-shaped. A bot
    # with no composed prompt cannot have that asserted, so it is disclosed
    # below rather than silently credited with a clean boot.
    startup_prompt="$(bot_conf_get "$botdir" STARTUP_PROMPT "")"
    [ -n "$startup_prompt" ] || printf '%s\n' "$bot" >> "$TMP/noprompt"
    start_line="$(boot_start_class "$tdir" "$BOOT_CMP" "$startup_prompt")"
    start_cls="$(printf '%s' "$start_line" | cut -f1)"
    start_ts="$(printf '%s' "$start_line" | cut -f2)"

    named_rescued=0
    grep -qx "$bot" "$TMP/rescued_names" 2>/dev/null && named_rescued=1

    # The rescue verdict, consulted only where a payload was actually submitted.
    #
    # THE NAME LIST IS NON-EXHAUSTIVE, and this is the load-bearing asymmetry:
    #   presence => rescued, definitively.
    #   absence  => UNKNOWN. Never a self-start verdict on its own.
    # A list is only as complete as the discipline of whoever writes it, and that
    # discipline failed on its first live test: a second rescue happened hours
    # after the receipt was designed, performed by the person who proposed it,
    # and went unrecorded. So absence falls through to the boundary — and where
    # there is no usable boundary to fall through TO, it is refused rather than
    # resolved. Reading "not on the list" as "started on its own" is how an
    # unrecorded rescue silently becomes a self-start.
    #
    # The stamp is the opposite kind of fact: a BOUNDARY, so everything after it
    # is suspect. list-says-rescued against boundary-says-clean is therefore a
    # CONTRADICTION and is refused by name; boundary-says-rescued against a name
    # merely missing from the list is not a contradiction at all, because the
    # list never claimed to be complete.
    rescue_v="NONE"
    if [ "$RESCUE_STATE" = "USABLE" ]; then
        if [ "$start_ts" \> "$RESCUE_STAMP_CMP" ]; then
            rescue_v="RESCUED"
        elif [ "$named_rescued" -eq 1 ]; then
            rescue_v="CONTRADICTION"
        fi
    elif [ "$RESCUE_STATE" = "NAMES-ONLY" ]; then
        if [ "$named_rescued" -eq 1 ]; then
            rescue_v="RESCUED-BY-NAME"
        else
            # A suppressed boundary AND a list that does not claim completeness
            # leaves nothing that could certify this bot. Refuse; print the
            # payload instant as evidence and let a human settle it.
            rescue_v="UNCERTIFIABLE"
        fi
    fi

    if [ "$start_cls" = "payload" ] && [ "$filtered" -gt 0 ]; then
        case "$rescue_v" in
            RESCUED)
                cls="RESCUED"
                why="payload submitted $start_ts, after the rescue boundary $RESCUE_STAMP — carried, not woken" ;;
            RESCUED-BY-NAME)
                cls="RESCUED"
                why="named on the rescue receipt; payload submitted $start_ts (boundary not comparable)" ;;
            CONTRADICTION)
                cls="ADJUDICATE"
                why="RECEIPT CONTRADICTS ITSELF: named as rescued, yet payload submitted $start_ts BEFORE its own boundary $RESCUE_STAMP" ;;
            UNCERTIFIABLE)
                cls="ADJUDICATE"
                why="payload submitted $start_ts, but the receipt boundary is unusable and its name list is not exhaustive — nothing here can certify a self-start" ;;
            *)
                # Nothing accounts for this payload. That is only good news if it
                # arrived when a self-start could still arrive — otherwise
                # "unaided" is being inferred from a MISSING record, which is the
                # inversion the bound exists to stop (#1203).
                if [ -n "$SELF_START_BOUND_CMP" ] && [ "$start_ts" \> "$SELF_START_BOUND_CMP" ]; then
                    cls="LATE-UNEXPLAINED"
                    why="payload submitted $start_ts, past the self-start bound ${SELF_START_BOUND_ISO}Z (ladder ${MAX_RUNG}s + first turn ${FIRST_TURN_ALLOWANCE_S}s) — something woke it and nothing recorded what"
                else
                    cls="SELF-STARTED"
                    why="payload submitted $start_ts, unaided"
                    # A self-start credited without the bound is credited on
                    # weaker evidence than one that cleared it. Named, not
                    # silently equated with the bounded kind.
                    #
                    # Keyed on the SAME predicate the comparison above uses, not
                    # on BOUND_STATE. One fact, one variable: if the two could
                    # disagree, a bot could be unbounded in fact and absent from
                    # the list that discloses it — silence in the one place the
                    # suppression is supposed to speak. BOUND_STATE/BOUND_WHY
                    # carry the operator-facing REASON and nothing else, so a
                    # drift between them can only ever produce a wrong sentence,
                    # never a wrong classification.
                    [ -n "$SELF_START_BOUND_CMP" ] || printf '%s\n' "$bot" >> "$TMP/unbounded"
                fi ;;
        esac
    elif [ "$start_cls" = "partial" ] && [ "$filtered" -gt 0 ]; then
        # Half a boot. Something startup-shaped submitted, but this bot's own
        # composed prompt did not — so it is running without the instructions it
        # was composed with, and it is NOT a self-start.
        #
        # A receipt verdict outranks this: "a human carried it" is the more
        # decision-relevant fact, and PARTIAL exists to stop a bot reading as a
        # SELF-START on half an injection, not to relabel one already known
        # carried.
        case "$rescue_v" in
            RESCUED|RESCUED-BY-NAME)
                cls="RESCUED"
                why="carried by a rescuer; its own composed prompt never submitted (first startup-shaped record $start_ts)" ;;
            *)
                cls="PARTIAL"
                why="boot injection only HALF submitted — startup-shaped record at $start_ts, but the composed STARTUP_PROMPT never landed" ;;
        esac
    elif [ "$start_cls" = "inbound" ] && [ "$filtered" -gt 0 ]; then
        # Alive, working, and never self-started. Counted OUT of the headline.
        cls="INBOUND-WOKEN"
        why="woken by an inbound channel message at $start_ts — no startup payload was submitted"
    elif [ "$not_due" -eq 1 ]; then
        # Dominates every negative verdict below, including the pre-crash-file
        # case: nothing negative can be concluded about a bot systemd has not
        # launched. Calling it a strand measures the elapsed clock, not
        # self-starting (#1050).
        cls="NOT-YET-DUE"; why="boot rung ${rung}s, only ${ELAPSED}s since boot — not launched yet"
    elif [ "$raw" -eq 0 ]; then
        cls="STRAND"; why="transcript exists but zero assistant records"
    elif [ "$CLOCK_VERDICT" = "STALE" ]; then
        # FILTERED is not trustworthy this boot, so the disagreement cannot be
        # resolved mechanically. Fail closed: flag it, do not pick a side.
        cls="ADJUDICATE"; why="RAW $raw vs FILTERED 0 under a stale boot clock — provisionally SELF-STARTED"
    elif [ "$CLOCK_VERDICT" = "UNKNOWN" ]; then
        cls="ADJUDICATE"; why="RAW $raw vs FILTERED 0 and the clock check could not run"
    elif [ "$fresh_file" -eq 1 ]; then
        # Should not happen: a file whose first record postdates boot ought to
        # contribute to FILTERED. Flag rather than guess.
        cls="ADJUDICATE"; why="RAW $raw vs FILTERED 0 but newest file postdates boot — inconsistent, inspect"
    else
        cls="STRAND"; why="newest transcript predates boot — pre-crash file, RAW false positive"
    fi

    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$bot" "$fleet" "$cls" "$raw" "$filtered" "$(basename "$newest")" "$first_ts" "$why" \
        "$start_cls:$start_ts" >> "$TMP/rows"
done < "$TMP/declared_rung"

# ── Undeclared leftovers (reported, never counted) ──────────────────────────
: > "$TMP/undeclared"
while IFS= read -r man; do
    [ -n "$man" ] || continue
    bdir="$(dirname "$man")/runtime/bots"
    [ -d "$bdir" ] || continue
    for d in "$bdir"/*/; do
        [ -d "$d" ] || continue
        b="$(basename "$d")"
        cut -f1 "$TMP/declared" | grep -qx "$b" || printf '%s\n' "$b" >> "$TMP/undeclared"
    done
done < "$TMP/manifests"

# ── Report — headline first ─────────────────────────────────────────────────
n_self="$(awk -F'\t' '$3=="SELF-STARTED"' "$TMP/rows" | wc -l | tr -d ' ')"
n_strand="$(awk -F'\t' '$3=="STRAND"' "$TMP/rows" | wc -l | tr -d ' ')"
n_adj="$(awk -F'\t' '$3=="ADJUDICATE"' "$TMP/rows" | wc -l | tr -d ' ')"
n_raw_pos="$(awk -F'\t' '$4>0' "$TMP/rows" | wc -l | tr -d ' ')"
n_filt_pos="$(awk -F'\t' '$5>0' "$TMP/rows" | wc -l | tr -d ' ')"
n_notdue="$(awk -F'\t' '$3=="NOT-YET-DUE"' "$TMP/rows" | wc -l | tr -d ' ')"
n_rescued="$(awk -F'\t' '$3=="RESCUED"' "$TMP/rows" | wc -l | tr -d ' ')"
n_inbound="$(awk -F'\t' '$3=="INBOUND-WOKEN"' "$TMP/rows" | wc -l | tr -d ' ')"
n_partial="$(awk -F'\t' '$3=="PARTIAL"' "$TMP/rows" | wc -l | tr -d ' ')"
n_late="$(awk -F'\t' '$3=="LATE-UNEXPLAINED"' "$TMP/rows" | wc -l | tr -d ' ')"
n_disagree="$(awk -F'\t' '($4>0)!=($5>0)' "$TMP/rows" | wc -l | tr -d ' ')"
n_rows="$(wc -l < "$TMP/rows" | tr -d ' ')"
# Summed ONCE. The completeness check and the banner that explains it used to
# carry the same addition written out twice, so adding a bucket and updating
# only one of them exits 4 on a page that is actually complete — the assertion
# built to catch a short sweep firing on itself instead.
n_classified=$(( n_self + n_strand + n_adj + n_notdue + n_rescued + n_inbound + n_partial + n_late ))

# A refusal that exits 0 is a caveat, not a refusal (#1051 review, vera). The
# banner below is advisory to a human reading the text and invisible to anything
# else, so without its own code a TOO EARLY page is indistinguishable to a
# programmatic consumer from a trustworthy 21-of-21. The whole argument for this
# gate was refuse-rather-than-caveat, and the exit code is where that either
# holds or quietly does not.
EXIT_CODE=0
[ "$TOO_EARLY" -eq 1 ] && EXIT_CODE=5

# ── Was this taken after someone started rescuing? ──────────────────────────
# The mirror of the too-early gate, and it refuses on the same terms. Any
# applicable receipt contaminates the page, even if no bot ended up classified
# RESCUED: a rescue sweep was under way while this ran, so what it produces is a
# reconstruction rather than the pre-registered measurement, which is defined as
# a reading taken BEFORE any rescue.
#
# Precedence 4 > 6 > 5: incomplete and contaminated are both permanent for this
# boot, while early resolves by re-running at the stated instant. Incomplete
# outranks contaminated because a short denominator invalidates the whole page
# including its contamination reading.
CONTAMINATED=0
RECEIPTS_UNKNOWN=0
if [ "$RESCUE_STATE" != "NONE" ]; then
    CONTAMINATED=1
    EXIT_CODE=6
fi
# The receipt SOURCE could not be read for at least one fleet: whether a rescue
# covers this boot is unknown, which is not a result either. Precedence 4 > 7 >
# 6 > 5: a receipt that WAS read still classifies its bots, but the page cannot
# claim the receipts were all seen, so it refuses as unknown rather than as
# covered (the instrument, not the boot, is what needs fixing).
if [ "$RECEIPTS_UNREACHABLE" -eq 1 ]; then
    RECEIPTS_UNKNOWN=1
    EXIT_CODE=7
fi

# ── Completeness assertion ──────────────────────────────────────────────────
# Every declared bot must have produced a row, and every row must have landed
# in exactly one bucket. Asserted positively, because without `set -e` there is
# no crash to infer completion from: a per-bot read that dies quietly would
# otherwise print a two-thirds page that EXITS 0, which is strictly worse than
# the aborted page dropping -e was meant to prevent. Removing the loud failure
# mode obliges the script to prove it finished (#1045 review, dara).
#
# PRECEDENCE: 4 overrides a TOO_EARLY 5 when both hold, and they are independent
# so both can. Early is a property of WHEN it ran and is fixed by re-running at
# the stated instant; incomplete is a property of the run itself and re-running
# need not fix it. The operator who gets only one number should get the one that
# does not resolve on its own.
INCOMPLETE=0
if [ "$n_rows" -ne "$TOTAL" ] || [ "$n_classified" -ne "$TOTAL" ]; then
    INCOMPLETE=1
    EXIT_CODE=4
    echo
    echo "############################################################"
    echo "##  INCOMPLETE SNAPSHOT — THE PAGE BELOW IS NOT A RESULT   ##"
    echo "############################################################"
    echo "##"
    echo "##  Declared bots      : $TOTAL"
    echo "##  Rows produced      : $n_rows"
    echo "##  Rows classified    : $n_classified"
    echo "##    self-started $n_self · stranded $n_strand · adjudicate $n_adj · not-yet-due $n_notdue"
    echo "##    rescued $n_rescued · inbound-woken $n_inbound · partial $n_partial · late-unexplained $n_late"
    echo "##"
    echo "##  Bots are missing from the counts below, so N is short and"
    echo "##  must NOT be compared against the baseline. Exit code 4."
    echo "############################################################"
    echo
fi

if [ "$TOO_EARLY" -eq 1 ]; then
    echo
    echo "############################################################"
    echo "##  TOO EARLY — THIS IS NOT A RESULT YET. DO NOT ACT ON IT ##"
    echo "############################################################"
    echo "##"
    echo "##  Only ${ELAPSED}s since boot."
    if [ "$n_notdue" -gt 0 ]; then
        echo "##  $n_notdue of $TOTAL bots have NOT BEEN LAUNCHED yet — systemd is"
        echo "##  still sleeping on their boot rung. They cannot have written"
        echo "##  anything, so they are NOT strands."
    fi
    echo "##  Boot ladder runs to ${MAX_RUNG}s; first turn adds ~${FIRST_TURN_ALLOWANCE_S}s"
    echo "##  (17-34s observed at load 31 on 2026-08-06, longer under worse"
    echo "##  load — override with SELFSTART_FIRST_TURN_ALLOWANCE_S)."
    echo "##"
    echo "##  A low count here measures the CLOCK, not self-starting, and"
    echo "##  against the baseline it reads as a catastrophe that has not"
    echo "##  happened. Re-run at $(epoch_to_iso_utc "$VALID_AT")Z, in $(( VALID_AT - NOW_EPOCH ))s."
    echo "##"
    echo "##  If something is visibly wedged and waiting is worse: rescue"
    echo "##  now and record the measurement as void. A lost measurement"
    echo "##  is recoverable at the next crash; a fleet left down is not."
    echo "############################################################"
    echo
fi

if [ "$RECEIPTS_UNKNOWN" -eq 1 ]; then
    echo
    echo "############################################################"
    echo "##  RESCUE UNKNOWN — THE RECEIPTS COULD NOT BE READ        ##"
    echo "############################################################"
    echo "##"
    echo "##  The plane holds the fleet_rescue receipts, and it could not"
    echo "##  answer for at least one declared fleet:"
    echo "##    $RECEIPTS_WHY"
    echo "##  So whether a rescue covers this boot is UNKNOWN, and a receipt"
    echo "##  gate that fails open is the one failure this measurement must"
    echo "##  never have. Restore the plane (state/plane/plane.db under"
    echo "##  $ROOT) and re-run; the boot is not spoiled by this refusal."
    echo "############################################################"
    echo
fi

if [ "$CONTAMINATED" -eq 1 ]; then
    echo
    echo "############################################################"
    echo "##  CONTAMINATED — TAKEN AFTER A RESCUE, NOT A RESULT      ##"
    echo "############################################################"
    echo "##"
    echo "##  A fleet_rescue receipt covers this boot, so this run is a"
    echo "##  reconstruction, not the #1002 measurement — which is defined"
    echo "##  as a reading taken BEFORE anyone rescues anything."
    echo "##"
    if [ "$RESCUE_STATE" = "USABLE" ]; then
        echo "##  Rescue boundary   : $RESCUE_STAMP"
    else
        echo "##  Rescue boundary   : UNUSABLE — $RESCUE_WHY"
        echo "##  Bots are therefore split by the receipt NAME LIST alone."
    fi
    echo "##  Named as rescued  : $N_RESCUE_NAMED bot(s)"
    echo "##  Classified rescued: $n_rescued  ·  inbound-woken: $n_inbound"
    echo "##"
    echo "##  $n_self of $TOTAL is what is LEFT after excluding those, and it is"
    echo "##  a lower bound on nothing: a bot rescued before it would have"
    echo "##  woken on its own is unrecoverable after the fact."
    echo "##"
    echo "##  Next time: run this BEFORE the first rescue. That is the whole"
    echo "##  reason it exists, and it costs one command."
    echo "############################################################"
    echo
fi

echo "==============================================================="
if [ "$RECEIPTS_UNKNOWN" -eq 1 ]; then
    echo "  SELF-START SNAPSHOT:  NOT A RESULT — whether a rescue covers this boot is UNKNOWN"
    echo "                        (provisional: $n_self of $TOTAL, $n_rescued carried, $n_inbound woken by inbound, $n_late late-unexplained)"
elif [ "$CONTAMINATED" -eq 1 ]; then
    echo "  SELF-START SNAPSHOT:  NOT A RESULT — a rescue covers this boot"
    echo "                        (provisional: $n_self of $TOTAL, $n_rescued carried, $n_inbound woken by inbound, $n_late late-unexplained)"
elif [ "$TOO_EARLY" -eq 1 ]; then
    echo "  SELF-START SNAPSHOT:  NOT A RESULT — only ${ELAPSED}s since boot"
    echo "                        (provisional: $n_self of $TOTAL, $n_notdue not launched yet)"
elif [ "$(( n_adj + n_late ))" -gt 0 ]; then
    # Both widen the range UPWARD and neither is counted in it. A late payload
    # might still be a very slow self-start, so folding it into the headline
    # would overclaim and dropping it from the range would underclaim.
    echo "  SELF-START SNAPSHOT:  $n_self of $TOTAL self-started"
    echo "                        range $n_self-$(( n_self + n_adj + n_late )) — $n_adj unresolved (ADJUDICATE), $n_late late (LATE-UNEXPLAINED)"
else
    echo "  SELF-START SNAPSHOT:  $n_self of $TOTAL self-started"
fi
echo "  prior figure:         $BASELINE_TEXT"
echo "                        $BASELINE_NOTE"
[ "$INCOMPLETE" -eq 1 ] && echo "  *** INCOMPLETE — $n_rows of $TOTAL bots produced a row, N IS SHORT ***"
[ "$PARTIAL" -eq 1 ] && echo "  *** PARTIAL DENOMINATOR — a manifest was skipped, N IS SHORT ***"
[ "$DUP_TOLERATED" -eq 1 ] && echo "  *** DUPLICATE BOT NAMES ACCEPTED — rows below are ambiguous: $(printf '%s' "$DUPES" | tr '\n' ' ') ***"
echo "==============================================================="
echo
echo "  RAW      (assistant records in newest transcript) : $n_raw_pos of $TOTAL"
echo "  FILTERED (assistant records after boot instant)   : $n_filt_pos of $TOTAL"
if [ "$n_disagree" -gt 0 ]; then
    echo "  counts DISAGREE on $n_disagree bot(s) — listed below"
else
    echo "  counts agree on all $TOTAL bots"
fi
echo "  clock at boot : $CLOCK_VERDICT — $CLOCK_DETAIL"
case "$RESCUE_STATE" in
    NONE)
        if [ "$RECEIPTS_UNREACHABLE" -eq 1 ]; then
            echo "  rescue receipt: UNREADABLE — $RECEIPTS_WHY"
        else
            echo "  rescue receipt: none covers this boot — contamination CANNOT be ruled out"
        fi ;;
    USABLE)
        echo "  rescue receipt: boundary $RESCUE_STAMP, $N_RESCUE_NAMED bot(s) named (fleet(s): $(tr '\n' ' ' < "$TMP/receipt_fleets"))" ;;
    *)
        echo "  rescue receipt: boundary UNUSABLE — $RESCUE_WHY"
        echo "                  $N_RESCUE_NAMED bot(s) named; the name list still applies" ;;
esac
if [ "$BOUND_STATE" = "APPLIED" ]; then
    echo "  self-start bound: ${SELF_START_BOUND_ISO}Z (ladder ${MAX_RUNG}s + first turn ${FIRST_TURN_ALLOWANCE_S}s)"
    echo "                  a payload after this that no receipt explains is LATE-UNEXPLAINED, not a self-start"
else
    echo "  self-start bound: NOT APPLIED — $BOUND_WHY"
    echo "                  so SELF-STARTED below is unbounded above: an unrecorded"
    echo "                  wake at any distance from boot still reads as a self-start"
fi
echo "  boot instant  : ${BOOT_ISO}Z (epoch $BOOT_EPOCH)"
echo "  snapshot at   : ${NOW_ISO}Z (${ELAPSED}s after boot)"
if [ "$RECEIPTS_UNKNOWN" -eq 1 ]; then
    echo "  result valid  : NO — the rescue receipts could not be read, see the RESCUE UNKNOWN banner"
elif [ "$CONTAMINATED" -eq 1 ]; then
    echo "  result valid  : NO — a rescue covers this boot, see the CONTAMINATED banner"
elif [ "$TOO_EARLY" -eq 1 ]; then
    echo "  result valid  : NOT YET — re-run at $(epoch_to_iso_utc "$VALID_AT")Z (ladder ${MAX_RUNG}s + first turn ${FIRST_TURN_ALLOWANCE_S}s)"
else
    echo "  result valid  : yes — past ladder ${MAX_RUNG}s + first turn ${FIRST_TURN_ALLOWANCE_S}s"
fi
echo "  denominator   : $TOTAL declared bots across $(wc -l < "$TMP/manifests" | tr -d ' ') fleet manifest(s)"
echo

print_section() {
    local want="$1" title="$2" n
    n="$(awk -F'\t' -v w="$want" '$3==w' "$TMP/rows" | wc -l | tr -d ' ')"
    echo "$title ($n)"
    if [ "$n" -eq 0 ]; then
        echo "  none"
    else
        awk -F'\t' -v w="$want" '$3==w {
            printf "  %-12s %-16s raw=%-5s filtered=%-5s  %s\n", $1, "["$2"]", $4, $5, $8
        }' "$TMP/rows"
    fi
    echo
}

print_section "SELF-STARTED"  "SELF-STARTED"
print_section "LATE-UNEXPLAINED" "LATE, UNEXPLAINED — a startup payload landed, but so long after boot that it cannot be a self-start, and no rescue receipt accounts for it. This is a GAP, not a pass: something woke these bots and nothing recorded what. They are counted OUT of the headline and into its upper range"
print_section "PARTIAL"       "HALF-BOOTED — a startup-shaped record landed but the bot's OWN composed prompt never did. Running without the instructions it was composed with, and NOT a self-start"
print_section "RESCUED"       "RESCUED — carried by a rescuer, NOT self-started"
print_section "INBOUND-WOKEN" "STRANDED ON BOOT, WOKEN BY AN INBOUND MESSAGE — these are NOT self-starts. A bot here looks healthy to every liveness signal: session up, pane active, work being done. It never started on its own"
print_section "STRAND"        "STRANDED"
print_section "NOT-YET-DUE"   "NOT YET DUE — systemd has not launched these, they are NOT strands"
print_section "ADJUDICATE"    "ADJUDICATE — counts disagree and cannot be resolved mechanically"

if [ "$RESCUE_STATE" = "NONE" ]; then
    echo "NOTE: no rescue receipt covers this boot. That is NOT evidence that"
    echo "      nobody rescued anything — with no external boundary to compare"
    echo "      against, a carried bot and a woken one are the same shape here."
    echo "      Only the per-bot classes above are decidable; contamination is"
    echo "      not. Run this BEFORE any rescue, and have whoever rescues write"
    echo "      a fleet_rescue receipt, or this limit is permanent for the boot."
    echo
fi

if [ -s "$TMP/unbounded" ]; then
    echo "NOTE: the lateness bound could not be applied, so these bots are counted"
    echo "      as self-starters on the weaker evidence that nothing accounts for"
    echo "      them. That is exactly the reading an unrecorded rescue produces,"
    echo "      and here nothing separates the two. Reason: $BOUND_WHY."
    sort -u "$TMP/unbounded" | sed 's/^/        /'
    echo
fi

if [ -s "$TMP/noprompt" ]; then
    echo "NOTE: no composed STARTUP_PROMPT readable for these bots, so the"
    echo "      whole-injection assertion could not be applied. A half-submitted"
    echo "      boot would read as a clean one here — they are disclosed rather"
    echo "      than credited:"
    sort -u "$TMP/noprompt" | sed 's/^/        /'
    echo
fi

if [ -s "$TMP/norung" ]; then
    echo "NOTE: no boot rung readable for these bots, so the not-yet-launched"
    echo "      gate could not be applied to them. They may be misreported as"
    echo "      strands if this ran early (no composed .service unit — a"
    echo "      launchd host staggers elsewhere)."
    # Keyed on the bound's OWN state, never on this list. When NO bot has a rung
    # every bot is in this list AND the bound is suppressed, so a flat "the bound
    # still applies" would be false in exactly the case where this note is
    # longest — a disclosure outliving its condition and becoming its own untruth.
    if [ "$BOUND_STATE" = "APPLIED" ]; then
        echo "      The lateness bound still applies to them, from the ladder end"
        echo "      read off the bots that DO have one — the loosest treatment"
        echo "      available, never a tighter one."
    else
        echo "      The lateness bound is not applied to them either — see the"
        echo "      self-start bound line above for why."
    fi
    sort -u "$TMP/norung" | sed 's/^/        /'
    echo
fi

if [ "$n_disagree" -gt 0 ]; then
    echo "DISAGREEMENT DETAIL (evidence for adjudication)"
    awk -F'\t' '($4>0)!=($5>0) {
        printf "  %-12s raw=%-5s filtered=%-5s  newest=%s  first_record=%s\n", $1, $4, $5, $6, $7
    }' "$TMP/rows"
    echo
fi

if [ -s "$TMP/undeclared" ]; then
    echo "NOTE: undeclared bot directories found on disk, EXCLUDED from the"
    echo "      denominator so they cannot inflate the total or read as"
    echo "      permanent strands:"
    sort -u "$TMP/undeclared" | sed 's/^/        /'
    echo
fi

if [ "$PARTIAL" -eq 1 ]; then
    echo "PARTIAL DENOMINATOR — these manifests were skipped and their bots are"
    echo "      NOT in the total above. N is short by a whole fleet:"
    sed 's/^/        /' "$TMP/badman"
    echo
fi

if [ "$DUP_TOLERATED" -eq 1 ]; then
    echo "DUPLICATE BOT NAMES ACCEPTED via SELFSTART_ALLOW_DUPLICATE_NAMES — these"
    echo "      names are declared in more than one fleet.yaml, so the rows above"
    echo "      that carry them identify two different bots each. Read them with"
    echo "      the [fleet] column, and do not key anything on the name alone:"
    printf '%s\n' "$DUPES" | sed 's/^/        /'
    echo
fi

exit "$EXIT_CODE"
