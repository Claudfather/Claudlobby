#!/bin/bash
# Install the fleet-wide keepalive as a launchd LaunchAgent (macOS).
#
# Companion to install-bot.sh. Per-bot plists have KeepAlive=false (or
# Restart=on-failure on Linux), so if a bot's tmux session dies nothing
# restarts it automatically. This agent ticks every 60s and runs
# keepalive-all.sh, which kickstarts dead sessions and nudges idle bots.
#
# Usage: install-keepalive.sh [<fleet-name>]
#   <fleet-name>: defaults to $CLAUDLOBBY_FLEET. Used to scope the
#                  LaunchAgent label and the runtime/bots/ directory the
#                  keepalive iterates.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

if [ "$_OS" != "Darwin" ]; then
    echo "install-keepalive.sh: macOS only. On Linux, install a systemd timer" >&2
    echo "  pointing at $CLAUDLOBBY_ROOT/lib/keepalive-all.sh (every 60s)." >&2
    exit 1
fi
FLEET="${1:-${CLAUDLOBBY_FLEET:-}}"
if [ -z "$FLEET" ]; then
    echo "install-keepalive.sh: pass a fleet name or set CLAUDLOBBY_FLEET" >&2
    exit 2
fi

# Derive service prefix from bot.conf (all bots share the same SERVICE_PREFIX).
if [ -z "${SERVICE_PREFIX:-}" ]; then
    _first_conf="$(find "$CLAUDLOBBY_ROOT/local/$FLEET/runtime/bots" -name bot.conf -print -quit 2>/dev/null)"
    if [ -n "$_first_conf" ]; then
        SERVICE_PREFIX="$(grep -m1 '^export SERVICE_PREFIX=' "$_first_conf" | cut -d= -f2- | tr -d "'")"
    fi
fi
if [ -z "${SERVICE_PREFIX:-}" ]; then
    echo "install-keepalive.sh: SERVICE_PREFIX not set and no bot.conf found." >&2
    echo "  Run 'claudlobby generate' first, or export SERVICE_PREFIX." >&2
    exit 2
fi

LABEL="$SERVICE_PREFIX.keepalive"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PROGRAM="$CLAUDLOBBY_ROOT/lib/keepalive-all.sh"
LOG_DIR="$CLAUDLOBBY_ROOT/lib"

if [ ! -x "$PROGRAM" ]; then
    echo "error: $PROGRAM not executable (run: chmod +x $PROGRAM)" >&2
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PROGRAM</string>
    </array>
    <!-- Tick every 60 seconds -->
    <key>StartInterval</key><integer>60</integer>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>$LOG_DIR/keepalive-agent.out.log</string>
    <key>StandardErrorPath</key><string>$LOG_DIR/keepalive-agent.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>$_HOMEBREW/bin:$_HOMEBREW/sbin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key><string>$HOME</string>
        <key>CLAUDLOBBY_ROOT</key><string>$CLAUDLOBBY_ROOT</string>
        <key>CLAUDLOBBY_FLEET</key><string>$FLEET</string>
    </dict>
</dict>
</plist>
PLIST

UID_NUM="$(id -u)"
/bin/launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
/bin/launchctl bootstrap "gui/$UID_NUM" "$PLIST"
echo "installed + loaded: $LABEL"
echo "plist: $PLIST"
echo "tail logs: tail -f $LOG_DIR/keepalive-agent.*.log"
