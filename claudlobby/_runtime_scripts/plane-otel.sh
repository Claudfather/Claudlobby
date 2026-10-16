#!/bin/bash
# Host-local OTLP/HTTP-JSON intake (#2145). Dormant until the host arms
# plane-otel.enroll. Script-line port and retention flags pass to the selected
# CLI; no Python or host-tool probe precedes exec. Raw files stay under state/otel.
set -euo pipefail
source "${BASH_SOURCE[0]%/*}/cli-context.sh"
_claudlobby_require_root
_claudlobby_require_cli
exec "$CLAUDLOBBY_CLI" --root "$CLAUDLOBBY_ROOT" plane otel-intake "$@"
