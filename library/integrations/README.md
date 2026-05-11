# library/integrations/

**Usage docs** for external services — guides for MCP servers and CLI tools. Different from `library/mcp/` (wire config) and `library/skills/` (slash-command actions).

Not all integrations require MCP configs. Two flavors exist:

- **MCP-paired** — companion to a `library/mcp/<name>.json` fragment. Auto-included when a bot lists the matching MCP server.
- **CLI-based** — guide for a CLI tool (`doctl`, `vercel`, `railway`) or clauDNA skill set with no MCP server config. List explicitly via `integrations:` in fleet.yaml.

## What belongs here

For each external service a bot interacts with, an optional `<service>.md` doc that captures:

- **Auth model** — what env var holds the token, how to rotate
- **Common operations** — the 5 calls you'll actually make
- **Gotchas** — pagination ceilings, rate limits, response shape quirks
- **Failure modes** — what an auth failure looks like, what a stale token looks like
- **When NOT to use this** — workflows where a different tool is more reliable

## Composition

**MCP-paired:** auto-included when a bot lists `mcp: [github, notion]` — the compositor includes `library/integrations/github.md` and `library/integrations/notion.md` (when present) in a `## Integrations` section.

**CLI-based:** not auto-paired. List `integrations: [vercel, railway]` explicitly in the bot's stanza.

To override auto-pairing, list `integrations: [...]` explicitly.

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

The `.md` filename matches the service name. For MCP-paired integrations, this matches the MCP fragment: `library/mcp/github.json` ↔ `library/integrations/github.md`.
