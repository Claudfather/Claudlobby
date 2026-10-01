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

Reply in-thread first with "Assigning to <Worker>." so the human sees what's happening. Then admit, assign, and deliver through `/fleet-ops`; assignment alone is not delivery. On the worker's recorded report, summarize the result to the originating thread.

**Posting proactively (no inbound message to reply to):**

1. MCP tool: `mcp__plugin_telegram_telegram__reply` with `chat_id: <GROUP_CHAT_ID>` and your text.
2. If the Telegram tool is unavailable, disclose that the human notification was not sent. Record the linked assignment report (or an explicitly unlinked fleet report) so the manager can relay it; do not invoke the private transport helper.

**Mandatory worker post moments:** completion (+ PR link, tag manager), blocked (+ record `assignment block ASSIGNMENT_ID --reason "..." --request-id UUID`, or an unlinked fleet report when no assignment exists), unexpected scope change. Telegram preserves human visibility; the canonical assignment report is the manager's structured record. No acknowledgement post — `assignment accept` records that separately (Worker Lifecycle, Step 2).
