#!/bin/bash
# check-npx-cache.sh — verify the on-demand package caches hold what the
# fleet's MCP servers need. Covers BOTH runtimes: npx (~/.npm/_npx) and uvx
# (uv cache + installed uv tools). The filename still says npx; it is
# referenced by reload-fleet, reconcile-fleet, doctor.py, two test modules and
# a composed skill, so the rename is deliberately deferred rather than bundled
# into #1577 (see that issue).
#
# These caches are load-bearing infrastructure. Without them MCP server
# startup goes from ~1.5s to a download that can exceed Claude Code's 30s
# connect budget; with 8 bots sharing packages, a cold cache on restart causes
# catastrophic IO contention on SD card hardware.
#
# Usage: check-npx-cache.sh [--fleet <name>]
#   Scans the fleet MCP fragments for npx- and uvx-fetched packages and
#   verifies each is resolvable without a download.
#   Exit 0 all resolvable, 1 some missing (listed), 2 the probe could not
#   answer (the shared grammar is unreachable) — never 0 for "cannot tell".
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

# Extract every warmable package across base + overlay in ONE call to the
# shared grammar (lib/mcp-package-grammar.py), which is also what warm-cache
# and the composer read. This used to be an embedded python3 heredoc spawned
# once PER FRAGMENT, and it was the only copy of the grammar that did not know
# uvx existed -- so a uv MCP server was invisible here, this probe passed, and
# reload-fleet never invoked the warm that would have fetched it (#1577).
# Rows are <runtime>\t<display spec>\t<bare name>.
# Unreachable is NOT empty. Discarding this failure would collapse "cannot
# read the grammar" into "the library has no packages", exit 0, and let
# reload-fleet call debounce_clear -- #1577 rebuilt one layer up, at the seam
# built to close it. Exit 2 says the probe could not answer (source_state.py
# rule); reload-fleet treats any nonzero as warm, which is the safe direction.
if ! TARGETS=$(python3 "$LIB_DIR/mcp-package-grammar.py" --probe-targets "${MCP_DIRS[@]}" 2>&1); then
    echo "check-npx-cache: cannot reach the package grammar at $LIB_DIR/mcp-package-grammar.py" >&2
    printf '%s\n' "$TARGETS" >&2
    exit 2
fi

if [ -z "$TARGETS" ]; then
    echo "check-npx-cache: no npx- or uvx-based packages found in MCP fragments"
    exit 0
fi

MISSING=()
GLOBAL=()
TOOLED=()
TOTAL=0

# Resolved on the FIRST cache miss, not up front: reconcile-fleet runs this
# check on every pass and the normal state is everything cached, so an eager
# `npm root -g` would spawn npm on the hot path for an answer nobody reads.
# Empty when npm is absent, which disables the global probe rather than
# failing the check. Called bare, never as a command substitution -- that runs
# in a subshell and would discard the memo, making it resolve once per package.
_global_root=""
_global_root_done=0
_resolve_global_root() {
    [ "$_global_root_done" -eq 1 ] && return 0
    _global_root_done=1
    _global_root="${NPM_GLOBAL_ROOT:-$(npm root -g 2>/dev/null || true)}"
}

# Same lazy-memo shape for uv, for the same reason.
_uv_cache=""
_uv_cache_done=0
_resolve_uv_cache() {
    [ "$_uv_cache_done" -eq 1 ] && return 0
    _uv_cache_done=1
    _uv_cache="${UV_CACHE_DIR:-$(uv cache dir 2>/dev/null || true)}"
}
# The INSTALLED TOOLS, asked of uv rather than read off its directory layout
# -- consume by contract, never by assertion. Resolved only on a cache miss,
# so the normal state (everything cached) forks uv exactly once.
_uv_tool_names=""
_uv_tools_done=0
_resolve_uv_tools() {
    [ "$_uv_tools_done" -eq 1 ] && return 0
    _uv_tools_done=1
    _uv_tool_names="$(uv tool list 2>/dev/null | awk '{print $1}' || true)"
}

# Is a distribution present in uv's cache?
#
# THIS IS NOT A TRANSLITERATION OF THE npx PROBE, because uv does not lay its
# cache out the way npx does -- there is no per-package directory holding an
# installed tree. What uv keys by name is the DISTRIBUTION it fetched.
#
# All THREE roots are checked, and the third was found by measuring rather than
# by reading: a package built from an sdist lands in built-wheels-v*/pypi/<n>,
# which wheels-v* does NOT match, and sdists-v*/ on this host holds only
# editable/ -- so a wheel-only probe would report such a package MISSING
# forever while uvx runs it, the #852 unsatisfiable warning by another route.
# Verified on uv 0.11.3: the three shipped uvx packages match wheels-v*, the
# sdist-built `daff` matches only built-wheels-v*, two never-fetched controls
# match nothing.
#
# The version component is globbed rather than pinned because uv version-stamps
# these names (wheels-v1 and wheels-v6 coexist on this host, as do
# simple-v9/v20/v21); hardcoding one is a probe that silently stops finding
# anything after a uv upgrade.
_uv_cached() {
    local n="$1" d
    for d in "$_uv_cache"/wheels-v*/pypi/"$n" \
             "$_uv_cache"/built-wheels-v*/pypi/"$n" \
             "$_uv_cache"/sdists-v*/pypi/"$n"; do
        [ -e "$d" ] && return 0
    done
    return 1
}

while IFS=$'\t' read -r rt spec bare; do
    [ -n "$rt" ] || continue
    TOTAL=$((TOTAL + 1))
    found=0

    if [ "$rt" = "npx" ]; then
        # npx caches in content-addressed dirs, so search node_modules for the
        # package dir. Scoped (@org/name) and unscoped lay out differently.
        if [ -d "$NPX_CACHE" ]; then
            case "$bare" in @*)
                if find "$NPX_CACHE" -path "*node_modules/$bare/package.json" 2>/dev/null | head -1 | grep -q .; then
                    found=1
                fi ;;
            *)
                if find "$NPX_CACHE" -path "*/.bin/$bare" -o -path "*/node_modules/$bare/package.json" 2>/dev/null | head -1 | grep -q .; then
                    found=1
                fi ;;
            esac
        fi
        # The npx cache is not the only place a package can already be
        # resolvable, and cache residency is a proxy for the question the
        # caller actually has: will `npx <pkg>` run without a download? A
        # globally installed package answers yes -- npx resolves it and so
        # never populates _npx for it -- which is why probing only the cache
        # reported it MISSING forever, with no amount of warm-cache able to
        # create the entry it waited for (#852). Global installs sit flat under
        # `npm root -g`, not nested under node_modules, so this is a direct
        # test rather than a find.
        if [ $found -eq 0 ]; then
            _resolve_global_root
            if [ -n "$_global_root" ] && [ -f "$_global_root/$bare/package.json" ]; then
                GLOBAL+=("$spec")
                found=1
            fi
        fi
    elif [ "$rt" = "uvx" ]; then
        # Cache first, tool list only on a miss. Measured: `uv tool install`
        # ALSO populates the wheel cache, so uv has no permanently
        # unsatisfiable state of npm's kind and the tool door is not the
        # common case -- checking it first would fork uv on every pass for an
        # answer the cache almost always gives, which is exactly what the npx
        # path deliberately avoids 20 lines above. The fallback still EXISTS,
        # which is what protects the divergent cases (a tool installed from a
        # path or VCS, or one whose cache entry was pruned); only the order
        # changed, and a package that is both simply reports as cached.
        _resolve_uv_cache
        if [ -n "$_uv_cache" ] && _uv_cached "$bare"; then
            found=1
        fi
        if [ $found -eq 0 ]; then
            _resolve_uv_tools
            if printf '%s\n' "$_uv_tool_names" | grep -qx -- "$bare"; then
                TOOLED+=("$spec")
                found=1
            fi
        fi
    fi

    [ $found -eq 0 ] && MISSING+=("$rt:$spec")
done <<< "$TARGETS"

# What this probe still cannot see, stated so a future reader does not mistake
# a pass for more than it is. Both runtimes: the version is not checked, so a
# package present at the WRONG version reads as present, and a cache entry that
# exists but is corrupt or partial reads as present. npx: resolvability via a
# project-local node_modules, or via an npm prefix other than the one
# `npm root -g` reports, is invisible. uv: the probe finds the package's OWN
# distribution, never its dependency closure, so a package whose wheel is
# cached while a dependency is not still downloads on first run. It answers
# "is this resolvable without a download, here", not "will this run".
_report_extra() {
    for _g in ${GLOBAL[@]+"${GLOBAL[@]}"}; do
        echo "  - $_g (global npm install — npx resolves it; it will never populate the npx cache)"
    done
    for _t in ${TOOLED[@]+"${TOOLED[@]}"}; do
        echo "  - $_t (uv tool install — uvx resolves it from the tool dir)"
    done
}
_extra_n=$(( ${#GLOBAL[@]} + ${#TOOLED[@]} ))
if [ ${#MISSING[@]} -eq 0 ]; then
    echo "check-npx-cache: all $TOTAL packages resolvable ✓ ($(( TOTAL - _extra_n )) cached, $_extra_n preinstalled)"
    _report_extra
    exit 0
else
    echo "check-npx-cache: ${#MISSING[@]}/$TOTAL packages MISSING (not cached, not preinstalled):"
    for pkg in "${MISSING[@]}"; do
        echo "  - $pkg"
    done
    _report_extra
    echo ""
    echo "  Fix: claudlobby warm-cache"
    exit 1
fi
