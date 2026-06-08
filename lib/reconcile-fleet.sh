#!/bin/bash
# Reconcile a fleet's runtime state: tmux sessions ↔ host service units ↔ fleet.yaml.
#
# Reports four buckets:
#   ✓ healthy      — defined in fleet.yaml + has unit + has tmux session
#   ⚠ orphan       — defined in fleet.yaml + has tmux session but NO unit (unsupervised)
#   ⚠ missing      — defined in fleet.yaml + has unit but NO tmux session (down)
#   🚨 unbound     — tmux session named like a bot but NOT in any fleet.yaml (rogue)
#
# Usage: reconcile-fleet.sh <fleet-name> [--enroll]
#   --enroll : auto-enroll orphans by calling spin-up-bot.sh on each
set -euo pipefail

FLEET="${1:?Usage: reconcile-fleet.sh <fleet-name> [--enroll]}"
ENROLL="${2:-}"

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
FLEET_YAML="$CLAUDLOBBY_ROOT/local/$FLEET/fleet.yaml"
RUNTIME_DIR=$(resolve_bots_dir "$FLEET")

if [ ! -f "$FLEET_YAML" ]; then
    echo "reconcile-fleet: $FLEET_YAML not found" >&2
    exit 1
fi

# Helper: extract bot names from a fleet.yaml using awk.
# Assumes claudlobby's documented schema: top-level `fleet:` with 2-space
# child indent, `bots:` section, bot keys at 4-space indent. Bot names are
# lowercase identifiers (matches new-bot scaffold convention).
parse_bots() {
    awk '
        /^  bots:[ \t]*$/ {in_bots=1; next}
        in_bots && /^    [a-zA-Z_][a-zA-Z0-9_-]*:[ \t]*$/ {
            gsub(/[ \t:]/, "", $0); print
        }
        in_bots && /^  [a-zA-Z_]/ && !/^    / {in_bots=0}
    ' "$1"
}

# Helper: extract fleet.service_prefix (used on Darwin to filter plists).
parse_service_prefix() {
    awk '
        /^  service_prefix:/ {
            sub(/^  service_prefix:[ \t]*/, "")
            gsub(/["'\'']/, "")
            print; exit
        }
    ' "$1"
}

# 1. Bots defined in this fleet's yaml (top-level keys under bots:)
defined=$(parse_bots "$FLEET_YAML" | sort -u)

# 2. tmux sessions on this host
tmux_sessions=$(tmux ls 2>/dev/null | awk -F: '{print $1}' | sort -u || true)

# 3. systemd-user unit files (Linux) OR launchd plists (macOS)
units=""
case "$_OS" in
Linux)
    units=$(ls "$HOME/.config/systemd/user/" 2>/dev/null \
        | grep -v '^claudlobby-' \
        | sed 's/\.service$//' \
        | sort -u || true)
    ;;
Darwin)
    # Filter to plists matching this fleet's service_prefix; without this,
    # any plist whose last dot-segment happens to share a bot's name
    # (e.g. com.spotify.helper → "helper") would falsely match.
    SERVICE_PREFIX=$(parse_service_prefix "$FLEET_YAML")
    if [ -z "$SERVICE_PREFIX" ]; then
        echo "reconcile-fleet: fleet.service_prefix missing in $FLEET_YAML" >&2
        exit 1
    fi
    units=$(ls "$HOME/Library/LaunchAgents/" 2>/dev/null \
        | grep "^${SERVICE_PREFIX}\." \
        | sed -e "s|^${SERVICE_PREFIX}\.||" -e 's|\.plist$||' \
        | sort -u || true)
    ;;
esac

# Build buckets
healthy=""; orphan=""; missing=""; unbound=""

while IFS= read -r b; do
    [ -z "$b" ] && continue
    has_tmux=0; has_unit=0
    echo "$tmux_sessions" | grep -qx "$b" && has_tmux=1
    echo "$units"         | grep -qx "$b" && has_unit=1
    if   [ $has_tmux = 1 ] && [ $has_unit = 1 ]; then healthy="$healthy $b"
    elif [ $has_tmux = 1 ] && [ $has_unit = 0 ]; then orphan="$orphan $b"
    elif [ $has_tmux = 0 ] && [ $has_unit = 1 ]; then missing="$missing $b"
    fi
done <<< "$defined"

# Unbound: tmux sessions whose name matches no defined bot in ANY fleet.yaml
all_defined=$(for fy in "$CLAUDLOBBY_ROOT"/local/*/fleet.yaml; do
    [ -f "$fy" ] && parse_bots "$fy"
done | sort -u)
while IFS= read -r s; do
    [ -z "$s" ] && continue
    echo "$all_defined" | grep -qx "$s" || unbound="$unbound $s"
done <<< "$tmux_sessions"

# Report
echo "Fleet: $FLEET"
echo "  ✓ healthy:  ${healthy:-(none)}"
echo "  ⚠ orphan:   ${orphan:-(none)}"
echo "  ⚠ missing:  ${missing:-(none)}"
echo "  🚨 unbound: ${unbound:-(none)}   ← if non-empty, investigate before killing"

# --- Root-cause diagnostics for missing bots ---------------------------------
_DIAG_SERVICE_PREFIX=$(parse_service_prefix "$FLEET_YAML")
_diagnose_missing_bot() {
    local bot="$1"

    echo "  ── $bot ──"

    # 1. Service unit status
    case "$_OS" in
    Linux)
        local unit_name="${bot}.service"
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
        local plist_label="${_DIAG_SERVICE_PREFIX}.${bot}"
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
        [ -z "$b" ] && continue
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
    while IFS= read -r b; do
        [ -z "$b" ] && continue
        bot_dir="$RUNTIME_DIR/$b"
        if [ -d "$bot_dir" ]; then
            echo "→ $b"
            "$_TMUX_BIN" kill-session -t "$b" 2>/dev/null || true
            "$LIB_DIR/spin-up-bot.sh" "$bot_dir"
        else
            echo "→ $b SKIPPED (no runtime dir at $bot_dir; run 'claudlobby generate' first)"
        fi
    done <<< "$orphan"
fi
