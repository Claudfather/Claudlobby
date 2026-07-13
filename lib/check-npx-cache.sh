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

# Check cache for each package
MISSING=()
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
    if [ $found -eq 0 ]; then
        MISSING+=("$pkg")
    fi
done

if [ ${#MISSING[@]} -eq 0 ]; then
    echo "check-npx-cache: all ${#PACKAGES[@]} packages cached ✓"
    echo "  cache size: $(du -sh "$NPX_CACHE" 2>/dev/null | cut -f1)"
    exit 0
else
    echo "check-npx-cache: ${#MISSING[@]}/${#PACKAGES[@]} packages MISSING from cache:"
    for pkg in "${MISSING[@]}"; do
        echo "  - $pkg"
    done
    echo ""
    echo "  Fix: claudlobby warm-cache (or: npx -y <pkg> --help)"
    exit 1
fi
