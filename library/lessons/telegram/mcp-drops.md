---
title: "Lesson: Telegram MCP drops inbound messages (Claude Code upstream bug)"
---

The Telegram channel plugin in Claude Code drops inbound messages unpredictably — both when the bot is idle AND mid-conversation. This is a fundamental upstream bug in Claude Code's MCP notification handler, not something a bot or fleet config can fix.

## Root cause (per upstream)

Tracked in Claude Code GitHub issues: #36477, #37933, #38259, #38736.

- Duplicate MCP connections on stdio channel servers
- Stale `onclose` handlers persisting across reconnects
- No auto-reconnect on stdio channel disconnect
- Notifications dropped silently when the connection state is degraded

## Workarounds that DO NOT help

Save your time — these have been tried and don't fix it:

- **Keepalive cron sending Enter to tmux** — no effect on inbound delivery; can also accidentally submit ghost text (see `lessons/telegram/keepalive-enter-injection` if you've codified that).
- **`/mcp reconnect`** — temporary at best; drops resume within minutes.
- **Pressing Enter between turns** — documented community workaround; doesn't help in practice.

## What to actually do

- **Outbound is reliable.** The bot can post to Telegram dependably. The drop affects inbound only.
- **Use Remote Control for reliable back-and-forth.** When drops are bad, `--remote-control` over the Claude web/desktop client gives a deterministic channel.
- **Expect gaps in message IDs.** When the human resends a dropped message, message IDs in the chat will not be contiguous — that's the symptom, not a separate bug.
- **Don't fabricate context for missing messages.** If the human references something the bot didn't see, ask them to repaste — never guess what was dropped.

## Implication for fleet design

Don't build flows that *require* every inbound Telegram message to land. Workflows that need guaranteed delivery (approvals, dispatch confirmations) should either:

- Use Remote Control for the human side, OR
- Have an explicit "ack" handshake the human can repeat if it's lost.

The fleet keeps running fine through drops; only the human-bot dialog is affected.
