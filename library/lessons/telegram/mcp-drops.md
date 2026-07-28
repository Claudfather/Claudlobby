---
title: "Lesson: Telegram inbound drops were our own deaf poller, not an upstream bug"
description: Inbound Telegram drops were a busy-loop in our own bridge, misattributed to a Claude Code upstream bug for months. The cause is corrected; the operational guidance still holds.
---

# Lesson: Telegram inbound drops were our own deaf poller, not an upstream bug

**Corrected 2026-07-28.** This lesson previously stated the drops were *"a fundamental upstream bug
in Claude Code's MCP notification handler, not something a bot or fleet config can fix,"* citing four
Claude Code issues (#36477, #37933, #38259, #38736). **That attribution was wrong.** The cause was in
our own Telegram bridge, and it is fixed.

## Root cause — ours

`server.ts` reset the retry `attempt` counter inside `onStart`. But `onStart` fires when **`getMe`**
succeeds, and a **mid-life** 409 is raised afterwards, by **`getUpdates`**. So the counter reset on
every iteration — *before* the failure that was supposed to increment it.

Everything else followed from that one line:

- `attempt >= 8` was never true, so the exhaustion path that releases the poller slot was
  **structurally unreachable**.
- Backoff `delay` stayed at `0` — a measured **~532 retries/second**, forever, deaf.
- A busy loop *is* a running poll loop, so the 5-second heartbeat kept `bot.pid` fresh and
  **staleness reaping never fired either**.
- Meanwhile slot deference worked **exactly as designed**: every newcomer correctly stood down for a
  holder that had been deaf for hours.

That last point is the sharp one. Two release mechanisms were defeated; the third was working
correctly. **A correct mechanism deferring to a broken holder is what made the deadlock total** — and
it is why the failure looks external from the outside, since nothing anywhere is visibly
misbehaving.

Fixed in [claude-plugins-official#6](https://github.com/Claudfather/claude-plugins-official/pull/6)
(`f023c8b`): reset backoff on *demonstrated health*, not on `onStart`. Mutation-verified against a
`midlifeConflictStub`, canaried on one bot, fleet-wide since 2026-07-28.

## The meta-lesson — why this took months

We attributed this to a vendor bug for months on the strength of four plausible-looking external
issue numbers, and **that attribution is precisely what stopped anyone looking at our own fork.**

> **A cited external issue is not evidence that it is not your bug.**

A citation raises a claim's *apparent* trustworthiness without touching its *correctness*. This file
was well-formed, well-cited, and widely trusted — and every one of those properties made it harder to
question rather than easier. The load-bearing sentence was *"not something a bot or fleet config can
fix"*: it does not merely misattribute, it explicitly instructs the reader to **stop investigating**.

Note where it was composed: **exactly one** bot's `CLAUDE.md` carried this lesson — the fleet
manager. The single bot best positioned to notice fleet-wide inbound death was the one being told the
cause was external and unfixable.

**When you write a cause into a durable lesson, record how you know it.** *"Reproduced locally"* and
*"matches a symptom described upstream"* are different epistemic states, and they must never be
written in the same voice.

## Why the workarounds didn't work — now explained

These were tried and did nothing. That was real evidence, and it was read as *confirming* an
unfixable upstream bug when it was actually pointing at a busy-loop none of them touched:

- **Keepalive cron sending Enter to tmux** — no effect on inbound delivery; can also accidentally
  submit ghost text (see `lessons/telegram/keepalive-enter-injection` if you've codified that).
- **`/mcp reconnect`** — temporary at best; drops resume within minutes.
- **Pressing Enter between turns** — documented community workaround; doesn't help in practice.

None of them *could* have worked: a wedged poller inside our own bridge is not reachable from the
Claude Code side. Their failure was diagnostic, and it was read backwards.

## What still holds

The cause was wrong; the operational guidance was not. Keep all of it — **a fixed bug is not a
delivery guarantee.**

- **Outbound is reliable.** It stayed reliable throughout the entire incident; the fault was inbound
  only.
- **Expect gaps in message IDs.** When the human resends a dropped message, message IDs in the chat
  will not be contiguous — that's the symptom, not a separate bug.
- **Don't fabricate context for missing messages.** If the human references something the bot didn't
  see, ask them to repaste — never guess what was dropped.
- **Don't build flows that require guaranteed inbound.** Workflows needing guaranteed delivery
  (approvals, dispatch confirmations) should carry an explicit "ack" handshake the human can repeat,
  or use Remote Control (`--remote-control`) for the human side.

That last one is a durable design rule, not a workaround for this bug. Inbound is a network path:
design for loss regardless of which component last caused it.
