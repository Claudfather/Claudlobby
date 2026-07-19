---
title: Gmail (native connector)
type: connector
tool_grants:
  - "mcp__claude_ai_Gmail__*"
---

# Gmail (native connector)

Claude's **native Gmail connector** — tools under the `mcp__claude_ai_Gmail__*` namespace (`search_threads`, `get_message`, `get_thread`, `create_draft`, `label_message`, …). There is no `library/mcp/` wire fragment and no `${...}` env var: the connector authenticates through the Claude account running the bot, not a fleet-managed OAuth secret. Equip it explicitly with `integrations: [gmail]`.

> **Backend cross-ref — pick ONE Google email path per bot.** This native connector and the self-hosted `gws` MCP (`library/integrations/gws.md`) both reach Gmail. Equip **`gmail`** for the account-native connector (zero fleet secrets, `mcp__claude_ai_Gmail__*`); equip **`gws`** for the self-hosted OAuth MCP (`${GOOGLE_OAUTH_CLIENT_ID}` etc., multi-instance, `mcp__gws__*`). Do **not** equip both for the same capability on one bot — the grants and prose duplicate and it is ambiguous which one answers a request.

## Availability vs grant

Two independent conditions must both hold for a Gmail tool to return data headless:

1. **Grant** — equipping `integrations: [gmail]` composes the `mcp__claude_ai_Gmail__*` allowlist entry into `settings.local.json`. This is what equipping this integration does.
2. **Availability** — the connector must actually be enabled and reachable for the running session. A grant on an unreachable connector yields a permanently empty result, not an error. Verify reachability in a real headless session before building a briefing section on it.

## Common ops

- **Search:** `mcp__claude_ai_Gmail__search_threads`
- **Read:** `mcp__claude_ai_Gmail__get_message`, `mcp__claude_ai_Gmail__get_thread`
- **Draft:** `mcp__claude_ai_Gmail__create_draft`
- **Label / triage:** `mcp__claude_ai_Gmail__label_message`, `mcp__claude_ai_Gmail__apply_sensitive_message_label`

## When NOT to use this

- The bot needs **multiple** Google accounts (personal + work) — use `gws` (multi-instance).
- The fleet standardizes on self-hosted OAuth for auditability or secret rotation — use `gws`.
