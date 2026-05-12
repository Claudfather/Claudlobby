#!/bin/bash
# Legacy entry point — delegates to the cross-platform setup-host.sh.
exec "$(dirname "$0")/setup-host.sh" "$@"
