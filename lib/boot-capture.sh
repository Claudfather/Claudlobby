#!/bin/bash
# boot-capture.sh — record what happened to every DECLARED bot at this host boot,
# without needing a bot to be awake to do it (#1265, #1236).
#
# THE CIRCULARITY THIS EXISTS TO BREAK. Every boot measurement the estate has
# was taken because a bot happened to self-start AND happened to look. The thing
# being measured is whether bots wake up; the instrument was a bot that woke up.
# So the misses correlate with severity: the boots we most need are exactly the
# ones nobody is awake to record. That makes n=1 structural rather than
# incidental — waiting does not fix it, and no boot before this lands can be
# backfilled.
#
# ── Why this polls instead of taking one snapshot ───────────────────────────
# The obvious design is a single pass at ladder-end plus a margin. It cannot
# work, and the reason is arithmetic rather than taste. FOUR facts are wanted
# per bot and they have THREE different expiries:
#
#   journal "Started <unit>"  survives (39 boots back on this host)
#   data/.spawn mtime         REWRITTEN by every start-bot.sh run
#   session_created           dies with the session
#   the outcome label         derived from transcripts, gone at 30 days
#
# .spawn is the nasty one: after a restart it is not missing, it is a plausible
# timestamp describing a DIFFERENT event, so a later reader gets a confident
# wrong answer rather than an absent one.
#
# Now the clock. keepalive is OnBootSec=60/OnUnitActiveSec=60 and every restart
# branch re-runs start-bot.sh, so every keepalive restart overwrites .spawn. On
# the 2026-09-07 boot (18:24:17, ladder end 18:25:43, last session 18:28:00) a
# pass at ladder-end+60s fires 18:26:43 and misses NINE of 21 bots whose
# sessions did not exist yet. Recording those as absent is the #1050
# premature-verdict defect rebuilt inside the instrument meant to fix it. A pass
# late enough to catch them is three keepalive ticks in, and on a boot where
# sessions genuinely die that is long after .spawn was overwritten.
#
# THERE IS NO INSTANT AT WHICH ALL FOUR FACTS ARE VALID FOR ALL BOTS. A better
# margin does not exist to be found. So each bot is captured at its FIRST
# OBSERVATION — the first tick on which its session exists — and a bot keepalive
# later restarts is already recorded before the overwrite.
#
# That 2026-09-07 boot only survived because the stranded bots held live
# sessions, so keepalive saw them alive and left them alone. That was luck: on a
# boot where sessions genuinely fail, keepalive walks them back up within ~60s
# and destroys both perishable fields — and the worse the boot, the faster the
# evidence goes.
#
# ── What is in the hot loop, and what is deliberately not ───────────────────
# Only the PERISHABLE facts are polled: session_created and .spawn (with the
# instant the .spawn read happened, so a later reader can tell a boot-spawn from
# a restart-spawn — without that stamp they are indistinguishable). The journal
# survives, so it is read ONCE at close rather than 21 times per tick: this runs
# inside the boot storm it is measuring, and an instrument that adds 21
# journalctl spawns every 30s to a loaded Pi is perturbing its own subject.
#
# ── Where the record goes ───────────────────────────────────────────────────
# The plane, through emit_fleet_event — the shipped door. Since the F18 closure
# the plane is the ONLY recorder, and a bespoke ledger here would fork the
# surface that closure removed. Durability is checked, not assumed: retention
# DELETEs metric_samples and nothing else (claudlobby/plane/retention.py), so a
# system event is never pruned. data/events/*.jsonl is dead on this branch and
# would have been the wrong answer anyway.
#
# The local state directory under runtime/_host/boot-capture/ is per-boot
# progress, NOT a second record: which bots are already captured, and the facts
# held between first observation and close. It survives spin-down --purge and
# data-sweep (which walks bot data/ dirs only), so a boot that closes after a
# host restart still emits.
#
# Exit codes:
#   0  nothing to do, or the boot advanced normally
#   2  the roster is empty — the denominator cannot be trusted, so nothing is
#      captured and the boot is NOT closed; a later tick with a fixed manifest
#      still gets it. Empty here licenses a LOSS, not a no-op (#1146).
#   3  the boot instant could not be resolved

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

# lib-common.sh arms `set -euo pipefail` AT SOURCE TIME, so sourcing it re-arms
# errexit AND nounset inside a script whose contract is not having them.
# Disarmed deliberately, for the reason selfstart-snapshot.sh disarms errexit: a
# mid-sweep failure must not abort a partial capture, because a boot that dies
# two thirds of the way through the roster is a boot lost forever. Both are
# needed — `set +e` alone leaves an unset probe result aborting the sweep under
# -u. Every probe below is individually guarded and answers "-" rather than
# failing.
set +e
set +u

usage() {
    cat <<'EOF'
Usage: boot-capture.sh [--status] [--help]

Polls from boot and records each DECLARED bot at its first observation.
Driven by the claudlobby-boot-capture timer; safe to run by hand.

  --status   print this boot capture progress and exit (no capture, no emit)

Env:
  BOOT_CAPTURE_BOUND_S   seconds past ladder end before the boot is closed
                         with whatever was seen (default 900)
  CLAUDLOBBY_BOOT_EPOCH  override the boot instant (test seam)
EOF
}

MODE=capture
case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    --status)  MODE=status ;;
    "") ;;
    *) echo "boot-capture: unknown argument: $1" >&2; usage >&2; exit 3 ;;
esac

: "${CLAUDLOBBY_ROOT:?boot-capture: CLAUDLOBBY_ROOT is required}"

# This sweep is HOST-scoped and crosses every fleet on the box, so it must carry
# no fleet identity of its own. emit_fleet_event prefers an ambient FLEET_NAME
# over the bot conf it is handed, which is right for a fleet-scoped door and
# wrong here: run by hand from inside a bot session -- the obvious way to try
# it -- every row on every fleet gets stamped with THAT bot fleet. Measured: a
# testfleet bot landed in the plane anchored bot:ai-platform/alpha. Unset, so
# each row resolves its fleet from the bot own conf and the host-level summary
# anchors on the host.
unset FLEET_NAME CLAUDLOBBY_FLEET
BOUND_S="${BOOT_CAPTURE_BOUND_S:-900}"
case "$BOUND_S" in ""|*[!0-9]*) BOUND_S=900 ;; esac

NOW="$(date +%s)"
BOOT_EPOCH="$(resolve_boot_epoch)"
if [ -z "$BOOT_EPOCH" ]; then
    echo "boot-capture: cannot resolve the boot instant — nothing recorded" >&2
    exit 3
fi

BASE="$CLAUDLOBBY_ROOT/runtime/_host/boot-capture"
DIR="$BASE/$BOOT_EPOCH"
OBS="$DIR/observed.tsv"
mkdir -p "$DIR" 2>/dev/null

# ── Roster: the LOUD door, because empty licenses a loss ────────────────────
# parse_fleet_bots soft-fails to nothing and bot_in_fleet inverts that into
# "every directory is declared". Here an empty roster would mean capturing zero
# bots and calling the boot done — absence of evidence recorded as evidence of
# absence, on a measurement that cannot be retaken.
declared_bots_strict "$DIR/bad-manifests" > "$DIR/declared" 2>/dev/null
DECLARED_N="$(wc -l < "$DIR/declared" 2>/dev/null | tr -d ' ')"
[ -n "$DECLARED_N" ] || DECLARED_N=0
if [ "$DECLARED_N" -eq 0 ]; then
    echo "boot-capture: no bots declared on this host — refusing to close boot $BOOT_EPOCH" >&2
    exit 2
fi

# ── Ladder end, derived from the composed rungs ─────────────────────────────
# Never hardcoded: the 3s stagger is a composer constant that will move, and a
# copy here would decay silently. boot_rung_for returns -1 for a rung it cannot
# read (a launchd plist, a missing unit), which is not the same as 0.
MAX_RUNG=-1
while IFS="$(printf '\t')" read -r _b _f _d; do
    [ -n "$_d" ] || continue
    _r="$(boot_rung_for "$_d")"
    [ "$_r" -gt "$MAX_RUNG" ] 2>/dev/null && MAX_RUNG="$_r"
done < "$DIR/declared"
if [ "$MAX_RUNG" -lt 0 ]; then
    LADDER_DERIVED=false
    LADDER_END=0
else
    LADDER_DERIVED=true
    LADDER_END="$MAX_RUNG"
fi
BOUND_AT=$(( BOOT_EPOCH + LADDER_END + BOUND_S ))

# ── Probes ──────────────────────────────────────────────────────────────────
# All three are individually guarded and print "-" rather than failing: a probe
# that cannot answer must not take the whole sweep down with it.
session_created_for() {
    local d="$1" sock sess out
    sock="$(tmux_socket_for_bot "$d" 2>/dev/null)" || { printf '%s\n' -; return 0; }
    sess="$(tmux_session_name "$d" 2>/dev/null)" || { printf '%s\n' -; return 0; }
    [ -n "$sess" ] || { printf '%s\n' -; return 0; }
    out="$(bot_tmux "$sock" display-message -p -t "$sess" '#{session_created}' 2>/dev/null)"
    case "$out" in ''|*[!0-9]*) printf '%s\n' - ;; *) printf '%s\n' "$out" ;; esac
}

# stat_mtime (lib-common) owns the portable stat ladder; this only adds the
# "-" sentinel, so an unreadable mtime cannot enter a row as a number.
file_mtime_epoch() {
    local v
    [ -f "$1" ] || { printf '%s\n' -; return 0; }
    v="$(stat_mtime "$1" 2>/dev/null)"
    case "$v" in ''|*[!0-9]*) printf '%s\n' - ;; *) printf '%s\n' "$v" ;; esac
}

# The injection stamp start-bot.sh writes at the actual send (#1265 blocker 2).
# Perishable for the same reason .spawn is — overwritten on every start — so it
# is read here, at first observation, not at close.
#
# Read ONCE per bot into INJ_*, never once per field: this is the hot loop the
# design keeps deliberately light, and a fork per field per bot per tick is the
# load this file claims not to add.
read_inject() {
    INJ_STATE=- INJ_KIND=- INJ_EPOCH=- INJ_BOOT=- INJ_DUR=-
    [ -f "$1" ] || return 0
    local line kv k v
    IFS= read -r line < "$1" 2>/dev/null || return 0
    for kv in $line; do
        k="${kv%%=*}"; v="${kv#*=}"
        [ -n "$v" ] || v=-
        case "$k" in
            state) INJ_STATE="$v" ;;
            kind)  INJ_KIND="$v" ;;
            epoch) INJ_EPOCH="$v" ;;
            boot)  INJ_BOOT="$v" ;;
            dur)   INJ_DUR="$v" ;;
        esac
    done
}

# ── Status mode ─────────────────────────────────────────────────────────────
observed_names() { cut -f1 "$OBS" 2>/dev/null | sort -u; }
if [ "$MODE" = status ]; then
    _seen="$(observed_names | wc -l | tr -d ' ')"
    echo "boot        : $BOOT_EPOCH ($(date -d "@$BOOT_EPOCH" 2>/dev/null || date -r "$BOOT_EPOCH" 2>/dev/null))"
    echo "declared    : $DECLARED_N"
    echo "observed    : $_seen"
    echo "ladder end  : ${LADDER_END}s (derived: $LADDER_DERIVED)"
    echo "bound at    : $BOUND_AT ($(( BOUND_AT - NOW ))s from now)"
    echo "closed      : $([ -f "$DIR/closed" ] && cat "$DIR/closed" || echo no)"
    echo "emitted     : $([ -f "$DIR/emitted" ] && echo yes || echo no)"
    if [ "$_seen" -lt "$DECLARED_N" ]; then
        echo "not yet seen:"
        comm -23 <(cut -f1 "$DIR/declared" | sort -u) <(observed_names) | sed 's/^/  /'
    fi
    exit 0
fi

# ── Sweep: capture every declared bot not already recorded ──────────────────
while IFS="$(printf '\t')" read -r bot fleet botdir; do
    [ -n "$bot" ] || continue
    cut -f1 "$OBS" 2>/dev/null | grep -qx "$bot" && continue

    sc="$(session_created_for "$botdir")"
    [ "$sc" = "-" ] && continue          # no session yet — try again next tick

    # FIRST OBSERVATION. Everything perishable is read now, in one place, and
    # the .spawn read instant is recorded beside the mtime: a later reader
    # cannot otherwise tell a boot-spawn from a restart-spawn.
    spawn="$(file_mtime_epoch "$botdir/data/.spawn")"
    read_at="$(date +%s)"
    read_inject "$botdir/data/.inject"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$bot" "$fleet" "$sc" "$spawn" "$read_at" \
        "$(boot_rung_for "$botdir")" \
        "$INJ_STATE" "$INJ_KIND" "$INJ_EPOCH" "$INJ_BOOT" "$INJ_DUR" >> "$OBS"
done < "$DIR/declared"

SEEN_N="$(observed_names | wc -l | tr -d ' ')"
[ -n "$SEEN_N" ] || SEEN_N=0

# Already closed and emitted: the cheap path every tick takes for the rest of
# the uptime. Two file tests and out.
if [ -f "$DIR/closed" ] && [ -f "$DIR/emitted" ]; then exit 0; fi

CLOSE_REASON=""
if [ "$SEEN_N" -ge "$DECLARED_N" ]; then
    CLOSE_REASON=complete
elif [ "$NOW" -ge "$BOUND_AT" ]; then
    CLOSE_REASON=bound
fi

if [ -z "$CLOSE_REASON" ]; then
    # Still collecting. Nothing is emitted mid-boot on purpose: the derived
    # label is a property of the finished picture, and a half-boot emission
    # would be a verdict taken early — the defect this design exists to avoid.
    exit 0
fi

# ── Close ───────────────────────────────────────────────────────────────────
UNSEEN="$(comm -23 <(cut -f1 "$DIR/declared" | sort -u) <(observed_names) | tr '\n' ' ' | sed 's/ *$//')"
# Every fact the emission needs is written into the boot directory, never left
# in a global. emit_boot also runs for PRIOR boots the host went down before
# emitting, and a global would describe the CURRENT boot -- the ladder facts of
# one boot silently attached to another.
printf 'reason=%s at=%s declared=%s observed=%s ladder_end=%s ladder_derived=%s bound_s=%s unaccounted=%s\n' \
    "$CLOSE_REASON" "$NOW" "$DECLARED_N" "$SEEN_N" \
    "$LADDER_END" "$LADDER_DERIVED" "$BOUND_S" "${UNSEEN:--}" > "$DIR/closed"

# The journal is read HERE, once, because "Started <unit>" is the one fact that
# survives. 21 journalctl calls at close cost nothing; 21 per tick during the
# boot storm would be the instrument loading its own subject.
: > "$DIR/journal"
while IFS="$(printf '\t')" read -r bot fleet botdir; do
    [ -n "$bot" ] || continue
    unit=""
    for u in "$botdir"/*.service; do [ -f "$u" ] && unit="$(basename "$u")" && break; done
    started=-
    if [ -n "$unit" ] && command -v journalctl >/dev/null 2>&1; then
        started="$(journalctl --user -b -u "$unit" -o short-unix --no-pager 2>/dev/null \
                   | grep -F 'Started' | head -1 | awk '{print int($1)}')"
        [ -n "$started" ] || started=-
    fi
    printf '%s\t%s\n' "$bot" "$started" >> "$DIR/journal"
done < "$DIR/declared"

# The derived classification, taken from the shipped classifier rather than
# re-derived here. It reads transcripts, which Claude Code deletes at 30 days
# (cleanupPeriodDays default, unset on this host), so the LABEL is the one fact
# that must be captured now or never. The rows come out of the classifier as
# data; parsing its printed page would be the private-copy defect one layer up.
SELFSTART_ROWS_OUT="$DIR/rows" "$LIB_DIR/selfstart-snapshot.sh" > "$DIR/snapshot.txt" 2>&1
SNAP_RC=$?
[ -f "$DIR/rows" ] || : > "$DIR/rows"

# ── Emission ────────────────────────────────────────────────────────────────
# One system event per bot, anchored on that bot, plus one host-anchored
# summary. Through emit_fleet_event, the shipped door: since the F18 closure
# the plane is the only recorder, and a private ledger here would fork exactly
# the surface that closure removed.
jnum() { case "$1" in ''|-|*[!0-9]*) printf 'null' ;; *) printf '%s' "$1" ;; esac; }
# One pass over the close line; the six fields were six tr|sed|head pipelines
# over the same 100 bytes.
read_closed() {
    CLOSE_REASON_R=- CLOSE_DECLARED=- CLOSE_OBSERVED=- CLOSE_LADDER=- \
        CLOSE_DERIVED=false CLOSE_BOUND=-
    local line kv k v
    IFS= read -r line < "$1/closed" 2>/dev/null || return 0
    for kv in $line; do
        k="${kv%%=*}"; v="${kv#*=}"
        case "$k" in
            reason)         CLOSE_REASON_R="$v" ;;
            declared)       CLOSE_DECLARED="$v" ;;
            observed)       CLOSE_OBSERVED="$v" ;;
            ladder_end)     CLOSE_LADDER="$v" ;;
            ladder_derived) CLOSE_DERIVED="$v" ;;
            bound_s)        CLOSE_BOUND="$v" ;;
        esac
    done
}

emit_boot() {
    local d="$1" boot_epoch reason declared observed unaccounted snap_rc=0
    local ladder_end ladder_derived bound_s
    [ -f "$d/closed" ] || return 0
    boot_epoch="$(basename "$d")"
    read_closed "$d"
    reason="$CLOSE_REASON_R"; declared="$CLOSE_DECLARED"; observed="$CLOSE_OBSERVED"
    ladder_end="$CLOSE_LADDER"; ladder_derived="$CLOSE_DERIVED"; bound_s="$CLOSE_BOUND"
    case "$ladder_derived" in true|false) ;; *) ladder_derived=false ;; esac
    unaccounted="$(sed -n 's/.*unaccounted=\(.*\)$/\1/p' "$d/closed")"
    [ -f "$d/snapshot.rc" ] && snap_rc="$(cat "$d/snapshot.rc")"

    local bot fleet sc spawn read_at rung i_state i_kind i_epoch i_boot i_dur
    while IFS="$(printf '\t')" read -r bot fleet sc spawn read_at rung i_state i_kind i_epoch i_boot i_dur; do
        [ -n "$bot" ] || continue
        local botdir cls why started payload
        botdir="$(awk -F'\t' -v b="$bot" '$1==b {print $3; exit}' "$d/declared")"
        cls="$(awk -F'\t' -v b="$bot" '$1==b {print $3; exit}' "$d/rows" 2>/dev/null)"
        why="$(awk -F'\t' -v b="$bot" '$1==b {print $8; exit}' "$d/rows" 2>/dev/null)"
        started="$(awk -F'\t' -v b="$bot" '$1==b {print $2; exit}' "$d/journal" 2>/dev/null)"
        [ -n "$cls" ] || cls="UNCLASSIFIED"

        # session_created and spawn_read_at are the pair that makes a later
        # reader able to tell a boot-spawn from a restart-spawn. Emitting the
        # mtime without the instant it was read is what makes an overwritten
        # .spawn look authoritative.
        printf -v payload '{"boot_epoch":%s,"fleet":"%s","session_created":%s,"spawn_mtime":%s,"spawn_read_at":%s,"journal_started":%s,"rung_s":%s,"inject_state":"%s","inject_kind":"%s","inject_epoch":%s,"inject_boot_epoch":%s,"inject_dur_s":%s,"class":"%s","class_why":"%s","snapshot_rc":%s,"close_reason":"%s"}' \
            "$(jnum "$boot_epoch")" "$(json_escape "$fleet")" \
            "$(jnum "$sc")" "$(jnum "$spawn")" "$(jnum "$read_at")" \
            "$(jnum "$started")" "$(jnum "$rung")" \
            "$(json_escape "$i_state")" "$(json_escape "$i_kind")" \
            "$(jnum "$i_epoch")" "$(jnum "$i_boot")" "$(jnum "$i_dur")" \
            "$(json_escape "$cls")" "$(json_escape "$why")" \
            "$(jnum "$snap_rc")" "$(json_escape "$reason")"
        emit_fleet_event "boot_capture" "boot-capture" "$payload" "$botdir" "$bot"
    done < "$d/observed.tsv"

    # The summary carries the shortfall BY NAME. A recorder that quietly covers
    # 12 of 21 reads as complete, which is the coverage-honesty failure the
    # guardrail names — and here it would be read as "9 bots stranded".
    local bad="" summary
    [ -s "$d/bad-manifests" ] && bad="$(cut -f1 "$d/bad-manifests" | tr '\n' ' ' | sed 's/ *$//')"
    printf -v summary '{"boot_epoch":%s,"declared":%s,"observed":%s,"unaccounted":"%s","close_reason":"%s","ladder_end_s":%s,"ladder_derived":%s,"bound_s":%s,"snapshot_rc":%s,"bad_manifests":"%s"}' \
        "$(jnum "$boot_epoch")" "$(jnum "$declared")" "$(jnum "$observed")" \
        "$(json_escape "$unaccounted")" "$(json_escape "$reason")" \
        "$(jnum "$ladder_end")" "$ladder_derived" "$(jnum "$bound_s")" \
        "$(jnum "$snap_rc")" "$(json_escape "$bad")"
    emit_fleet_event "boot_capture_summary" "boot-capture" "$summary" "" "fleet"

    date +%s > "$d/emitted"
}

printf '%s\n' "$SNAP_RC" > "$DIR/snapshot.rc"
emit_boot "$DIR"

# A prior boot that closed and never emitted — the host went down in between.
# Without this the facts sit on disk forever and the boot is lost anyway, which
# is the original defect wearing a different hat.
for prior in "$BASE"/*/; do
    [ -d "$prior" ] || continue
    [ "$(basename "$prior")" = "$BOOT_EPOCH" ] && continue
    [ -f "$prior/closed" ] || continue
    [ -f "$prior/emitted" ] && continue
    echo "boot-capture: flushing unemitted boot $(basename "$prior")" >&2
    emit_boot "${prior%/}"
done

exit 0
