---
title: Granola
type: mcp
tool_grants:
  - "mcp__granola__*"
---

# Granola

Wire config: `library/mcp/granola.json` (uses `${GRANOLA_MCP_PORT}`).

Meeting transcript and notes service via MCP remote proxy.

### Common ops

- List meetings: `mcp__granola__list_meetings`
- Get transcript: `mcp__granola__get_meeting_transcript`
- Search meetings: `mcp__granola__query_granola_meetings`
- Browse folders: `mcp__granola__list_meeting_folders`

### Gotchas

- Uses `mcp-remote` proxy — requires OAuth flow on first use
- Port must not conflict with other MCP remote instances (configure via `GRANOLA_MCP_PORT`)
- Meeting data is read-only — no create/update/delete operations
