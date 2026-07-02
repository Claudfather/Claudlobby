#!/bin/bash
# Legacy entry point — delegates to the cross-platform setup-system.
exec "$(dirname "$0")/setup-system" "$@"
