#!/bin/bash
# plane-host-probe.sh — the host hardware/system facet emitter (chunk 3;
# spec §2b: the probe loop, cause=probe). Reads the VOLATILE host facets
# (F12 moved these OUT of the registry keyframe: they change every minute,
# so they live in metric_samples, not the payload) and emits ONE
# cause=probe batch per run for the seven seeded host.* metric names.
#
# Subject is the host, keyed by hostname → the SAME uid the registry
# keyframes the host under, so a facet sample joins the Host card with no
# glue. Runs from the composed `plane-host-probe` host timer, NOT the
# ingest-only daemon (scope tripwire). DORMANT behind PLANE_EMIT_ENABLED
# (the standard plane emission flag, stamped onto the unit by the
# host-timer arming carrier) and NON-BLOCKING: every path exits 0, a
# health monitor is elsewhere — this only RECORDS.
#
# Facet readers are the estate's proven cross-platform patterns
# (avail_ram_mb from lib-common; a -P-wrapped df for portable free-GB; the
# vcgencmd decode from host-health-check.sh's convention) — never reinvented. Pi-only facets
# (thermal, undervoltage) are emitted ONLY where vcgencmd exists; on a
# non-Pi host they are absent, not fabricated as zero.

set -uo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
if [ -r /proc/stat ]; then
    _bsec="$(awk '/^btime /{print $2}' /proc/stat 2>/dev/null)"
elif command -v sysctl >/dev/null 2>&1; then
    _bsec="$(sysctl -n kern.boottime 2>/dev/null \
        | sed -n 's/[^0-9]*\([0-9][0-9]*\).*/\1/p')"
fi
case "$_bsec" in ''|*[!0-9]*) _bsec="" ;; esac
if [ -n "$_bsec" ]; then
    _boot="$(date -u -r "$_bsec" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
        || date -u -d "@$_bsec" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)"
fi
[ -n "$_boot" ] && _add "$(_metric host.boot_time "\"$_boot\"")"

# host.job_ran — one proof-of-run sample per probe, so a silent probe (a
# facet-less host, an unarmed fleet) is distinguishable from a probe that
# never fired at all.
_add "$(_metric host.job_ran 1)"

printf '{"events":[%s]}' "$_samples" | plane_emit_events plane-host-probe || true
exit 0
