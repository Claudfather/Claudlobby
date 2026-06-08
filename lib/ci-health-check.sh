#!/bin/bash
# ci-health-check.sh — check if a repo's default branch CI is healthy.
#
# Queries GitHub Actions for recent workflow runs on the target branch.
# Deduplicates by workflow name and checks the latest run per workflow.
# Returns exit 0 if all workflows pass (or none exist), exit 1 if any
# workflow is failing. Workers call this before pushing a PR to know
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

# Fetch recent completed runs — per_page=20 covers multi-workflow repos.
# Deduplicate by workflow name, keeping only the latest run per workflow.
_raw=$(gh api "repos/$REPO/actions/runs?branch=$BRANCH&status=completed&per_page=20" 2>/dev/null) || true

if [ -z "$_raw" ]; then
    [ "$QUIET" -eq 0 ] && echo "ci-health-check: GitHub API error for $REPO@$BRANCH"
    exit 2
fi

# Latest run per workflow: group by .name, pick first (most recent) from each.
_latest=$(printf '%s' "$_raw" | jq -c '
    [.workflow_runs[] | {conclusion, name, html_url, updated_at}]
    | group_by(.name)
    | map(.[0])
') || true

if [ -z "$_latest" ] || [ "$_latest" = "[]" ] || [ "$_latest" = "null" ]; then
    [ "$QUIET" -eq 0 ] && echo "ci-health-check: no completed runs found on $REPO@$BRANCH (no CI configured?)"
    exit 0
fi

# Check for any failing workflow.
_failing=$(printf '%s' "$_latest" | jq -c '[.[] | select(.conclusion != "success")]')
_fail_count=$(printf '%s' "$_failing" | jq 'length')

if [ "$_fail_count" -eq 0 ]; then
    _count=$(printf '%s' "$_latest" | jq 'length')
    [ "$QUIET" -eq 0 ] && echo "ci-health-check: $REPO@$BRANCH CI healthy — $_count workflow(s) passing"
    exit 0
else
    if [ "$QUIET" -eq 0 ]; then
        echo "ci-health-check: $REPO@$BRANCH CI FAILING — $_fail_count workflow(s):"
        printf '%s' "$_failing" | jq -r '.[] | "  \(.name): \(.conclusion) — \(.html_url)"'
    fi
    exit 1
fi
