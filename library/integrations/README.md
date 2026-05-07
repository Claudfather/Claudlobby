# library/integrations/

Per-MCP **usage docs** — the "how to use this MCP server well" companion to each `library/mcp/<name>.json`. Different from `library/mcp/` (which is wire config) and `library/skills/` (which are slash-command-style actions).

## What belongs here

For each MCP server in `library/mcp/`, an optional `<server>.md` doc that captures:

- **Auth model** — what env var holds the token, how to rotate
- **Common operations** — the 5 calls you'll actually make
- **Gotchas** — pagination ceilings, rate limits, response shape quirks
- **Failure modes** — what an auth failure looks like, what a stale token looks like
- **When NOT to use this MCP** — workflows where the CLI (`gh`, `dbt`, `railway`) is more reliable

## Composition

Auto-paired with `mcp:` in fleet.yaml: if a bot lists `mcp: [github, notion]`, the compositor includes `library/integrations/github.md` and `library/integrations/notion.md` (when present) in a `## Integrations` section.

To override, list `integrations: [...]` explicitly in the bot's stanza.

## Example

`library/integrations/github.md`:

```markdown
## GitHub MCP

Wire config: `library/mcp/github.json` (uses `${GITHUB_PAT}`).

### Common ops

- List PRs: `mcp__github__list_pull_requests`
- Read a PR: `mcp__github__get_pull_request` + `mcp__github__get_pull_request_files`
- Comment on a PR: `mcp__github__create_pull_request_review`

### Gotcha: 30-file pagination

`mcp__github__get_pull_request_files` returns **only the first GitHub API page** — max 30 files. PRs with > 30 files silently truncate.

**Canonical full-file list:**

    gh pr view <NN> --json files --jq '.files[].path'

If you see exactly 30 files in the MCP response, assume truncation and re-fetch via `gh`.

### When `gh` CLI is better than the MCP

- Bulk operations across many PRs (`gh pr list --json ...`)
- Anything that needs streaming or pagination control
- Operations the MCP doesn't expose (e.g., `gh pr review --admin`)
```

## Naming

The `.md` filename matches the MCP fragment filename: `library/mcp/github.json` ↔ `library/integrations/github.md`.
