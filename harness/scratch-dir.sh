#!/usr/bin/env bash
# scratch-dir.sh — sourced by harness instruments: where a harness script makes
# its throwaway directory, and how it removes one.
#
# Why this exists. rehearse-held-push.sh and rehearse-debounce-recipient.sh made
# their root with
#     ROOT="$(mktemp -d)"
#     ROOT="$(cd "$ROOT" && pwd -P)"
# under `set -uo pipefail`, with no -e. On a full disk mktemp fails and prints
# nothing; `cd ""` succeeds in bash without moving; so ROOT became the directory
# the script was started in, and the EXIT trap ran `rm -rf "$ROOT"` on it. On
# 2026-11-21 a test run started from a checkout lost the whole checkout that
# way. Started from a home directory, it would have removed that.
#
# Two rules follow. A directory that could not be made stops the script before
# anything is removed. And a removal says what it will never take, rather than
# trusting the variable it was handed.
#
#   scratch_dir VAR [TEMPLATE]
#       Make a directory with `mktemp -d [TEMPLATE]` and put its physical path
#       in VAR. When mktemp fails, or what it printed is not a new directory
#       this script may remove, say why and exit 3. Call it as a plain command,
#       never inside $( ): it has to be able to end the script.
#   scratch_remove PATH...
#       `rm -rf` each PATH that scratch_refusal passes. A refused path is named
#       on stderr and makes the status 1; the other paths are still removed. A
#       path that does not exist is passed over.
#   scratch_refusal PATH
#       Print why PATH must not be removed, or nothing. It removes nothing.
#       Refused: an empty path, a relative one, the filesystem root, and a
#       directory that is or holds the current directory, the directory this
#       script was started in, the checkout this file is in, or the home
#       directory.
#
# Sourcing sets no shell option and no trap, so it is safe under a caller with
# or without -e. bash 3.2 (the macOS /bin/bash) runs it.
#
# Tests: tests/test_harness_scratch_dir.py.

# The physical path of an existing directory, or status 1. The empty test comes
# first because `cd ""` succeeds without moving, which is the defect above.
_scratch_physical () {
    local physical
    [ -n "${1:-}" ] || return 1
    [ -d "$1" ] || return 1
    physical="$(cd -P -- "$1" 2>/dev/null && pwd -P)" || return 1
    # bash keeps a leading "//" as a root of its own: `cd // && pwd -P` prints
    # "//", which would not compare equal to "/".
    while [ "${physical#//}" != "$physical" ]; do physical="${physical#/}"; done
    [ -n "$physical" ] || return 1
    printf '%s\n' "$physical"
}

# Read once, when sourced: a later cd must not hide where the script started.
_SCRATCH_STARTED_IN="$(_scratch_physical .)" || _SCRATCH_STARTED_IN=""

# The checkout this file is in: the parent of harness/. An instrument is run
# from a checkout it must leave whole, wherever it was started.
_SCRATCH_CHECKOUT=""
case "${BASH_SOURCE[0]:-}" in
    */*) _SCRATCH_CHECKOUT="$(_scratch_physical "${BASH_SOURCE[0]%/*}/..")" || _SCRATCH_CHECKOUT="" ;;
    ?*)  _SCRATCH_CHECKOUT="$(_scratch_physical ..)" || _SCRATCH_CHECKOUT="" ;;
esac

# <directory> <kept>: whether kept is that directory or lies inside it.
_scratch_holds () {
    [ -n "$2" ] || return 1
    case "$2/" in
        "$1"/*) return 0 ;;
    esac
    return 1
}

scratch_refusal () {
    local path="${1:-}" physical here home
    if [ -z "$path" ]; then
        echo "the path is empty"
        return 0
    fi
    case "$path" in
        /*) ;;
        *) echo "the path is relative"; return 0 ;;
    esac
    # Not a directory: rm takes at most that one name, and it holds nothing.
    [ -d "$path" ] || return 0
    physical="$(_scratch_physical "$path")" || physical=""
    if [ -z "$physical" ]; then
        echo "its physical path cannot be read"
        return 0
    fi
    if [ "$physical" = "/" ]; then
        echo "it is the filesystem root"
        return 0
    fi
    here="$(_scratch_physical .)" || here=""
    home="$(_scratch_physical "${HOME:-}")" || home=""
    if _scratch_holds "$physical" "$here"; then
        echo "it is or holds the current directory ($here)"
    elif _scratch_holds "$physical" "$_SCRATCH_STARTED_IN"; then
        echo "it is or holds the directory this script was started in ($_SCRATCH_STARTED_IN)"
    elif _scratch_holds "$physical" "$_SCRATCH_CHECKOUT"; then
        echo "it is or holds the checkout this helper is in ($_SCRATCH_CHECKOUT)"
    elif _scratch_holds "$physical" "$home"; then
        echo "it is or holds the home directory ($home)"
    fi
    return 0
}

scratch_remove () {
    local path why status=0
    for path in "$@"; do
        why="$(scratch_refusal "$path")"
        if [ -n "$why" ]; then
            echo "scratch_remove: refusing to remove '$path': $why" >&2
            status=1
            continue
        fi
        rm -rf -- "$path" || status=1
    done
    return "$status"
}

scratch_dir () {
    local _sd_var="${1:-}" _sd_made _sd_physical _sd_why
    if [ -z "$_sd_var" ]; then
        echo "FATAL: scratch_dir needs the name of a variable. Nothing was removed." >&2
        exit 3
    fi
    if [ "$#" -ge 2 ]; then
        _sd_made="$(mktemp -d "$2")" || _sd_made=""
    else
        _sd_made="$(mktemp -d)" || _sd_made=""
    fi
    _sd_physical="$(_scratch_physical "$_sd_made")" || _sd_physical=""
    if [ -z "$_sd_physical" ]; then
        _sd_why="mktemp -d made no directory"
    else
        _sd_why="$(scratch_refusal "$_sd_physical")"
    fi
    if [ -n "$_sd_why" ]; then
        echo "FATAL: no temporary directory for this run: $_sd_why. Is the disk full? Nothing was removed." >&2
        exit 3
    fi
    printf -v "$_sd_var" '%s' "$_sd_physical"
}
