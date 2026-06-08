#!/bin/bash
# ci-health-check.sh — check if a repo's default branch CI is healthy.
#
# Queries GitHub Actions for the latest workflow run on the target branch.
# Returns exit 0 if CI is green (or no workflows exist), exit 1 if CI is
# failing. Designed for workers to call before pushing a PR so they know
# whether red checks are pre-existing or caused by their change.
#
# Usage: ci-health-check.sh [--repo owner/repo] [--branch main] [--quiet]
#
# Options:
#   --repo OWNER/REPO   GitHub repo (default: inferred from git remote)
#   --branch BRANCH     Branch to check (default: main)
#   --quiet             Suppress human-readable output; exit code only
#
# Exit codes:
#   0  CI healthy (latest run succeeded, or no workflows configured)
#   1  CI failing (latest completed run has a non-success conclusion)
#   2  Usage error or GitHub API failure

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

REPO=""
BRANCH="main"
QUIET=0

while [ $# -gt 0 ]; do
    case "$1" in
        --repo)   REPO="$2"; shift 2 ;;
        --branch) BRANCH="$2"; shift 2 ;;
        --quiet)  QUIET=1; shift ;;
        -h|--help) show_help; exit 0 ;;
        *) echo "ci-health-check: unknown option '$1'" >&2; exit 2 ;;
    esac
done

# Infer repo from git remote if not provided
if [ -z "$REPO" ]; then
    _remote=$(git remote get-url origin 2>/dev/null || true)
    if [ -z "$_remote" ]; then
        echo "ci-health-check: not in a git repo and --repo not specified" >&2
        exit 2
    fi
    # Extract owner/repo from HTTPS or SSH remote URLs
    REPO=$(printf '%s' "$_remote" | sed -E 's#.*github\.com[:/]##; s#\.git$##')
fi

if ! command -v gh >/dev/null 2>&1; then
    echo "ci-health-check: gh CLI not found" >&2
    exit 2
fi

# Fetch the most recent completed workflow run on the target branch.
_runs=$(gh api "repos/$REPO/actions/runs?branch=$BRANCH&status=completed&per_page=1" \
    --jq '.workflow_runs[0] | {conclusion, name, html_url, updated_at}' 2>/dev/null) || true

if [ -z "$_runs" ] || [ "$_runs" = "null" ]; then
    [ "$QUIET" -eq 0 ] && echo "ci-health-check: no completed runs found on $REPO@$BRANCH (no CI configured?)"
    exit 0
fi

_conclusion=$(printf '%s' "$_runs" | jq -r '.conclusion')
_name=$(printf '%s' "$_runs" | jq -r '.name')
_url=$(printf '%s' "$_runs" | jq -r '.html_url')
_updated=$(printf '%s' "$_runs" | jq -r '.updated_at')

if [ "$_conclusion" = "success" ]; then
    [ "$QUIET" -eq 0 ] && echo "ci-health-check: $REPO@$BRANCH CI healthy — $_name passed ($_updated)"
    exit 0
else
    [ "$QUIET" -eq 0 ] && echo "ci-health-check: $REPO@$BRANCH CI FAILING — $_name: $_conclusion ($_updated)"
    [ "$QUIET" -eq 0 ] && echo "  $_url"
    exit 1
fi
