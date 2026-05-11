#!/bin/bash
# Install the credential keepalive as a launchd LaunchAgent (macOS).
#
# Companion to install-keepalive.sh. Where install-keepalive.sh ticks every
# 60s to restart dead bot tmux sessions, this agent fires once a day at
# 09:00 local to ping fleet-critical credentials and alert Telegram on
# state transitions. See lib/creds-check.sh for the probe implementation.
#
# Usage: install-creds-check.sh [<fleet-name>]
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

if [ "$_OS" != "Darwin" ]; then
    echo "install-creds-check.sh: macOS only. On Linux, install a systemd timer" >&2
    echo "  with OnCalendar=*-*-* 09:00:00 pointing at $CLAUDLOBBY_ROOT/lib/creds-check.sh." >&2
    exit 1
fi
FLEET="${1:-${CLAUDLOBBY_FLEET:-}}"
if [ -z "$FLEET" ]; then
    echo "install-creds-check.sh: pass a fleet name or set CLAUDLOBBY_FLEET" >&2
    exit 2
fi

LABEL="com.claudlobby.$FLEET.creds-check"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PROGRAM="$CLAUDLOBBY_ROOT/lib/creds-check.sh"
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
    <!-- Daily at 09:00 local. Tokens don't churn on shorter horizons,
         and we already pay 24h of latency on any silent expiry. -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>9</integer>
        <key>Minute</key><integer>0</integer>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>$LOG_DIR/creds-check-agent.out.log</string>
    <key>StandardErrorPath</key><string>$LOG_DIR/creds-check-agent.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>$_HOMEBREW/bin:$_HOMEBREW/sbin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key><string>$HOME</string>
        <key>CLAUDLOBBY_ROOT</key><string>$CLAUDLOBBY_ROOT</string>
    </dict>
</dict>
</plist>
PLIST

UID_NUM="$(id -u)"
/bin/launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
/bin/launchctl bootstrap "gui/$UID_NUM" "$PLIST"
echo "installed + loaded: $LABEL"
echo "plist: $PLIST"
echo "tail logs: tail -f $LOG_DIR/creds-check.log"
echo "manual fire: $PROGRAM"
