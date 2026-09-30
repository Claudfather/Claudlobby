#!/usr/bin/env bash
# claude-version.sh — the Claude Code version, as a standalone query door (#1772).
#
# Prints the measured version (X.Y.Z) and exits 0. When the version cannot be
# measured it prints NOTHING on stdout, says why on stderr and exits 3. There is
# no sentinel: an "unavailable", an "unknown" or a 0.0.0 on stdout is exactly
# the value this door exists to keep a consumer from recording as a version.
#
# There is no logic here. The predicate is measure_claude_version in
# lib-common.sh, the reader the update job gates on, and this file exists so a
# non-bash consumer (the compositor's registry scan, via
# claudlobby/claude_version.py) asks the runtime its own question instead of
# keeping a second copy of the answer: the env-tiers.sh pattern.
#
# Usage: claude-version.sh [<binary>]
#   With no argument: the binary the fleet launches (fleet_claude_path: CLAUDE_BIN,
#   else the staged fleet link, else `claude` on the launch PATH). CLAUDLOBBY_ROOT,
#   HOME and CLAUDE_BIN are read from the environment exactly as start-bot.sh
#   reads them.
# Exit: 0 measured · 3 could not measure (the reason on stderr) · 2 usage.
set -uo pipefail
if [ "$#" -gt 1 ]; then
    printf 'usage: claude-version.sh [<binary>]\n' >&2
    exit 2
fi
LIB_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
# lib-common arms set -e on its caller; this door reports a failure, never dies of one.
set +e
if measure_claude_version "$@"; then
    printf '%s\n' "$CLAUDE_VERSION"
    exit 0
fi
printf 'claude-version: could not measure: %s\n' "$CLAUDE_VERSION_WHY" >&2
exit 3
