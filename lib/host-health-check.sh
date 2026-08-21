#!/bin/bash
# host-health-check.sh -- early-warning for the two host-hardware failure modes
# that silently freeze a whole cheap-hardware fleet: (1) under-voltage / thermal
# throttling (Raspberry Pi vcgencmd), and (2) storage stalls -- SD/MMC bus hangs,
# kernel hung-task, and filesystem I/O errors (kernel log). Raises a FLEET ALERT
# via the shared emit_failure_alert path (fleet event + manager tmux nudge +
# Telegram) when a signature is seen, DE-DUPED via a state file so a standing
# condition alerts once (until it changes), not every run.
#
# Motivation: a Raspberry Pi fleet host froze when its SD card dropped off the
# MMC bus (kworker mmc_rescan blocked in __mmc_claim_host); the first kernel
# hung-task warning preceded the full freeze by ~2.5h, but the daily monitors
# were too coarse to see it and had no signal for it. This closes that gap.
#
# Runs as the host-health-check host job (system.yaml host.jobs, enrolled by
# setup-system). Pi-focused but HARMLESS elsewhere: each probe self-skips when
# its tool is absent -- vcgencmd on non-Pi, journalctl on non-systemd/macOS
# (the storage probe still generalizes to any Linux host with a journal).
#
# Usage:
#   host-health-check.sh [--since <systemd-since>]
#     --since   limit the kernel-log scan window (default: this boot, -b 0)
#
# Output goes to $CLAUDLOBBY_ROOT/lib/host-health-check.log ; de-dup fingerprint
# in host-health-check.state. Exits 0 always -- monitors never abort the fleet.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

SINCE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --since) SINCE="$2"; shift 2 ;;
        -h|--help) show_help; exit 0 ;;
        *) echo "host-health-check: unknown arg: $1" >&2; exit 2 ;;
    esac
done

LOG="$CLAUDLOBBY_ROOT/lib/host-health-check.log"
STATE="$CLAUDLOBBY_ROOT/lib/host-health-check.state"
setup_log_dir "$LOG"
TS=$(ts_iso)
FLEET="${CLAUDLOBBY_FLEET:-${FLEET_NAME:-}}"

# --- Probe 1: under-voltage / throttling (Raspberry Pi vcgencmd) --------------
# get_throttled returns throttled=0xN. Bit 0 under-voltage NOW, 1 arm-freq-cap
# NOW, 2 throttled NOW, 3 soft-temp-limit NOW; bits 16-19 are the same four as
# "has occurred since boot" latches. Non-Pi hosts have no vcgencmd -> skip.
undervoltage_report() {
    command -v vcgencmd >/dev/null 2>&1 || return 0
    local raw hex n parts mask label c
    raw=$(vcgencmd get_throttled 2>/dev/null) || return 0
    hex=${raw#*=}
    if [ -z "$hex" ]; then return 0; fi
    # Only decode a well-formed hex value, ANCHORED end-to-end. An unanchored
    # match on a 0x-prefix-then-garbage value (0x1zzz) would reach $(( )), whose
    # arithmetic error is FATAL under set -e (it aborts past a `|| return 0`) and
    # crashes the whole monitor with no verdict logged. Reject it before decode.
    [[ "$hex" =~ ^0x[0-9a-fA-F]+$ ]] || return 0
    n=$(( hex ))
    parts=""
    for c in "0x1:under-voltage-NOW" "0x2:arm-cap-NOW" "0x4:throttled-NOW" \
             "0x8:soft-temp-NOW" "0x10000:under-voltage-occurred" \
             "0x20000:arm-cap-occurred" "0x40000:throttling-occurred" \
             "0x80000:soft-temp-occurred"; do
        mask=${c%%:*}; label=${c#*:}
        if (( n & mask )); then parts="$parts $label"; fi
    done
    # A value that decodes to no documented bit (0x0, the non-canonical 0x00, or
    # an undefined bit >= 20) is not a throttling event -- emit nothing rather
    # than an empty-label alert.
    if [ -z "$parts" ]; then return 0; fi
    printf 'power(%s):%s' "$hex" "$parts"
}

# --- Probe 2: storage stall / I/O errors (kernel log) -------------------------
# The SD/MMC-hang, kernel hung-task and ext4 I/O-error signatures that precede a
# storage-induced freeze. Generalizes to any Linux host with a journal; macOS
# has no journalctl -> skip.
storage_report() {
    command -v journalctl >/dev/null 2>&1 || return 0
    local since_args hits cnt rep
    since_args=(-k --no-pager)
    if [ -n "$SINCE" ]; then since_args+=(--since "$SINCE"); else since_args+=(-b 0); fi
    hits=$(journalctl "${since_args[@]}" 2>/dev/null \
        | grep -iE 'blocked for more than [0-9]+ seconds|__mmc_claim_host|mmc_rescan|mmc(blk)?[0-9].*(error|timeout|reset)|stuck in programming state|EXT4-fs error|I/O error' \
        | tail -8) || true
    if [ -z "$hits" ]; then return 0; fi
    cnt=$(printf '%s\n' "$hits" | grep -c . || true)
    rep=$(printf '%s\n' "$hits" | tail -1 | sed -E 's/.* kernel: //')
    printf 'storage(%s hits): %s' "$cnt" "$rep"
}

# Join the power and storage findings into one " | "-separated line, omitting
# whichever probe was clean. Shared by the human-readable message and (with
# storage digit-collapsed) the de-dup fingerprint, so the two never drift apart.
join_finding() {  # $1=power  $2=storage
    local out=""
    if [ -n "$1" ]; then out="$1"; fi
    if [ -n "$2" ]; then out="${out:+$out | }$2"; fi
    printf '%s' "$out"
}

# Stable signature for a storage finding: preserve mmc device identity (mmc0,
# mmcblk1, ...) so two DISTINCT devices never collide to one fingerprint, THEN
# collapse the remaining volatile digit runs (kworker thread, PID, sector, error
# code, elapsed seconds, hit count) so one device's ongoing incident still
# de-dups to a single alert. Identity preservation is mmc-scoped by design -- the
# dominant device class on this SD/MMC fleet; if storage_report widens its device
# vocabulary (sd*, nvme*), widen this token pattern too or those classes collapse.
storage_signature() {  # $1=storage finding text
    local devs
    devs=$(printf '%s' "$1" | grep -oE 'mmc(blk)?[0-9]+' | sort -u | tr '\n' ',' || true)
    printf '%s%s' "$devs" "$(printf '%s' "$1" | sed -E 's/[0-9]+/#/g')"
}

POWER=$(undervoltage_report || true)
STORAGE=$(storage_report || true)

FINDING=$(join_finding "$POWER" "$STORAGE")

# --- Clean path ---------------------------------------------------------------
if [ -z "$FINDING" ]; then
    echo "$TS OK -- host health clean (power + storage)" >>"$LOG"
    printf 'clean\n' >"$STATE"
    exit 0
fi

# --- Alert path (de-duped by a STABLE fingerprint) ----------------------------
# The fingerprint identifies a CONDITION, not a log line: it stays constant while
# a condition persists across runs (so a standing condition alerts ONCE), yet
# changes when the condition genuinely worsens OR the host reboots.
#   - power: the decoded hex+labels are already stable and meaningful -- kept
#     exact, so a worsened throttle state has a different fingerprint and re-alerts.
#   - storage: preserve mmc device identity, then collapse the volatile digit runs
#     (see storage_signature) so one device's ongoing stall fingerprints identically
#     while a DIFFERENT device failing still produces a fresh fingerprint and re-alerts.
#   - boot id: folded in so a recurrence after a reboot always re-alerts even when
#     the finding text is byte-identical (the state file is boot-unaware while the
#     -b0 log scan is boot-scoped). Overridable for hosts without a /proc boot_id.
SIGTEXT=$(join_finding "$POWER" "$(storage_signature "$STORAGE")")
BOOT_ID="${HOST_HEALTH_BOOT_ID:-$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || uptime -s 2>/dev/null || echo "")}"
SIG=$(printf '%s|%s' "$BOOT_ID" "$SIGTEXT" | cksum | awk '{print $1}')
LAST=$(cat "$STATE" 2>/dev/null || echo "")
MSG="host hardware health warning on $(hostname): $FINDING"

if [ "$SIG" = "$LAST" ]; then
    echo "$TS REPEAT -- $MSG (already alerted; fp $SIG)" >>"$LOG"
else
    echo "$TS ALERT -- $MSG (fp $SIG)" >>"$LOG"
    KEY="host_health"
    case "$FINDING" in *power*|*under-voltage*) KEY="undervoltage" ;; esac
    case "$FINDING" in *storage*) KEY="storage_stall" ;; esac
    emit_failure_alert "$(resolve_bots_dir "$FLEET")" "$KEY" "$MSG" || true
    # Gate the fingerprint on delivery. This file is the de-dup state: once SIG
    # is written, every later run takes the REPEAT branch above and NEVER
    # ATTEMPTS DELIVERY AGAIN. Writing it after a failed send is what turned one
    # undelivered hardware alarm into permanent silence -- the log filled with
    # REPEAT lines while nobody had ever been told once. Same class as the
    # fleet-pulse debounce marker, in a second file.
    if [ "${_ALERT_DELIVERED:-0}" -eq 1 ]; then
        printf '%s\n' "$SIG" >"$STATE"
    else
        echo "$TS DELIVERY-FAILED -- $MSG (fp $SIG; state NOT written, will retry next run)" >>"$LOG"
        printf '%s ALERT-DELIVERY-FAILED %s: not delivered, fingerprint withheld so the next run retries\n' \
            "$TS" "$KEY" >&2
    fi
fi
exit 0
