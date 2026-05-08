---
title: Telegram Routing
---

# Telegram Routing

**Reply locality:**

- **DMs** — reply directly.
- **In your bot's group** — reply in that group; never cross-post.

**`requireMention: false` (manager in own group):**

You see every message. Stay silent if the message @-mentions another bot or replies to another bot's message. Respond if it's generic, addressed to the group, or names you.

**`requireMention: true` (worker in shared group):**

Respond only when your `@<handle>` is mentioned, or when the user replies to your own message.

**When dispatching as a manager:**

Reply in-thread first with "Assigning to <Worker>." so the human sees what's happening. Then dispatch via tmux. On worker report-back, summarize the result to the originating thread.

**Posting proactively (no inbound message to reply to):**

1. MCP tool: `mcp__plugin_telegram_telegram__reply` with `chat_id: <GROUP_CHAT_ID>` and your text.
2. Bash fallback: `$CLAUDLOBBY_ROOT/lib/tg-post.sh "Your message"`.

**Mandatory worker post moments:** task acknowledged, progress milestone (~2-3 min during active work), completion (+ PR link, tag manager), blocked (+ run `report-back.sh blocked`), unexpected scope change.
