#!/bin/bash
# Install the code-audit-sweep timer as a systemd user timer (Linux).
#
# Thin caller of install_fleet_timer.sh — all enrollment logic lives there.
# Compose a `sweep:` block in fleet.yaml through staged host activation first.
#
# Usage: install-code-audit-sweep-systemd.sh [<fleet-name>]
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$LIB_DIR/install_fleet_timer.sh" code-audit-sweep "$@"
