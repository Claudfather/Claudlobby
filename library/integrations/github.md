---
title: GitHub MCP
---

# GitHub MCP

Wire config:

- `library/mcp/github.json` — static `${GITHUB_PAT}` mode. Simplest setup. Reference via `mcp: [github]` in fleet.yaml.
- `library/mcp/github-app.json` — GitHub App installation-token mode. Mints a fresh `ghs_` token at MCP spawn time via `git-credential-botfarm` (no long-lived PAT in the fleet `.env`). Reference via `mcp: [github-app]`. Setup: [`documentation/runbooks/github-app-setup.md`](../../documentation/runbooks/github-app-setup.md). Skills/CLI calls that need a short-lived token can mint one with `lib/mint-github-token.sh`.

#### Identity / Auth — bot App, not personal PAT

Fleet-scope GitHub operations authenticate via the **bot App identity** the fleet commits as, never via a developer's personal PAT.

**Why:**

- Personal PATs scope to the individual's repo access — they miss org repos the developer isn't on, and rotate when the developer rotates them.
- Bot Apps install on the orgs/repos they need. Installation tokens are stable across team changes and scoped to the install.
- Fleet commits already land as `<botname>[bot]` via git config — issue/PR auth should match the same identity.

**How to wire:**

- `$GITHUB_PAT` (referenced by `library/mcp/github.json`) and `GH_TOKEN` (read by `gh` CLI) both resolve to the App's installation token, not a personal PAT.
- The App must be installed on the target orgs with the needed scopes: `issues:write`, `pull_requests:write`, `contents:read` at minimum.
- Mint short-lived installation tokens via GitHub's API (App private key → JWT → installation token) and refresh on a cron via a fleet script. Or use a long-lived flow if the integration is one-off.

**Failure signal:** `Could not resolve to a Repository` on a known org repo means the token's identity doesn't have access. **Don't rotate the personal PAT — wire the App token.** The tell: `gh repo list <org>` returns only a developer's visible subset (e.g. ~8 repos) instead of the org's full repo list.

#### Common Ops

- **List PRs:** `mcp__github__list_pull_requests` — returns open PRs for a repo
- **Read a PR:** `mcp__github__get_pull_request` + `mcp__github__get_pull_request_files`
- **Create issue:** `mcp__github__create_issue` — title, body, labels, assignees
- **Post review:** `mcp__github__create_pull_request_review` — approve, request changes, or comment
- **Search code:** `mcp__github__search_code` — regex across repos

#### Gotcha: 30-File Pagination

`mcp__github__get_pull_request_files` returns **only the first GitHub API page** — max 30 files. PRs with > 30 files silently truncate.

**Canonical full-file list:**

```bash
gh pr view <NN> --json files --jq '.files[].path'
```

If you see exactly 30 files in the MCP response, assume truncation and re-fetch via `gh`.

#### Same-Identity Fleet

When all bots share one GitHub PAT (single identity), GitHub blocks `--approve` and `--request-changes` on PRs that same identity authored. Use the `same-identity-fallback` protocol: post the verdict as a COMMENT with `**Approve**` or `**Request Changes**` in the body.

#### When `gh` CLI Is Better

- Bulk operations across many PRs (`gh pr list --json ...`)
- Anything needing pagination control
- Operations the MCP doesn't expose (`gh pr merge --admin`, `gh pr review`)
- Fetching raw diff content (`gh pr diff <NN>`)

#### Failure Modes

- `401 Bad credentials` → PAT expired or revoked. Regenerate at github.com/settings/tokens.
- `403 rate limit` → shared PAT hit the 5,000 req/hr limit. Wait or reduce parallel bot activity.
- `422 Validation Failed` → usually a missing required field or invalid label name.
- Context deadline exceeded → proxy/network issue. Retry once, then report blocked.
