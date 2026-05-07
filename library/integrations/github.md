### GitHub MCP

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
