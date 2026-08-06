#!/usr/bin/env bash
# lib/selfstart-snapshot.sh — record how many bots SELF-STARTED after a host boot.
#
# The #1002 pre-registration asks one question at the next unplanned reboot:
# how many bots came up without help? Baseline to beat is 6 of 21 (2026-08-06,
# load 31, pre-fix ladder). Run this BEFORE rescuing anything, then rescue
# immediately — the snapshot is what removes the trade between a clean
# experiment and leaving bots stranded (issue #1043).
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
# ── Why this script is deliberately self-contained ──────────────────────────
# It does not source lib-common.sh, call the claudlobby CLI, touch the network,
# or require any bot to be up. Pure bash + coreutils + awk, with journalctl used
# only for the clock assertion and degrading to UNKNOWN when it is absent. The
# one time this script is ever run is the one time the host is least healthy —
# a half-booted machine at load 31 — so every dependency it does not have is a
# way it cannot fail. Do not "fix" this by wiring it into the shared helpers.
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
set -uo pipefail

BASELINE_TEXT="6 of 21 (2026-08-06, load 31, pre-fix ladder)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${CLAUDLOBBY_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
CFG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/selfstart.XXXXXX")" || {
    echo "selfstart-snapshot: cannot create temp dir" >&2; exit 3; }
trap 'rm -rf "$TMP"' EXIT

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
EOF
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    "") ;;
    *) echo "selfstart-snapshot: unknown argument: $1" >&2; usage >&2; exit 3 ;;
esac

# ── Loud failure ────────────────────────────────────────────────────────────
# The denominator has no degraded mode. See the note above list_manifests.
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
    echo "##  SELFSTART_ALLOW_PARTIAL=1 to accept a declared-partial"
    echo "##  denominator."
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
NOW_ISO="$(epoch_to_iso_utc "$(date +%s)")"

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
# Same code shape, opposite correct answer; the discriminator is action versus
# measurement. Hence: parse failure is loud and fatal here.
list_manifests() {
    # Both supported overlay depths: local/<fleet>/ and local/<system>/<fleet>/.
    ls -1 "$ROOT"/local/*/fleet.yaml "$ROOT"/local/*/*/fleet.yaml 2>/dev/null | sort -u
}

# Bot keys are the first nesting level under the `bots:` mapping. Comments,
# blank lines, deeper keys and sibling top-level keys are all excluded, and an
# anchor on the key (`alex: &base`) is still a bot.
bots_from_manifest() {
    awk '
        /^[[:space:]]*#/ { next }
        !inbots && $0 ~ /^[[:space:]]*bots:[[:space:]]*(#.*)?$/ {
            match($0, /^[ ]*/); ind = RLENGTH; inbots = 1; botind = 0; next
        }
        inbots {
            if ($0 ~ /^[[:space:]]*$/) next
            match($0, /^[ ]*/); cur = RLENGTH
            if (cur <= ind) { inbots = 0; next }
            if (botind == 0) botind = cur
            if (cur == botind && $0 ~ /^[ ]*[A-Za-z0-9_-]+:[ ]*(&[A-Za-z0-9_-]+)?[ ]*(#.*)?$/) {
                k = $0; sub(/^[ ]*/, "", k); sub(/:.*$/, "", k); print k
            }
        }
    ' "$1" 2>/dev/null
}

MANIFESTS="$(list_manifests)"
[ -n "$MANIFESTS" ] && printf '%s\n' "$MANIFESTS" > "$TMP/manifests"
if [ ! -s "$TMP/manifests" ]; then
    die_loud "No fleet.yaml found under $ROOT/local/" \
             "Looked at local/*/fleet.yaml and local/*/*/fleet.yaml"
fi

: > "$TMP/declared"
: > "$TMP/badman"
while IFS= read -r man; do
    [ -n "$man" ] || continue
    fleet="$(basename "$(dirname "$man")")"
    if [ ! -r "$man" ]; then
        printf '%s\tunreadable\n' "$man" >> "$TMP/badman"; continue
    fi
    if ! grep -qE '^[[:space:]]*bots:[[:space:]]*(#.*)?$' "$man" 2>/dev/null; then
        printf '%s\tno bots: block\n' "$man" >> "$TMP/badman"; continue
    fi
    names="$(bots_from_manifest "$man")"
    if [ -z "$names" ]; then
        printf '%s\tbots: block declares no bots\n' "$man" >> "$TMP/badman"; continue
    fi
    while IFS= read -r b; do
        [ -n "$b" ] || continue
        printf '%s\t%s\t%s\n' "$b" "$fleet" "$(dirname "$man")/runtime/bots/$b" >> "$TMP/declared"
    done <<EOF
$names
EOF
done < "$TMP/manifests"

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

# A duplicate name across fleets breaks the union: two distinct bots would
# collapse into one row and N would be short. Fatal, not a warning.
DUPES="$(cut -f1 "$TMP/declared" | sort | uniq -d)"
if [ -n "$DUPES" ] && [ "${SELFSTART_ALLOW_PARTIAL:-0}" != "1" ]; then
    die_loud "Bot name declared in more than one fleet.yaml — the union would" \
             "collapse two distinct bots into one row:" \
             "$(printf '%s' "$DUPES" | tr '\n' ' ')"
fi

TOTAL="$(wc -l < "$TMP/declared" | tr -d ' ')"

# ── Per-bot snapshot ────────────────────────────────────────────────────────
# Transcript dir is the bot working directory with / replaced by -, under the
# Claude Code projects root.
transcript_dir_for() { printf '%s/projects/%s\n' "$CFG_DIR" "$(printf '%s' "$1" | tr '/' '-')"; }

: > "$TMP/rows"
while IFS="$(printf '\t')" read -r bot fleet botdir; do
    [ -n "$bot" ] || continue
    tdir="$(transcript_dir_for "$botdir")"
    files="$(ls -1t "$tdir"/*.jsonl 2>/dev/null)"

    if [ -z "$files" ]; then
        # Declared, but no transcript at all — the third blind spot. A naive
        # transcript walk drops this bot from both lists entirely.
        printf '%s\t%s\tSTRAND\t0\t0\t-\t-\tno transcript file at all\n' "$bot" "$fleet" >> "$TMP/rows"
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

    if [ "$filtered" -gt 0 ]; then
        cls="SELF-STARTED"; why="post-boot assistant records present"
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

    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$bot" "$fleet" "$cls" "$raw" "$filtered" "$(basename "$newest")" "$first_ts" "$why" >> "$TMP/rows"
done < "$TMP/declared"

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
n_disagree="$(awk -F'\t' '($4>0)!=($5>0)' "$TMP/rows" | wc -l | tr -d ' ')"

echo "==============================================================="
if [ "$n_adj" -gt 0 ]; then
    echo "  SELF-START SNAPSHOT:  $n_self of $TOTAL self-started"
    echo "                        range $n_self-$(( n_self + n_adj )) — $n_adj unresolved, see ADJUDICATE"
else
    echo "  SELF-START SNAPSHOT:  $n_self of $TOTAL self-started"
fi
echo "  baseline to beat:     $BASELINE_TEXT"
[ "$PARTIAL" -eq 1 ] && echo "  *** PARTIAL DENOMINATOR — a manifest was skipped, N IS SHORT ***"
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
echo "  boot instant  : ${BOOT_ISO}Z (epoch $BOOT_EPOCH)"
echo "  snapshot at   : ${NOW_ISO}Z"
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

print_section "SELF-STARTED" "SELF-STARTED"
print_section "STRAND"       "STRANDED"
print_section "ADJUDICATE"   "ADJUDICATE — counts disagree and cannot be resolved mechanically"

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

exit 0
