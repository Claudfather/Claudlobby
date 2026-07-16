---
title: Google Workspace (Gmail + Calendar)
type: mcp
tool_grants:
  - "mcp__gws__*"
---

# Google Workspace (Gmail + Calendar)

Wire config: `library/mcp/gws.json` (uses `${GOOGLE_OAUTH_CLIENT_ID}`, `${GOOGLE_OAUTH_CLIENT_SECRET}`).

Multi-instance capable: configure separate instances for different Google accounts (e.g., personal and work).

Supersedes the deprecated `gmail.json` fragment.

> **Backend cross-ref — self-hosted vs native.** `gws` is the **self-hosted OAuth MCP** for Google (multi-instance, `${GOOGLE_OAUTH_CLIENT_ID}`/`${GOOGLE_OAUTH_CLIENT_SECRET}`, grants under `mcp__gws__*`). Claude also ships **native connectors** for the same capabilities — `library/integrations/gmail.md` and `library/integrations/google-calendar.md` (`mcp__claude_ai_Gmail__*` / `mcp__claude_ai_Google_Calendar__*`, no fleet secrets). Equip **one path per capability per bot**, not both.

### Common ops

- Search email: `mcp__gws__search_gmail_messages`
- Read message: `mcp__gws__get_gmail_message_content`
- Draft/send: `mcp__gws__draft_gmail_message`, `mcp__gws__send_gmail_message`
- Calendar events: `mcp__gws__get_events`, `mcp__gws__manage_event`
- Auth flow: `mcp__gws__start_google_auth` (required on first use per instance)

### Gotchas

- OAuth credentials are per-instance — each instance needs its own `CREDENTIALS_DIR`
- `start_google_auth` opens a browser URL; headless environments need the URL copied manually
- Gmail search uses Google's search syntax, not regex
- Calendar operations use RFC3339 timestamps
