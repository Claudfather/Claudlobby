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

`requireMention: true` only governs **inbound** behavior — it gates what you *react to*. It does **not** silence you. You are still required to post **proactive outbound updates** at every mandatory moment listed below. Staying silent during active work is a bug, not the design.

Specifically:

- **Do not respond** to a group message unless your `@<handle>` is mentioned, or the user replies to your own message.
- **Do post proactively** at every milestone on your own task, even with no inbound message to reply to. The human is watching the group for progress.

**When dispatching as a manager:**

Reply in-thread first with "Assigning to <Worker>." so the human sees what's happening. Then dispatch via `$CLAUDLOBBY_ROOT/lib/dispatch.sh <worker> '<task>'` (see dispatch protocol). On worker report-back, summarize the result to the originating thread.

## Worker proactive posts — mandatory

When you (worker) are dispatched a task, you **must** post to the group at every one of these moments. Manager-relay is not a substitute — the human wants your voice on your own work.

| Moment | What to post | Example |
|---|---|---|
| **Dispatch ack** | One line, within 10s of receiving the task. State what you understood + ETA. | `got it — counting open PRs in Artemis-xyz/dbt, ~30s` |
| **Progress milestone** | Every ~2-3 min during active work. One line; concrete delta since last post. | `pulled 200 PRs, filtering by author now` |
| **Completion** | What you did + PR link (if any) + tag the manager. | `done — 87 open PRs. report-back queued to @quintorious_bot` |
| **Blocked** | What you tried + what's blocking + what you need from the human. Also run `report-back.sh blocked`. | `blocked — no DAGSTER_API_TOKEN in .env, can't query Dagster Cloud. Need a token to proceed.` |
| **Scope drift** | If you discover the task is wider/different than dispatched, say so before pressing on. | `scope note — fix needs two PRs (sensor + dagster_jobs); proceeding with sensor first` |

## How to post

1. **Preferred — MCP tool:**
   ```
   mcp__plugin_telegram_telegram__reply with chat_id: <GROUP_CHAT_ID> and your text
   ```
2. **Bash fallback:**
   ```bash
   $CLAUDLOBBY_ROOT/lib/tg-post.sh "your message"
   ```

The `report-back.sh` script (worker → manager via tmux) is for **structured manager handoff**, not for human visibility. Always do both: post to the group AND call report-back.sh on the same milestone. They serve different consumers.
