#!/bin/bash
# Install the credential keepalive as a systemd user timer (Linux).
#
# Thin caller of install_fleet_timer.sh — all enrollment logic lives there.
# On macOS: install_fleet_timer_launchd.sh creds-check. Run `claudlobby generate` first.
#
# Usage: install-creds-check-systemd.sh [<fleet-name>]
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$LIB_DIR/install_fleet_timer.sh" creds-check "$@"
