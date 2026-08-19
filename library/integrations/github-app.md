---
title: GitHub MCP (App identity)
tool_grants:
  - "mcp__github-app__*"
---

# GitHub MCP (App identity)

Wire config: `library/mcp/github-app.json` — the GitHub MCP server authenticated as the
fleet's **GitHub App** with ~1h installation tokens minted at use time, instead of a
long-lived `${GITHUB_PAT}`. Reference via `mcp: [github-app]`; tools are
`mcp__github-app__*`. The token refresh wrapper (`lib/github-app-mcp-wrapper.py`)
re-mints and respawns the server every ~50 minutes — token expiry never requires a bot
restart, and in-flight MCP requests fail for ~2s during a respawn (retry on transient
MCP errors).

Setup: run `lib/setup-github-app.sh` once per host (validates the App credentials end
to end and prints the fleet `.env` names), then set `GITHUB_APP_ID`,
`GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY_PATH` in the fleet `.env`.

#### Short-lived tokens outside the MCP

Skills and shell calls that need the App identity mint per call — never a boot-time
export, the token dies in about an hour:

```bash
GH_TOKEN=$("$CLAUDLOBBY_ROOT"/lib/mint-github-token.sh) gh pr list
```

#### Everything else

Ops guidance, failure modes (401 vs 403), pagination gotchas, and the piped-`gh`
exit-status trap are identity-independent and live in the paired
[`github.md`](github.md) integration — read that alongside this file. A fleet can
equip both `github` and `github-app` during a migration window; the two servers
compose side by side with distinct tool prefixes.
