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

## Frontmatter fields

Every real integration file has YAML frontmatter with a `title:` matching its H1 (see the worked example below) — none of the 16 shipped files skip this. Two more fields show up in practice:

- **`type: mcp|cli`** — labels which of the two flavors above the file is. Present on 12 of 16 files; the 4 that omit it (`github.md`, `homeassistant.md`, `notion.md`, `shopify.md`) fit one flavor just as clearly. Nothing in `composer.py`/`validator.py` reads this field — it's documentation-only, applied inconsistently even as that. Add it for clarity, but don't rely on it being enforced.
- **`env_contract:`** — a real, code-consumed map of environment variables this integration needs, e.g. (`snowflake.md`):

  ```yaml
  ---
  title: Snowflake
  type: cli
  env_contract:
    SNOWFLAKE_ACCOUNT:
      description: Snowflake account identifier
      tier: fleet
    SNOWFLAKE_PRIVATE_KEY_PATH:
      description: Path to Snowflake RSA private key
      tier: fleet
  ---
  ```

  `composer.py::collect_env_contracts` and `mcp_resolve.py` both read `env_contract:` (same `{description, tier: fleet|bot}` shape as MCP fragments' `_env_contract` — see `library/mcp/README.md`) to fold these vars into the fleet's environment-variable contract, which `claudlobby doctor` checks against `.env`. 5 of 16 files use it today (`neon.md`, `printify.md`, `railway.md`, `shopify.md`, `snowflake.md`) — add it whenever an integration depends on env vars not already declared by a paired MCP fragment.

## Grant contract (`tool_grants:`)

An integration declares the tools it authorizes as an additive grant contract in frontmatter. `composer.py::_resolve_integration_grants` reads it and emits the corresponding `allow` entries into each equipping bot's `settings.local.json` — so a bot's permissions travel with the source that requires them instead of a hand-maintained global allow-list:

```yaml
---
title: GitHub MCP
tool_grants:
  - "mcp__github__*"   # an mcp__ glob (trailing * only)
---
```

Each entry is one of three shapes (the grant grammar): an `mcp__<server>__*` glob, a `Bash(<cmd> *)` pattern, or a bare CamelCase tool name. For an MCP-paired integration the prefix is rewritten per instance (`gws` + instance `personal` → `mcp__gws-personal__*`); a connector-backed grant (`mcp__claude_ai_*`, no wire fragment) is emitted literally. The compositor validates each entry's shape and warns on a malformed grant or an over-broad bare `Bash`. Validation is folder-aware: a `dir/` folder-expansion equip resolves every member integration's contract, so a malformed grant nested in an expanded folder is not silently skipped.

## Example

`library/integrations/github.md` (MCP-paired, but omits `type:`/`env_contract:` despite fitting the MCP flavor clearly — real content, abbreviated):

```markdown
---
title: GitHub MCP
---

# GitHub MCP

Wire config: `library/mcp/github.json` (uses `${GITHUB_PAT}`).

#### Common Ops

- **List PRs:** `mcp__github__list_pull_requests` — returns open PRs for a repo
- **Read a PR:** `mcp__github__get_pull_request` + `mcp__github__get_pull_request_files`
- **Post review:** `mcp__github__create_pull_request_review` — approve, request changes, or comment

#### Gotcha: 30-File Pagination

`mcp__github__get_pull_request_files` returns **only the first GitHub API page** — max 30 files. PRs with > 30 files silently truncate.

**Canonical full-file list:**

    gh pr view <NN> --json files --jq '.files[].path'

If you see exactly 30 files in the MCP response, assume truncation and re-fetch via `gh`.

... (Same-Identity Fleet, When `gh` CLI Is Better, and Failure Modes sections follow — see the real file)
```

## Naming

The `.md` filename matches the service name. For MCP-paired integrations, this matches the MCP fragment: `library/mcp/github.json` ↔ `library/integrations/github.md`.
