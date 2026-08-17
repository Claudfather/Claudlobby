# library/mcp/

MCP server wire-config fragments — small JSON files containing one MCP server definition each, plus compositor-only metadata. The compositor merges selected fragments into a single `.mcp.json` per bot, and separately derives env-var requirements and tool permissions from that same metadata.

## What belongs here

One `.json` file per MCP server. Use `${ENV_VAR}` placeholders for secrets — never hardcode tokens. Fragments are flat, not wrapped in `mcpServers`: the compositor takes whichever top-level key doesn't start with `_` as the server config.

For an in-fleet absolute **path** in a fragment (a local server's `dist/index.js`, a socket), write it against a composer anchor — `${FLEET_ROOT}/…`, `${BOT_DIR}/…`, or `${CLAUDLOBBY_ROOT}/…` — never a raw absolute. The deny-by-default source guard fails `generate` on any un-anchored, un-declared absolute path in a fragment; the anchor resolves at runtime to the fleet's real (migration-safe) location. A genuine path *outside* the fleet must be declared in the referencing bot's `external_paths`.

```json
{
  "_env_contract": {
    "GITHUB_PAT": {
      "description": "GitHub Personal Access Token",
      "tier": "fleet",
      "secret": true,
      "source": "cli:gh-token"
    }
  },
  "_permissions_contract": {
    "tools": ["search_code", "get_issue", "create_pull_request"]
  },
  "github": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github@2025.4.8"],
    "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PAT}" }
  },
  "_global_binary": "mcp-server-github"
}
```

The three underscore-prefixed keys are optional compositor metadata — never written into a bot's `.mcp.json` — so include whichever apply:

- **`_env_contract`** — maps each `${VAR}` placeholder to `{description, tier, secret, source, scope, provided_by}`. **`secret` (bool) is REQUIRED on every entry — `generate` refuses a fragment missing it.** It does not mean "is this string sensitive to print". **The test: can the integration AUTHENTICATE without this value? No → `true`. Yes → `false`.** Worked example: `PRINTIFY_SHOP_ID` is `false` because without it you still authenticate fine, you just cannot target a shop — a config failure, not a credential failure. `PRINTIFY_API_KEY` is `true`. Key *paths* are `true` (`GA4_SA_KEY_PATH`, `SNOWFLAKE_PRIVATE_KEY_PATH`): a missing key path is an auth failure. So is an auth principal — `SNOWFLAKE_USER` is `true`, because key-pair auth is (user, key) and the key authenticates nobody without it — while values that say *where* to send a credential (`SHOPIFY_STORE_DOMAIN`, `POSTHOG_HOST`, `SNOWFLAKE_ACCOUNT`) are `false`: that is connectivity, not authentication. 20 of the library's 48 declared vars (both surfaces) are `false`: the split is what keeps the fail-loud credential rung from firing on `PORT`, becoming noise, and getting suppressed along with the real alerts. `source` (optional string) names **where the value comes from** so the system can supply it at boot instead of a human pasting it into a `.env` — it is a CLOSED, framework-owned registry (`known_values.KNOWN_CREDENTIAL_SOURCES`: `literal`, `cli:gh-token`, and `mint:github-app` reserved), validated at any tier including a fleet overlay. Entries are whole identifiers rather than a kind plus a free parameter, and that is a security property: the resolver dispatches on the entire string through a fixed per-entry `case` arm, so contract text can never reach command position. **`source` is OPTIONAL — unlike `secret`, omitting it is legal and is the norm** (47 of the 48 declared vars). Absent means "a human supplies this value", exactly as today. **Do not add a `source` to an entry just because the field exists, and never invent a registry member to fill it** — an invented entry is precisely what the closed registry exists to prevent, and it would defeat the injection guarantee. `literal` is the explicit spelling of the same opt-out. `tier` (`"fleet"` or `"bot"`, default `"fleet"`) decides which `.env` the var gets scaffolded into: `scaffold_env_files` splits the fleet's collected vars on `EnvVar.tier`, writing fleet-tier vars to `local/<fleet>/.env` and bot-tier vars to each bot's own `.env`. `scope` (`"shared"` default, or `"instance"`) decides namespacing for multi-instance bots: `"shared"` vars pass through unchanged, `"instance"` vars get prefixed with the service (and instance) name so multiple instances don't collide — e.g. notion's generic `TOKEN` becomes `NOTION_TOKEN`, or `NOTION_WORK_TOKEN` for a `work` instance (see `mcp_resolve.py::canonical_var_name`). `provided_by` (`"composer"`, or omit): mark a var `"composer"` when the compositor emits it into `bot.conf` from fleet.yaml rather than the operator supplying it (e.g. claudron's `CLAUDRON_VAULT_PATH` from `claudron_vault_path`) — the entry stays in the fragment as self-documentation of the wire config's `${VAR}` reference, but `collect_env_contracts` skips it, so it is never scaffolded into a `.env` (a misleading empty stub) and never false-alarms doctor's env presence check; pairing checks for such vars belong to ecosystem-specific doctor checks instead. `collect_env_contracts` walks every bot's fragments to build the fleet-wide var list used by `claudlobby doctor` and `.env` scaffolding.
- **`_permissions_contract`** — `{tools: [...], read_only_tools: [...]}`. `tools` is the full list of MCP tool names the server exposes. Without `read_only_tools`, `_resolve_mcp_permissions` turns a non-empty `tools` into a `mcp__<server>__*` allow-pattern per instance in the bot's `settings.local.json` — all-or-nothing, appropriate for dev-tool servers (github, notion) whose writes are part of normal bot work. With `read_only_tools` (a subset of `tools`), the compositor instead emits one exact `mcp__<server>__<tool>` allow per read-only tool and **never** the server wildcard: reads compose in so headless bots don't wedge on safe-read prompts, while every other tool (catalog writes, order mutations) keeps prompting. The split is enforced at compose time — the paired integration file's `tool_grants` must mirror the read-only set exactly (a wildcard, write-tool, or missing-read entry fails generation with a directional error), a union-layer assert additionally rejects any skill/expertise/guardrail grant covering a non-read tool of the server, and a `read_only_tools` entry missing from `tools` fails too (typo/rename protection). A bot that genuinely needs an unattended write gets it explicitly via fleet.yaml `tools.allow`. See `shopify.json`/`printify.json` for the read-only shape. Omit the key, or leave `tools` empty, and no permission entry is generated for that server. **Gotcha:** a fragment that exposes tools but ships no `_permissions_contract` produces no allow-pattern, so those `mcp__<server>__*` calls silently hang on the permission prompt when a bot runs unattended — always include the contract for any server whose tools the bot should call on its own.
- **`_global_binary`** — the shell command name of a globally-installed equivalent to the `npx` package (e.g. `mcp-server-github`, `notion-mcp-server`). If `shutil.which()` finds it on `PATH`, the compositor rewrites `command: npx` and its `-y <pkg>` bootstrap args to invoke the binary directly via `node`, saving ~0.8s of npx cold-start per server instance. Pure optimization, falls back silently to `npx` otherwise — several fragments (`slack.json`, `gws.json`, `docker.json`) omit it entirely.

## Composition

Bots list MCP servers in fleet.yaml: `mcp: [github, notion]` (or, for multi-instance servers, an entry with `instances: [work, personal]`). For each entry, `composer.py::compose_mcp_json` loads the fragment, strips the `_env_contract`/`_permissions_contract`/`_global_binary` metadata, resolves `${VAR}` placeholders to their canonical instance-scoped names, and writes the remaining server dict into the bot's `.mcp.json` under the key `<name>` (default instance) or `<name>-<instance>` (named instances). The same metadata is read separately to build the bot's tool permissions (`_resolve_mcp_permissions`) and the fleet's env-var contract (`collect_env_contracts`). Pair with `library/integrations/<name>.md` for usage docs.

## Naming

Matches the service name: `github.json`, `notion.json`, `slack.json`.
