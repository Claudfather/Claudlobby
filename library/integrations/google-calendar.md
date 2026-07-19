---
title: Google Calendar (native connector)
type: connector
tool_grants:
  - "mcp__claude_ai_Google_Calendar__*"
---

# Google Calendar (native connector)

Claude's **native Google Calendar connector** — tools under the `mcp__claude_ai_Google_Calendar__*` namespace (`list_events`, `create_event`, `list_calendars`, `suggest_time`, …). There is no `library/mcp/` wire fragment and no `${...}` env var: the connector authenticates through the Claude account running the bot, not a fleet-managed OAuth secret. Equip it explicitly with `integrations: [google-calendar]`.

> **Backend cross-ref — pick ONE Google Calendar path per bot.** This native connector and the self-hosted `gws` MCP (`library/integrations/gws.md`) both reach Calendar. Equip **`google-calendar`** for the account-native connector (zero fleet secrets, `mcp__claude_ai_Google_Calendar__*`); equip **`gws`** for the self-hosted OAuth MCP (`mcp__gws__*` calendar tools, multi-instance). Do **not** equip both for the same capability on one bot.

## Availability vs grant

Two independent conditions must both hold for a Calendar tool to return data headless:

1. **Grant** — equipping `integrations: [google-calendar]` composes the `mcp__claude_ai_Google_Calendar__*` allowlist entry into `settings.local.json`.
2. **Availability** — the connector must actually be enabled and reachable for the running session. A grant on an unreachable connector yields a permanently empty result, not an error. Verify reachability in a real headless session before building a briefing section on it.

## Common ops

- **List events:** `mcp__claude_ai_Google_Calendar__list_events`
- **Create:** `mcp__claude_ai_Google_Calendar__create_event`
- **Calendars:** `mcp__claude_ai_Google_Calendar__list_calendars`
- **Find a slot:** `mcp__claude_ai_Google_Calendar__suggest_time`

## When NOT to use this

- The bot needs **multiple** Google accounts (personal + work) — use `gws` (multi-instance).
- The fleet standardizes on self-hosted OAuth for auditability or secret rotation — use `gws`.
