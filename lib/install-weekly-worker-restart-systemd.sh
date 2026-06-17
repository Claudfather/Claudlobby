#!/bin/bash
# Install the weekly-worker-restart timer as a systemd user timer (Linux).
#
# Thin caller of install_fleet_timer.sh — all enrollment logic lives there.
# Run `claudlobby generate` first to produce the units.
#
# Usage: install-weekly-worker-restart-systemd.sh [<fleet-name>]
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$LIB_DIR/install_fleet_timer.sh" weekly-worker-restart "$@"
