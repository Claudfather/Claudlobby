#!/bin/bash
# plane-host-probe.sh — the host hardware/system facet emitter (chunk 3;
# spec §2b: the probe loop, cause=probe). Reads the VOLATILE host facets
# (F12 moved these OUT of the registry keyframe: they change every minute,
# so they live in metric_samples, not the payload) and emits ONE
# cause=probe batch per run for the seeded host.* metric names
# (`claudlobby/plane/registries.py` METRIC_NAMES).
#
# Subject is the host, keyed by hostname → the SAME uid the registry
# keyframes the host under, so a facet sample joins the Host card with no
# glue. Runs from the composed `plane-host-probe` host timer, NOT the
# ingest-only daemon (scope tripwire). Always on since the F18 closure
# (`plane_armed`: PLANE_EMIT_DISABLED=1 is the one silencer) and
# NON-BLOCKING: every path exits 0, a health monitor is elsewhere — this
# only RECORDS.
#
# Facet readers are the estate's proven cross-platform patterns
# (avail_ram_mb from lib-common; a -P-wrapped df for portable free-GB; the
# vcgencmd decode from host-health-check.sh's convention) — never reinvented. Pi-only facets
# (thermal, undervoltage) are emitted ONLY where vcgencmd exists; on a
# non-Pi host they are absent, not fabricated as zero.

set -uo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLANE_EMIT_CLASS=background   # nothing reads its plane record's result: its socket deadline (#1693, claudlobby/_runtime_scripts/plane-emit.sh)
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
set +e   # lib-common re-arms set -e; a probe must never die mid-run

if ! plane_armed plane-host-probe; then
    exit 0
fi

_host_raw="$(hostname 2>/dev/null || uname -n 2>/dev/null)"
# an empty subject fails min_length=1 and rejects the WHOLE batch — job_ran
# included, the exact edge the proof-of-run exists for (gauntlet SEV-3)
[ -n "$_host_raw" ] || _host_raw="unknown-host"
HOST="$(json_escape "$_host_raw")"

# Where the Linux /proc facets are read from. A test seam only: a fixture dir
# stands in for /proc so a present, missing or garbled field can be pinned.
PROC="${HOST_PROBE_PROC:-/proc}"

# Each reader prints one metric_sample EVENT object, or nothing when the
# facet is unavailable (absent ≠ zero). subject_kind=host; value is a
# number, bool, or small object per the metric.
_samples=""
_add() { _samples="$_samples${_samples:+,}$1"; }
_metric() {  # <metric> <json-value>
    printf '{"event_type":"metric_sample","emitter":"host-probe","fleet":"_host","payload":{"subject_kind":"host","subject":"%s","metric":"%s","value":%s}}' \
        "$HOST" "$1" "$2"
}

# host.load — the 1/5/15 triplet from uptime (portable: BSD "load
# averages:" space-sep, GNU "load average:" comma-sep). Each token is
# VALIDATED as a bare decimal and DROPPED otherwise: a comma-decimal
# locale (de_DE/fr_FR: "0,52, 0,58") would otherwise split a decimal comma
# into a bogus separator and record silently-wrong numbers both directions
# (gauntlet SEV-2). C-locale forced for the same reason.
_load="$(LC_ALL=C uptime 2>/dev/null | awk -F'load average[s]?: *' 'NF>1{
    n=split($2,a,/[, ]+/);
    ok=1; for(i=1;i<=3;i++){ if(a[i] !~ /^[0-9]+\.[0-9]+$/) ok=0 }
    if(n>=3 && ok){printf "{\"one\":%s,\"five\":%s,\"fifteen\":%s}",a[1],a[2],a[3]}}')"
[ -n "$_load" ] && _add "$(_metric host.load "$_load")"

# host.mem_available_mb — lib-common avail_ram_mb (the fleet-memory-check
# figure, one definition).
_mem="$(avail_ram_mb 2>/dev/null)"
case "$_mem" in ''|*[!0-9]*) _mem="" ;; esac
[ -n "$_mem" ] && _add "$(_metric host.mem_available_mb "$_mem")"

# host.disk_free_gb — free GB on / (df -P is portable; column 4 is 1K blocks
# available).
_disk="$(df -Pk / 2>/dev/null | awk 'NR==2{printf "%d",$4/1048576}')"
case "$_disk" in ''|*[!0-9]*) _disk="" ;; esac
[ -n "$_disk" ] && _add "$(_metric host.disk_free_gb "$_disk")"

# host.thermal_flags + host.undervoltage — Pi vcgencmd only (absent on a
# non-Pi host, never a fabricated 0). The raw 0xN flags ride as a string;
# undervoltage is bit 0 (NOW) OR bit 16 (occurred-since-boot).
if command -v vcgencmd >/dev/null 2>&1; then
    _raw="$(vcgencmd get_throttled 2>/dev/null)"
    _hex="${_raw#*=}"
    if [[ "$_hex" =~ ^0x[0-9a-fA-F]+$ ]]; then
        _n=$(( _hex ))
        _add "$(_metric host.thermal_flags "\"$_hex\"")"
        if (( _n & 0x1 )) || (( _n & 0x10000 )); then _uv=true; else _uv=false; fi
        _add "$(_metric host.undervoltage "$_uv")"
    fi
fi

# host.boot_time — the last boot instant, UTC+Z on BOTH platforms (a naive
# local Linux stamp vs a UTC macOS stamp made Pi and Mac uncomparable —
# gauntlet SEV-3). Linux: /proc/stat btime (epoch, unambiguous) →
# date -u. macOS: kern.boottime's `sec = NNN` — the FIRST digit run, NOT
# a greedy match, because the string also holds `usec = …` and `.*sec = `
# greedily captured the wrong number (every Mac recorded a 1970 boot
# every minute — gauntlet SEV-1, live).
_boot=""; _bsec=""
if [ -r "$PROC/stat" ]; then
    _bsec="$(awk '/^btime /{print $2}' "$PROC/stat" 2>/dev/null)"
else
    # ONE parser (lib-common.sh boot_epoch_from_sysctl): this file had its own
    # copy of the kern.boottime sed while resolve_boot_epoch kept the greedy
    # form that captured usec -- the second-copy divergence CLAUDE.md warns
    # about, found live 2026-09-21. It also resolves sysctl off the launchd
    # PATH, which lacks /usr/sbin.
    _bsec="$(boot_epoch_from_sysctl 2>/dev/null || true)"
fi
case "$_bsec" in ''|*[!0-9]*) _bsec="" ;; esac
if [ -n "$_bsec" ]; then
    _boot="$(date -u -r "$_bsec" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
        || date -u -d "@$_bsec" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)"
fi
[ -n "$_boot" ] && _add "$(_metric host.boot_time "\"$_boot\"")"

# host.plane_wal_bytes — the plane's WAL, one stat (#1905). A reader holding a
# snapshot keeps the daemon's checkpoint from resetting it, and nothing else
# records how big it got. Only where the db exists: a host with no plane has no
# WAL (absent, not 0), while a missing -wal beside an existing db IS an empty
# WAL, because SQLite deletes it on the last clean close.
_db="$CLAUDLOBBY_ROOT/state/plane/plane.db"
if [ -f "$_db" ]; then
    if [ -e "$_db-wal" ]; then _wal="$(stat_size "$_db-wal" 2>/dev/null)"; else _wal=0; fi
    case "$_wal" in ''|*[!0-9]*) _wal="" ;; esac
    [ -n "$_wal" ] && _add "$(_metric host.plane_wal_bytes "$_wal")"
fi

# #1644: what splits load into CPU and IO, for reading the minutes before a
# reset. Linux load counts tasks waiting on IO as well as tasks waiting for a
# CPU, so load alone cannot tell a CPU burst from an SD-card stall. PSI would,
# but /proc/pressure needs psi=1 on the kernel command line, a host decision.
# Each facet reads /proc and is ABSENT when its file or a field is unreadable
# or not a plain number (macOS has no /proc): never a fabricated 0. The
# cumulative ones count since boot; a rate is the difference of two samples.

# host.swap_used_mb — SwapTotal - SwapFree, in MB.
_swap=""
if [ -r "$PROC/meminfo" ]; then
    _swap="$(awk '$1 == "SwapTotal:" && $2 ~ /^[0-9]+$/ { t = $2; ht = 1 }
        $1 == "SwapFree:" && $2 ~ /^[0-9]+$/ { f = $2; hf = 1 }
        END { if (ht && hf && t >= f) printf "%d", (t - f) / 1024 }' "$PROC/meminfo" 2>/dev/null)"
fi
case "$_swap" in ''|*[!0-9]*) _swap="" ;; esac
[ -n "$_swap" ] && _add "$(_metric host.swap_used_mb "$_swap")"

# host.swap_pages — pages swapped in and out since boot (vmstat pswpin/pswpout).
# Each sample object here is built by awk, never in a bash word: bash 3.2
# (macOS /bin/bash) brace-expands a {a,b} inside a quoted $( ) argument and
# splits the object in two, which rejects the whole batch.
_swp=""; _swp_why=""
if [ -r "$PROC/vmstat" ]; then
    _swp="$(awk '$1 == "pswpin" && $2 ~ /^[0-9]+$/ { i = $2; hi = 1 }
        $1 == "pswpout" && $2 ~ /^[0-9]+$/ { o = $2; ho = 1 }
        END { if (hi && ho) printf "{\"in\":%s,\"out\":%s}", i, o }' "$PROC/vmstat" 2>/dev/null)"
    [ -n "$_swp" ] || _swp_why="no pswpin and pswpout numbers in $PROC/vmstat"
else
    _swp_why="no $PROC/vmstat"
    [ "$_OS" = Darwin ] && _swp_why="$_swp_why (macOS)"
fi
[ -n "$_swp" ] && _add "$(_metric host.swap_pages "$_swp")"
# The thrash check (below) reads the out count this sample carries: the number
# after its last colon, less the closing brace. _swp_why says why there is none.
_swp_out="${_swp##*:}"; _swp_out="${_swp_out%?}"

# host.procs — processes runnable now, and blocked on IO now (stat).
_procs=""
if [ -r "$PROC/stat" ]; then
    _procs="$(awk '$1 == "procs_running" && $2 ~ /^[0-9]+$/ { r = $2; hr = 1 }
        $1 == "procs_blocked" && $2 ~ /^[0-9]+$/ { b = $2; hb = 1 }
        END { if (hr && hb) printf "{\"running\":%s,\"blocked\":%s}", r, b }' "$PROC/stat" 2>/dev/null)"
fi
[ -n "$_procs" ] && _add "$(_metric host.procs "$_procs")"
# The thrash alert carries the blocked count, read from the sample the same way.
_blocked="${_procs##*:}"; _blocked="${_blocked%?}"

# host.cpu_ticks — iowait ticks since boot, and every CPU tick since boot, from
# the aggregate cpu line. The iowait share of a minute is the difference of
# iowait over the difference of total, with no tick rate or core count
# assumed. The total stops at steal: guest and guest_nice are already inside
# user and nice.
_ticks=""
if [ -r "$PROC/stat" ]; then
    _ticks="$(awk '$1 == "cpu" {
            ok = (NF >= 6); t = 0; last = (NF < 9) ? NF : 9
            for (i = 2; i <= last; i++) { if ($i !~ /^[0-9]+$/) ok = 0; t += $i }
            if (ok) printf "{\"iowait\":%s,\"total\":%.0f}", $6, t
            exit }' "$PROC/stat" 2>/dev/null)"
fi
[ -n "$_ticks" ] && _add "$(_metric host.cpu_ticks "$_ticks")"

# host.job_ran — one proof-of-run sample per probe, so a silent probe (a
# facet-less host, an unarmed fleet) is distinguishable from a probe that
# never fired at all.
_add "$(_metric host.job_ran 1)"

# Fire-and-forget, so a socket cooldown stages it for the daemon (#1657).
printf '{"events":[%s]}' "$_samples" | PLANE_EMIT_COOLDOWN_STAGE=1 plane_emit_events plane-host-probe || true

# memory_thrash (#1645): swap-out over this host's line for its number of readings
# in a row raises one FLEET ALERT an episode, naming the largest bots by memory.
# memory-thrash.py holds the rule: it keeps the last reading and the episode in
# state/memory-thrash/ and reads the host's line from the lines file there. No
# file is no check, said once on this job's journal. A file it refuses is no check
# either, and raises memory_thrash_unarmed once, so a bad edit never turns the
# alert off unseen. It runs after the samples are sent, so a slow check never
# holds them, and it reads the count the samples carry, so a replay of the
# recorded samples is the check that ran.

# _thrash_sent DIR WHAT MARK...: after an alert, mark it sent (memory-thrash.py
# delivered MARK...) once Telegram or the manager's pane took it; until then the
# journal says so and the next reading raises it again. (notify_currency also
# counts a host with no Telegram target as sent, because a notice has no other
# leg; this alert has the manager's pane.)
_thrash_sent() {
    local dir="$1" what="$2"
    shift 2
    if [ "${_ALERT_DELIVERED:-0}" = 1 ] || [ "${_ALERT_TMUX_REACHED:-0}" = 1 ]; then
        python3 -I "$LIB_DIR/memory-thrash.py" delivered --state-dir "$dir" "$@" || true
    else
        printf 'memory-thrash: %s: no channel took the alert; the next reading raises it again\n' \
            "$what" >&2
    fi
}

_thrash() {
    local dir="$CLAUDLOBBY_ROOT/state/memory-thrash" boot="" given verdict kind id text
    local dirs=() bots_dir bots msg
    command -v python3 >/dev/null 2>&1 || return 0
    given=(--skip "$_swp_why")
    if [ -n "$_swp_out" ]; then
        read -r boot 2>/dev/null < "$PROC/sys/kernel/random/boot_id" || boot=""
        given=(--out "$_swp_out" --boot-id "$boot")
    fi
    verdict="$(python3 -I "$LIB_DIR/memory-thrash.py" check --state-dir "$dir" "${given[@]}")"
    IFS="$(printf '\t')" read -r kind id text <<<"$verdict"
    case "$kind" in
        alert)
            while IFS= read -r bots_dir; do
                dirs+=(--bots-dir "$bots_dir")
            done < <(host_bots_dirs 2>/dev/null)
            bots="$(with_timeout 30 python3 -I "$LIB_DIR/memory-thrash.py" bots --proc "$PROC" \
                ${dirs[@]+"${dirs[@]}"} 2>/dev/null)"
            [ -n "$bots" ] || bots="Largest bots by memory: not read."
            msg="swap thrash on $_host_raw: $text, MemAvailable ${_mem:-unread} MB, ${_blocked:-an unread number of} processes blocked on IO. $bots Read the named bots first, then act through the manager's doors: bot compact, bot restart --fresh or bot stop, once their work is committed."
            emit_failure_alert "$(resolve_bots_dir)" "memory_thrash" "$msg"
            _thrash_sent "$dir" "episode $id" --episode "$id"
            ;;
        refused)
            msg="swap thrash alert off on $_host_raw: its line file was refused: $text. This host has no swap thrash alert until the file is fixed; the check reads it every minute, and memory-thrash.py status reads the rule back."
            emit_failure_alert "$(resolve_bots_dir)" "memory_thrash_unarmed" "$msg"
            _thrash_sent "$dir" "the refused line file" --refused "$id"
            ;;
    esac
    return 0
}
_thrash || true
exit 0
