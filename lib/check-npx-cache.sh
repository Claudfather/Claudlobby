#!/bin/bash
# check-npx-cache.sh — verify npx cache contains expected MCP packages.
#
# The npx cache (~/.npm/_npx/) is load-bearing infrastructure. Without it,
# MCP server startup goes from ~1.5s to 30-60s per package (download + install).
# With 8 bots sharing the same packages, a cold cache on restart causes
# catastrophic IO contention on SD card hardware.
#
# Usage: check-npx-cache.sh [--fleet <name>]
#   Scans fleet's MCP fragments for npx packages and verifies each is cached.
#   Exit 0 if all cached, exit 1 if any missing (prints missing list).
#
# Designed to be called from reconcile-fleet.sh or as a standalone health check.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$HOME/claudlobby}"
NPX_CACHE="${NPX_CACHE_DIR:-$HOME/.npm/_npx}"

# Parse --fleet arg
FLEET=""
while [ $# -gt 0 ]; do
    case "$1" in
        --fleet) FLEET="${2:-}"; shift 2 ;;
        *) shift ;;
    esac
done

# Scan the shared library — the canonical source of npx package names —
# plus the fleet's local overlay (local/<fleet>/library/mcp/), which can add
# or override fragments with packages the base library doesn't know about.
MCP_DIR="$CLAUDLOBBY_ROOT/library/mcp"

if [ ! -d "$MCP_DIR" ]; then
    echo "check-npx-cache: MCP library not found at $MCP_DIR" >&2
    exit 2
fi

MCP_DIRS=("$MCP_DIR")
if [ -n "$FLEET" ]; then
    _fleet_dir=$(resolve_fleet_dir "$FLEET") || _fleet_dir="$CLAUDLOBBY_ROOT/local/$FLEET"
    FLEET_MCP_DIR="$_fleet_dir/library/mcp"
    [ -d "$FLEET_MCP_DIR" ] && MCP_DIRS+=("$FLEET_MCP_DIR")
fi

# Extract npx packages from MCP fragments (base + overlay, deduped by name)
PACKAGES=()
for dir in "${MCP_DIRS[@]}"; do
    for frag in "$dir"/*.json; do
        [ -f "$frag" ] || continue
        # Extract package names from "args": ["-y", "<package>", ...] patterns
        pkg=$(python3 - "$frag" <<'PYEOF'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    for k, v in d.items():
        if k.startswith('_') or not isinstance(v, dict):
            continue
        if v.get('command') == 'npx' and 'args' in v:
            args = v['args']
            for i, a in enumerate(args):
                if a == '-y' and i + 1 < len(args):
                    print(args[i+1])
                    break
except Exception:
    pass
PYEOF
        )
        if [ -n "$pkg" ]; then
            already=0
            for existing in "${PACKAGES[@]+"${PACKAGES[@]}"}"; do
                [ "$existing" = "$pkg" ] && already=1 && break
            done
            [ $already -eq 0 ] && PACKAGES+=("$pkg")
        fi
    done
done

if [ ${#PACKAGES[@]} -eq 0 ]; then
    echo "check-npx-cache: no npx packages found in MCP fragments"
    exit 0
fi

# Classify each package into one of three states rather than the two the old
# probe could express. "Absent from the cache" had been collapsed into MISSING,
# which is what made the warning unsatisfiable: a globally installed package
# reported missing has no remedy, so the operator's only correct response was to
# ignore the check.
MISSING=()
GLOBAL=()

# Resolved on the FIRST cache miss, not up front: reconcile-fleet runs this check
# on every pass and the normal state is "everything cached", so an eager
# `npm root -g` would spawn npm on the hot path for an answer nobody reads.
# Empty when npm is absent, which disables the global probe rather than failing
# the check. Called bare, never as `$(...)` -- a command substitution runs in a
# subshell and would discard the memo, making it resolve once per package.
_global_root=""
_global_root_done=0
_resolve_global_root() {
    [ "$_global_root_done" -eq 1 ] && return 0
    _global_root_done=1
    _global_root="${NPM_GLOBAL_ROOT:-$(npm root -g 2>/dev/null || true)}"
}

# One description of a global install, used by both outcomes below.
_report_global() {
    for _g in ${GLOBAL[@]+"${GLOBAL[@]}"}; do
        echo "  - $_g (global install — npx resolves it; it will never populate the npx cache)"
    done
}

for pkg in "${PACKAGES[@]}"; do
    # npx caches in content-addressed dirs. Strategy:
    # Strip version suffix, then search node_modules for the package dir.
    # e.g. "@modelcontextprotocol/server-github@2025.4.8" → look for
    # node_modules/@modelcontextprotocol/server-github/
    # Strip version/tag suffix: @1.2.3, @latest, @^2.0.0, etc.
    pkg_bare=$(printf '%s' "$pkg" | sed -E 's/@[0-9^~><=][^/]*$//; s/@latest$//')
    found=0
    if [ -d "$NPX_CACHE" ]; then
        # For scoped packages (@org/name), look for the dir structure
        # For unscoped (name), look for node_modules/name/ or .bin/name
        if printf '%s' "$pkg_bare" | grep -q "^@"; then
            # Scoped: look for node_modules/@org/name/package.json
            if find "$NPX_CACHE" -path "*node_modules/$pkg_bare/package.json" 2>/dev/null | head -1 | grep -q .; then
                found=1
            fi
        else
            # Unscoped: look for .bin/<name> or node_modules/<name>/
            if find "$NPX_CACHE" -path "*/.bin/$pkg_bare" -o -path "*/node_modules/$pkg_bare/package.json" 2>/dev/null | head -1 | grep -q .; then
                found=1
            fi
        fi
    fi
    # The npx cache is not the only place a package can already be resolvable,
    # and cache residency is a proxy for the question the caller actually has:
    # "will `npx <pkg>` run without a download?". A globally installed package
    # answers yes -- npx resolves it and so never populates _npx for it -- which
    # is why probing only the cache reported it MISSING forever, with no amount
    # of warm-cache able to create the entry it waited for (#852).
    #
    # Global installs sit flat under `npm root -g` (<root>/<name>/package.json,
    # <root>/@org/name/package.json), not nested under node_modules/ the way the
    # npx cache lays them out -- so this is a direct test, not a find.
    if [ $found -eq 0 ]; then
        _resolve_global_root
        if [ -n "$_global_root" ] && [ -f "$_global_root/$pkg_bare/package.json" ]; then
            GLOBAL+=("$pkg")
            found=1
        fi
    fi
    if [ $found -eq 0 ]; then
        MISSING+=("$pkg")
    fi
done

# What this probe still cannot see, stated so a future reader does not mistake a
# pass for more than it is: the version suffix is stripped before matching, so a
# package present at the WRONG version reads as present; a cache entry that
# exists but is corrupt or partial reads as present; and resolvability via a
# project-local node_modules, or via an npm prefix other than the one
# `npm root -g` reports, is invisible. It answers "is this resolvable without a
# download, here", not "will this run".
_global_n=${#GLOBAL[@]}
if [ ${#MISSING[@]} -eq 0 ]; then
    echo "check-npx-cache: all ${#PACKAGES[@]} packages resolvable ✓ ($(( ${#PACKAGES[@]} - _global_n )) cached, $_global_n global)"
    echo "  cache size: $(du -sh "$NPX_CACHE" 2>/dev/null | cut -f1)"
    _report_global
    exit 0
else
    echo "check-npx-cache: ${#MISSING[@]}/${#PACKAGES[@]} packages MISSING (not cached, not installed globally):"
    for pkg in "${MISSING[@]}"; do
        echo "  - $pkg"
    done
    _report_global
    echo ""
    echo "  Fix: claudlobby warm-cache (or: npx -y <pkg> --help)"
    exit 1
fi
