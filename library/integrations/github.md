---
title: GitHub MCP
tool_grants:
  - "mcp__github__*"
---

# GitHub MCP

Wire config: `library/mcp/github.json` (uses `${GITHUB_PAT}`).

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

#### Gotcha: a 403 blocks REST, not GitHub

REST core, search and GraphQL throttle in **independent buckets**, so a REST block is not an outage and rarely blocks the actual work. Verified live under an active secondary block, reads *and* writes: `gh pr list`, `gh issue list`, `gh issue view`, `gh issue create` and `gh api graphql` all kept working while `gh api repos/...` 403'd on the same token in the same second. Reroute to those rather than idling.

The GitHub MCP is REST-backed, so `mcp__github__*` failing while `gh pr` / `gh issue` succeed is **expected, not a broken MCP** — do not go hunting an auth bug. Not every `gh` subcommand is GraphQL-backed either (`gh api` and `gh pr diff` are REST), so test the specific one you need instead of assuming porcelain is safe.

**Check exit status before a pipe.** `gh api ... | head` reports *head's* status, so a 403 prints as exit `0` and reads as success. Use `${PIPESTATUS[0]}`, or run the call before piping it.

#### When `gh` CLI Is Better

- Bulk operations across many PRs (`gh pr list --json ...`)
- Anything needing pagination control
- Operations the MCP doesn't expose (`gh pr merge --admin`, `gh pr review`)
- Fetching raw diff content (`gh pr diff <NN>`)

#### Failure Modes

- `401 Bad credentials` → PAT expired or revoked. Regenerate at github.com/settings/tokens.
- `403 rate limit` → **reroute, do not wait.** Two different limits return this. **Primary** (5,000/hr) is visible in `gh api rate_limit` and publishes a reset. **Secondary** (abuse) is invisible there — the meter reads a full quota while every REST call 403s — and publishes **no reset**, so "wait" has no defined end. Do not read the meter to decide whether you are throttled; test the door: `gh api repos/<org>/<repo>`. Then see "Gotcha: a 403 blocks REST, not GitHub" — much of the work is still reachable.
- `422 Validation Failed` → usually a missing required field or invalid label name.
- Context deadline exceeded → proxy/network issue. Retry once, then report blocked.
