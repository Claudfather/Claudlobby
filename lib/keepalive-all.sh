#!/bin/bash
# Fleet-wide keepalive — iterates every enrolled bot in a fleet's runtime
# directory and runs keepalive.sh against each.
#
# Designed to be invoked every 60s by a launchd LaunchAgent (macOS) or
# a systemd timer (Linux). Enrolled by sealed host activation.
#
# Usage: keepalive-all.sh [<fleet-name> | <fleet-runtime-bots-dir>]
#   Composed fleet units pass the fleet NAME (the uniform fleet-job arg
#   convention — reload-fleet, fleet-pulse, weekly-worker-restart all take a
#   name); an absolute path selects a bots dir directly (manual use).
#   Default: $CLAUDLOBBY_ROOT/local/$CLAUDLOBBY_FLEET/runtime/bots
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

if [ -n "${1:-}" ]; then
    case "$1" in
        /*) BOTS_DIR="$1" ;;
        *) BOTS_DIR=$(resolve_bots_dir "$1") ;;
    esac
else
    BOTS_DIR=$(resolve_bots_dir)
    if [ -z "${CLAUDLOBBY_FLEET:-}${FLEET_NAME:-}" ]; then
        echo "keepalive-all: pass a runtime/bots dir or set CLAUDLOBBY_FLEET" >&2
        exit 2
    fi
fi

KEEPALIVE="$LIB_DIR/keepalive.sh"
LOG="$CLAUDLOBBY_ROOT/state/logs/keepalive-all.log"

setup_log_dir "$LOG"
TS=$(ts_iso)

if [ ! -x "$KEEPALIVE" ]; then
    echo "$TS FATAL — $KEEPALIVE not executable" >>"$LOG"
    exit 1
fi

if [ ! -d "$BOTS_DIR" ]; then
    echo "$TS FATAL — runtime bots dir not found: $BOTS_DIR" >>"$LOG"
    exit 1
fi

install_error_trap ""

# fleet.yaml is authoritative for which bots this fleet owns. Skip stale/cross-fleet
# residue dirs that are no longer declared — otherwise two fleets' keepalive timers
# both supervise a same-named bot. Derive the fleet.yaml from the fleet name, or
# from the bots-dir parent when called with an explicit dir. Empty result (no/
# unreadable fleet.yaml) → scan every dir, preserving prior behavior.
_kf_fleet="${CLAUDLOBBY_FLEET:-${FLEET_NAME:-}}"
if [ -n "$_kf_fleet" ]; then
    # Flat local/<fleet> byte-identically, or nested local/<system>/<fleet>.
    _kf_dir=$(resolve_fleet_dir "$_kf_fleet") || _kf_dir="$CLAUDLOBBY_ROOT/local/$_kf_fleet"
    _kf_yaml="$_kf_dir/fleet.yaml"
else
    _kf_yaml="$(dirname "$(dirname "$BOTS_DIR")")/fleet.yaml"
fi
# #1146: an empty roster here does NOT mean do-nothing — bot_in_fleet reads
# empty as "declared", so a manifest that drifts out of the documented
# 2/4-space shape makes this act on EVERY bot dir on the host, other fleets
# included. Classified as over-inclusive-action, not a delete; if this ever
# grows a destructive leg, move it to declared_bots_strict (the loud door).
declared_bots=$(parse_fleet_bots "$_kf_yaml")

AGENTS_DIR="$HOME/Library/LaunchAgents"
failed=""
paused=""
attempted=0

for conf in "$BOTS_DIR"/*/bot.conf; do
    [ -f "$conf" ] || continue
    bot_dir="$(dirname "$conf")"
    bot_name="$(basename "$bot_dir")"
    bot_in_fleet "$bot_name" "$declared_bots" || continue   # not in fleet.yaml → not ours to supervise

    # Read BOT_SERVICE from bot.conf (falls back to bot_name for pre-generate fleets).
    svc=$(bot_conf_get "$bot_dir" BOT_SERVICE "$bot_name")

    # Only touch bots whose service is registered with the host's init.
    if [ "$_OS" = "Darwin" ]; then
        if [ ! -f "$AGENTS_DIR/$svc.plist" ]; then
            # Fallback: check for legacy plist pattern (*.$bot_name.plist)
            plist=$(find "$AGENTS_DIR" -maxdepth 1 -name "*.$bot_name.plist" 2>/dev/null | head -1) || true
            [ -n "$plist" ] || continue
        fi
    else
        # Linux: check BOT_SERVICE unit first, fall back to bot_name
        if [ ! -f "$HOME/.config/systemd/user/$svc.service" ] && \
           [ ! -f "$HOME/.config/systemd/user/$bot_name.service" ]; then
            continue
        fi
    fi

    attempted=$((attempted + 1))
    if "$KEEPALIVE" "$bot_dir"; then
        :
    else
        rc=$?
        if [ "$rc" -eq 75 ]; then
            paused="${paused:+$paused, }$bot_name"
            echo "$TS PAUSED — activation holds the host lock; $bot_name will be checked next sweep" >>"$LOG"
        else
            failed="${failed:+$failed, }$bot_name (exit $rc)"
            echo "$TS ERROR — keepalive.sh failed for $bot_name (exit $rc)" >>"$LOG"
        fi
    fi
done

# Admission happens before keepalive.sh installs its ERR trap. A stale CLI or
# broken selected release would otherwise fail every bot and leave this timer
# green. One fleet-level alert is enough; retain the marker only after delivery
# so an undelivered alert is retried. An activation-lock pause is transient and
# must not clear a previous fault until a sweep actually admits the bots.
alert_key=$(printf '%s' "$BOTS_DIR" | cksum | awk '{print $1}')
alert_state="$CLAUDLOBBY_ROOT/state/keepalive-admission-$alert_key.alerted"
if [ -n "$failed" ]; then
    message="keepalive admission/runtime failed for $failed; inspect $LOG and the selected release"
    printf '%s ERROR — %s\n' "$TS" "$message" >&2
    if [ ! -f "$alert_state" ]; then
        emit_failure_alert "$BOTS_DIR" "keepalive_failed" "$message" || true
        if [ "${_ALERT_DELIVERED:-0}" -eq 1 ]; then
            printf '%s\n' "$failed" >"$alert_state"
        fi
    fi
    exit 1
fi
if [ "$attempted" -eq 0 ]; then
    : # No admitted bot was observed; preserve any previous fault marker.
elif [ -z "$paused" ]; then
    rm -f "$alert_state"
else
    echo "$TS PAUSED — activation lock held for: $paused" >>"$LOG"
fi
