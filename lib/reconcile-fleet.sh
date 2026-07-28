#!/bin/bash
# Reconcile a fleet's runtime state: tmux sessions ↔ host service units ↔ fleet.yaml.
#
# Reports five buckets:
#   ✓ healthy           — defined in fleet.yaml + has unit + has tmux session
#   ⚠ orphan            — defined in fleet.yaml + has tmux session but NO unit (unsupervised, up)
#   ⚠ missing           — defined in fleet.yaml + has unit but NO tmux session (supervised, down)
#   ⚠ unsupervised-down — defined in fleet.yaml + NO unit and NO tmux session
#   🚨 unbound          — tmux session named like a bot but NOT in any fleet.yaml (rogue)
#
# The bucket names describe what was OBSERVED, never how the bot got there:
# unsupervised-down covers a torn-down bot, a never-generated one, and a
# generated-but-never-started one alike. Intent (parked on purpose vs fell off
# by accident) is not observable from tmux + unit state and is not claimed here.
#
# Usage: reconcile-fleet.sh <fleet-name> [--enroll]
#   --enroll : auto-enroll orphans by calling spin-up-bot.sh on each.
#              Deliberately NOT extended to unsupervised-down: reviving a bot
#              that was taken down on purpose needs an intent signal this script
#              cannot see. Do not "fix" the asymmetry without one.
set -euo pipefail

FLEET="${1:?Usage: reconcile-fleet.sh <fleet-name> [--enroll]}"
ENROLL="${2:-}"

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""
# Flat local/<fleet> resolves byte-identically; nested local/<system>/<fleet>
# resolves too. Flat fallback keeps the pre-generate error path (missing yaml).
FLEET_DIR=$(resolve_fleet_dir "$FLEET") || FLEET_DIR="$CLAUDLOBBY_ROOT/local/$FLEET"
FLEET_YAML="$FLEET_DIR/fleet.yaml"
RUNTIME_DIR=$(resolve_bots_dir "$FLEET")

if [ ! -f "$FLEET_YAML" ]; then
    echo "reconcile-fleet: $FLEET_YAML not found" >&2
    exit 1
fi

# Bot-name extraction lives in lib-common.sh (parse_fleet_bots) so fleet-pulse,
# keepalive-all, and reconcile-fleet share ONE fleet.yaml parser — no drift.

# 1. Bots defined in this fleet's yaml (top-level keys under bots:)
defined=$(parse_fleet_bots "$FLEET_YAML" | sort -u)

# 2. Per-bot tmux servers. Each bot runs on its OWN server (-L <socket>), so a
#    single `tmux ls` only sees the legacy default socket. has_tmux is resolved
#    per-bot below against each bot's own socket; the unbound scan walks the
#    socket files directly.

# 3. Build buckets by checking each defined bot against tmux + host service state.
#    Unit names come from bot.conf (BOT_SERVICE), not from parsing filenames.
#    Buckets are space-separated single-line word accumulators — consumers
#    iterate unquoted (for b in $bucket), never a line-oriented read. Names are
#    word-safe by construction (fleet.yaml keys); single-line is an external
#    contract (migrate-fleet-to-system.sh parses the healthy: line, head -1).
healthy=""; orphan=""; missing=""; unsup_down=""; unbound=""

while IFS= read -r b; do
    [ -z "$b" ] && continue
    bot_dir="$RUNTIME_DIR/$b"

    has_tmux=0; has_unit=0
    bot_socket=$(tmux_socket_for_bot "$bot_dir" 2>/dev/null || true)
    check_tmux_session "$b" "$bot_socket" 2>/dev/null && has_tmux=1
    # Unit presence via the shared predicate (bot_unit_present) — the same
    # check setup-fleet's skip-healthy uses, so audit and apply never drift.
    bot_unit_present "$b" "$bot_dir" && has_unit=1

    # Exhaustive by construction: an else, not a fourth condition. A defined bot
    # that matches no arm is reported in no bucket at all — invisible precisely
    # when it most needs seeing.
    if   [ $has_tmux = 1 ] && [ $has_unit = 1 ]; then healthy="$healthy $b"
    elif [ $has_tmux = 1 ] && [ $has_unit = 0 ]; then orphan="$orphan $b"
    elif [ $has_tmux = 0 ] && [ $has_unit = 1 ]; then missing="$missing $b"
    else                                              unsup_down="$unsup_down $b"
    fi
done <<< "$defined"

# Unbound: a live session on ANY per-bot server whose name matches no defined
# bot in ANY fleet.yaml. A rogue/leftover server is invisible to a default-socket
# `tmux ls`, so walk the socket files under $TMUX_TMPDIR/tmux-$(id -u)/ directly
# and list each server's sessions.
# `|| continue` (not `&& parse`): a non-matching glob stays literal and would be
# the loop's LAST iteration — its failed [ -f ] test would make the loop exit
# nonzero, and under pipefail that nonzero rides `| sort -u` into the $() and
# trips set -e. continue keeps the last status 0. (Both flat globs + the nested one.)
all_defined=$(for fy in "$CLAUDLOBBY_ROOT"/local/*/fleet.yaml "$CLAUDLOBBY_ROOT"/local/*/*/fleet.yaml; do
    [ -f "$fy" ] || continue
    parse_fleet_bots "$fy"
done | sort -u)
_sock_dir="${TMUX_TMPDIR:-/tmp}/tmux-$(id -u)"
if [ -d "$_sock_dir" ]; then
    for _sf in "$_sock_dir"/*; do
        [ -S "$_sf" ] || continue
        while IFS= read -r s; do
            [ -z "$s" ] && continue
            echo "$all_defined" | grep -qx "$s" || unbound="$unbound $s"
        done <<< "$("$_TMUX_BIN" -L "$(basename "$_sf")" ls 2>/dev/null | awk -F: '{print $1}' || true)"
    done
fi

# Report
echo "Fleet: $FLEET"
echo "  ✓ healthy:  ${healthy:-(none)}"
echo "  ⚠ orphan:   ${orphan:-(none)}"
echo "  ⚠ missing:  ${missing:-(none)}"
echo "  ⚠ unsupervised-down: ${unsup_down:-(none)}   ← neither supervised nor running; keepalive cannot revive it (fix: claudlobby generate, then lib/spin-up-bot.sh <bot-dir>)"
echo "  🚨 unbound: ${unbound:-(none)}   ← if non-empty, investigate before killing"

# --- Default-job drift --------------------------------------------------------
# Every composed fleet timer (merged system.yaml defaults.jobs + opt-ins like
# code-audit-sweep) should be enrolled on this host. A composed unit with no
# live enrollment is drift — the default tier silently rotting.
job_drift=""
_timers_dir="$FLEET_DIR/runtime/fleet/timers"
if [ -d "$_timers_dir" ]; then
    # Composed-but-dormant units (unit_is_dormant: enroll: false jobs) are
    # opt-in — not enrolled by design, so never drift.
    case "$_OS" in
    Linux)
        for _t in "$_timers_dir"/*.timer; do
            [ -e "$_t" ] || continue
            _tb="$(basename "$_t" .timer)"
            unit_is_dormant "$_timers_dir" "$_tb" && continue
            systemctl --user is-enabled "$_tb.timer" >/dev/null 2>&1 || job_drift="$job_drift $_tb"
        done
        ;;
    Darwin)
        _uid="$(id -u)"
        for _t in "$_timers_dir"/*.plist; do
            [ -e "$_t" ] || continue
            _tb="$(basename "$_t" .plist)"
            unit_is_dormant "$_timers_dir" "$_tb" && continue
            # Live launchd state, not file presence — a copied-but-never-
            # bootstrapped plist is still drift (mirrors is-enabled on Linux).
            launchctl print "gui/$_uid/$_tb" >/dev/null 2>&1 || job_drift="$job_drift $_tb"
        done
        ;;
    esac
    echo "  ⚠ job-drift: ${job_drift:-(none)}   ← composed timers not enrolled (fix: lib/setup-fleet $FLEET)"
fi

# --- Root-cause diagnostics for missing bots ---------------------------------
_diagnose_missing_bot() {
    local bot="$1"
    local bot_dir_diag="$RUNTIME_DIR/$bot"

    echo "  ── $bot ──"

    # 1. Service unit status
    case "$_OS" in
    Linux)
        local unit_name
        unit_name="$(bot_conf_get "$bot_dir_diag" BOT_SERVICE "$bot").service"
        local exit_status
        exit_status=$(systemctl --user show "$unit_name" --property=ExecMainStatus --value 2>/dev/null || echo "unknown")
        local active_state
        active_state=$(systemctl --user show "$unit_name" --property=ActiveState --value 2>/dev/null || echo "unknown")
        local sub_state
        sub_state=$(systemctl --user show "$unit_name" --property=SubState --value 2>/dev/null || echo "unknown")
        echo "    service: $active_state/$sub_state (exit code: $exit_status)"

        # 2. Last journal lines
        local journal
        journal=$(journalctl --user -u "$unit_name" -n 10 --no-pager 2>/dev/null || echo "(journal unavailable)")
        if [ -n "$journal" ] && [ "$journal" != "(journal unavailable)" ]; then
            echo "    last journal lines:"
            printf '%s\n' "$journal" | sed 's/^/      /'
        fi
        ;;
    Darwin)
        local plist_label
        plist_label=$(bot_conf_get "$bot_dir_diag" BOT_SERVICE "$bot")
        local launchctl_info
        launchctl_info=$(launchctl print "gui/$(id -u)/$plist_label" 2>/dev/null | head -15 || echo "(launchctl info unavailable)")
        if [ "$launchctl_info" != "(launchctl info unavailable)" ]; then
            echo "    launchctl:"
            printf '%s\n' "$launchctl_info" | sed 's/^/      /'
        fi
        ;;
    esac

    # 3. Fleet-state last known state
    local state_file="${FLEET_STATE_PATH:-$CLAUDLOBBY_ROOT/state/fleet-state.json}"
    if [ -f "$state_file" ] && command -v jq >/dev/null 2>&1; then
        local state_summary
        state_summary=$(jq -r --arg b "$bot" '
            .bots[$b] // empty |
            if . then "status=\(.status // "unknown"), task=\(.current_task // "none")\(
                if .last_completed then "\n    last completed: \(.last_completed)" else "" end
            )" else empty end
        ' "$state_file" 2>/dev/null)
        [ -n "$state_summary" ] && echo "    fleet-state: $state_summary"
    fi

    # 4. Startup log (last 5 lines)
    local startup_log="$RUNTIME_DIR/$bot/logs/startup.log"
    if [ -f "$startup_log" ]; then
        echo "    startup.log (last 5):"
        tail -5 "$startup_log" | sed 's/^/      /'
    fi
}

if [ -n "${missing// /}" ]; then
    echo
    echo "Diagnostics for missing bots:"
    for b in $missing; do
        _diagnose_missing_bot "$b"
    done
fi
# --- end root-cause diagnostics ----------------------------------------------

# Prune fleet-state entries for bots no longer in fleet.yaml
if [ -x "$LIB_DIR/fleet-state-update.sh" ]; then
    "$LIB_DIR/fleet-state-update.sh" prune "$FLEET_YAML" || true
fi

# NPX cache health check
if [ -x "$LIB_DIR/check-npx-cache.sh" ]; then
    echo
    "$LIB_DIR/check-npx-cache.sh" --fleet "$FLEET" || true
fi

# Enroll orphans if requested
if [ "$ENROLL" = "--enroll" ] && [ -n "${orphan// /}" ]; then
    echo
    echo "Enrolling orphans via spin-up-bot.sh..."
    for b in $orphan; do
        bot_dir="$RUNTIME_DIR/$b"
        if [ -d "$bot_dir" ]; then
            echo "→ $b"
            _osock=$(tmux_socket_for_bot "$bot_dir" 2>/dev/null || true)
            bot_tmux "$_osock" kill-session -t "$b" 2>/dev/null || true
            "$LIB_DIR/spin-up-bot.sh" "$bot_dir"
        else
            echo "→ $b SKIPPED (no runtime dir at $bot_dir; run 'claudlobby generate' first)"
        fi
    done
fi
