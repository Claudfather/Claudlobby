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

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$HOME/claudlobby}"
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
FLEET_YAML="$CLAUDLOBBY_ROOT/local/$FLEET/fleet.yaml"
RUNTIME_DIR="$CLAUDLOBBY_ROOT/local/$FLEET/runtime/bots"

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
case "$(uname)" in
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

for b in $defined; do
    has_tmux=0; has_unit=0
    echo "$tmux_sessions" | grep -qx "$b" && has_tmux=1
    echo "$units"         | grep -qx "$b" && has_unit=1
    if   [ $has_tmux = 1 ] && [ $has_unit = 1 ]; then healthy="$healthy $b"
    elif [ $has_tmux = 1 ] && [ $has_unit = 0 ]; then orphan="$orphan $b"
    elif [ $has_tmux = 0 ] && [ $has_unit = 1 ]; then missing="$missing $b"
    fi
done

# Unbound: tmux sessions whose name matches no defined bot in ANY fleet.yaml
all_defined=$(for fy in "$CLAUDLOBBY_ROOT"/local/*/fleet.yaml; do
    [ -f "$fy" ] && parse_bots "$fy"
done | sort -u)
for s in $tmux_sessions; do
    echo "$all_defined" | grep -qx "$s" || unbound="$unbound $s"
done

# Report
echo "Fleet: $FLEET"
echo "  ✓ healthy:  ${healthy:-(none)}"
echo "  ⚠ orphan:   ${orphan:-(none)}"
echo "  ⚠ missing:  ${missing:-(none)}"
echo "  🚨 unbound: ${unbound:-(none)}   ← if non-empty, investigate before killing"

# Enroll orphans if requested
if [ "$ENROLL" = "--enroll" ] && [ -n "${orphan// /}" ]; then
    echo
    echo "Enrolling orphans via spin-up-bot.sh..."
    for b in $orphan; do
        bot_dir="$RUNTIME_DIR/$b"
        if [ -d "$bot_dir" ]; then
            echo "→ $b"
            tmux kill-session -t "$b" 2>/dev/null || true
            "$LIB_DIR/spin-up-bot.sh" "$bot_dir"
        else
            echo "→ $b SKIPPED (no runtime dir at $bot_dir; run 'claudlobby generate' first)"
        fi
    done
fi
