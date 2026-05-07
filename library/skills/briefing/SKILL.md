---
name: briefing
description: "Use when it's time for a daily briefing or the user requests an ad-hoc summary. Orchestrates data from multiple MCP servers into a consolidated update."
argument-hint: "[morning|midday|evening]"
---

# Briefing

Consolidated daily briefing delivered via Telegram.

## Briefing Types

| Type | Trigger | Focus |
|------|---------|-------|
| **Morning** | 8:30 AM or `morning` | Overnight activity, today's calendar, pending tasks |
| **Midday** | 1:00 PM or `midday` | Morning progress, new items, afternoon prep |
| **Evening** | 6:30 PM or `evening` | Day summary, open items, tomorrow prep |

## Data Sources

Pull from each MCP server the bot has access to:

1. **Calendar** — today's events, upcoming meetings
2. **Email** — unread count, important items, action-needed
3. **Notion** — tasks due today, overdue items, status changes
4. **GitHub** — open PRs, review requests, CI failures
5. **Slack** — unread channels, mentions, alerts (if configured)

Skip any source the bot doesn't have MCP access to.

## Format

Keep it concise — this is delivered to mobile via Telegram. Lead with what needs attention, then context.

## Scheduled Trigger

Briefings are triggered by `briefing-cron.sh` which sends a prompt to the bot's tmux session:

```bash
#!/bin/bash
BRIEFING_TYPE="${1:-morning}"
BOT_SESSION="your-bot-session"

if ! /usr/bin/tmux has-session -t $BOT_SESSION 2>/dev/null; then
    exit 0
fi

/usr/bin/tmux send-keys -t $BOT_SESSION "/briefing $BRIEFING_TYPE" Enter
```
