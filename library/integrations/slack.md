---
title: Slack
type: mcp
---

# Slack

Wire config: `library/mcp/slack.json` (uses `${SLACK_TOKEN}`).

### Common ops

- List channels: `mcp__slack__channels_list`
- Read history: `mcp__slack__conversations_history`
- Post message: `mcp__slack__conversations_add_message`
- Search: `mcp__slack__conversations_search_messages`
- Check unreads: `mcp__slack__conversations_unreads`

### Gotchas

- Token is a user OAuth token (`xoxp-...`), not a bot token — actions appear as the user
- `SLACK_TOKEN` is bot-tier (per-bot `.env`) since different bots may use different Slack identities
- Channel history returns newest-first by default — paginate with `oldest`/`latest` params
- Rate limits are per-method — bulk operations need backoff
