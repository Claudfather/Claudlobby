#!/bin/bash
# install-code-audit-sweep.sh — Enroll the code-audit-sweep timer as a launchd
# LaunchAgent (macOS).
#
# Thin caller of install_fleet_timer_launchd.sh — all enrollment logic lives
# there. Run `claudlobby generate` (with a `sweep:` block in fleet.yaml) first.
#
# Usage: install-code-audit-sweep.sh [<fleet-name>]
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$LIB_DIR/install_fleet_timer_launchd.sh" code-audit-sweep "$@"
