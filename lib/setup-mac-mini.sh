#!/bin/bash
# Legacy macOS setup script — now a thin wrapper around setup-host.sh.
# setup-host.sh handles both macOS and Linux. See it for full docs.
exec "$(dirname "$0")/setup-host.sh" "$@"
