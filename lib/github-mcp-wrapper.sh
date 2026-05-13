#!/bin/bash
# github-mcp-wrapper — start the GitHub MCP server with a fresh App
# installation token instead of a long-lived PAT.
#
# Flow:
#   1. Mint a `ghs_` token via lib/mint-github-token.sh (git's credential
#      helper chain → git-credential-botfarm → JWT exchange).
#   2. Export it as GITHUB_PERSONAL_ACCESS_TOKEN.
#   3. exec npx @modelcontextprotocol/server-github@<pinned-version>.
#
# The MCP server reads the env var verbatim and includes it in
# `Authorization: token <…>` headers. GitHub's API accepts ghs_ tokens
# wherever it accepts ghp_ tokens, so no MCP-server changes are needed.
#
# Lifetime: installation tokens expire after ~1 hour. If the MCP server
# runs longer than that without a restart, API calls return 401. Bot
# restarts (manual kickstart or via launchd/systemd) mint a fresh token.
# Long-running bots may want a periodic restart cron for the MCP server.
#
# Fallback: if mint-github-token.sh can't produce a token, fall back to
# the GITHUB_PAT env var (the same env contract used by the standard
# github.json fragment). Refuse to start if neither is available.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GITHUB_PERSONAL_ACCESS_TOKEN="$("$LIB_DIR/mint-github-token.sh" 2>/dev/null || true)"

if [ -z "$GITHUB_PERSONAL_ACCESS_TOKEN" ] && [ -n "${GITHUB_PAT:-}" ]; then
    GITHUB_PERSONAL_ACCESS_TOKEN="$GITHUB_PAT"
fi

if [ -z "$GITHUB_PERSONAL_ACCESS_TOKEN" ]; then
    echo "github-mcp-wrapper: no token from credential helper or GITHUB_PAT env. Configure git-credential-botfarm per documentation/runbooks/github-app-setup.md, or set GITHUB_PAT in fleet-tier .env. Refusing to start MCP server with no auth." >&2
    exit 1
fi
export GITHUB_PERSONAL_ACCESS_TOKEN

exec npx -y @modelcontextprotocol/server-github@2025.4.8 "$@"
