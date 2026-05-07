#!/bin/bash
# Bulk install composed bots as systemd user services (Linux).
#
# Usage:
#   setup-linux.sh --fleet <fleet-name> --enroll-all
#   setup-linux.sh --fleet <fleet-name> --bot <bot-name>
#
# Prerequisites:
#   - `claudlobby generate --fleet <fleet>` has been run
#   - Runtime bots exist at local/<fleet>/runtime/bots/
#
# This script:
#   1. Ensures loginctl enable-linger is set for the current user
#   2. Iterates bots in runtime/bots/ (or a single --bot)
#   3. Calls install-bot-systemd.sh for each
#   4. Reports summary
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

FLEET=""
BOT=""
ENROLL_ALL=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --fleet) FLEET="$2"; shift 2 ;;
        --bot) BOT="$2"; shift 2 ;;
        --enroll-all) ENROLL_ALL=true; shift ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

if [ -z "$FLEET" ]; then
    echo "Usage: setup-linux.sh --fleet <fleet-name> [--enroll-all | --bot <name>]" >&2
    exit 1
fi

FLEET_DIR="$REPO_ROOT/local/$FLEET"
BOTS_DIR="$FLEET_DIR/runtime/bots"

if [ ! -d "$BOTS_DIR" ]; then
    echo "No bots at $BOTS_DIR — run 'claudlobby --fleet $FLEET generate' first." >&2
    exit 1
fi

# Step 1: ensure linger
if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
    echo "Enabling loginctl linger for $USER (requires sudo)..."
    sudo loginctl enable-linger "$USER"
    echo "  linger enabled."
else
    echo "  linger already enabled."
fi

# Step 2: install bots
INSTALLED=0
FAILED=0

install_bot() {
    local bot_dir="$1"
    local bot_name
    bot_name="$(basename "$bot_dir")"

    if [ ! -f "$bot_dir/bot.conf" ]; then
        echo "  SKIP $bot_name — no bot.conf (not composed?)"
        return
    fi

    echo "  installing $bot_name..."
    if "$SCRIPT_DIR/install-bot-systemd.sh" "$bot_dir"; then
        INSTALLED=$((INSTALLED + 1))
    else
        echo "  FAILED: $bot_name"
        FAILED=$((FAILED + 1))
    fi
}

if [ -n "$BOT" ]; then
    # Single bot
    if [ ! -d "$BOTS_DIR/$BOT" ]; then
        echo "Bot '$BOT' not found in $BOTS_DIR" >&2
        exit 1
    fi
    install_bot "$BOTS_DIR/$BOT"
elif [ "$ENROLL_ALL" = true ]; then
    # All bots
    for bot_dir in "$BOTS_DIR"/*/; do
        [ -d "$bot_dir" ] || continue
        install_bot "$bot_dir"
    done
else
    echo "Specify --enroll-all or --bot <name>" >&2
    exit 1
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Fleet: $FLEET"
echo "  Installed: $INSTALLED"
[ "$FAILED" -gt 0 ] && echo "  Failed: $FAILED"
echo ""
echo "  Status:  systemctl --user status <bot>"
echo "  Logs:    journalctl --user -u <bot> -f"
echo "  Restart: systemctl --user restart <bot>"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
