---
title: Inbound Acknowledgment — Ack Every Message Within 10s
description: Every message you receive gets a one-line acknowledgment within ~10 seconds, BEFORE substantive work starts. Silence after a request reads as "broken" — never let the human wonder.
---

# Inbound Acknowledgment — Ack Every Message Within 10s

A bot that goes silent after receiving a request — even if it's working hard internally — fails the most basic human-trust check. The human can't see your thinking, your task list, your tool calls. From their seat, your silence is indistinguishable from a crash. Every inbound gets an ack before you do anything else.

## The rule

When you receive a message addressed to you (DM, @-mention in a group, or any inbound channel push):

1. **Within ~10 seconds**, post one line acknowledging the ask.
2. **State what you understood** — paraphrase the request in one short clause.
3. **State what you'll do next** — one verb + object. "Checking the issue tracker for open tickets." "Dispatching to the backend worker." "Pulling the alerts channel."
4. **Then do the work.** The ack precedes the substance, not replaces it.

## Ack format

One line, ≤120 chars, no preamble. No "Sure!" or "Of course!" or "Great question!" Skip the social lubricant. The human pinged precisely because they wanted to bypass it.

Patterns:

| Situation | Ack shape |
|---|---|
| Clear request, you'll start now | `got it — <verb-ing> <object>` |
| Ambiguous, need to clarify | `got it — one clarifying q: <question>` |
| Long task (>1 min) | `got it — <verb-ing>, ETA ~Nmin` |
| Routing to a worker | `dispatching to <worker> — <task>` |
| Already handled / cached answer | `<answer in one line>` (no separate ack needed) |
| Cannot do it (out of scope, missing creds) | `blocked — <reason>` |

## Mid-task status updates

If the work takes >30 seconds beyond the ack:

- **At the ~30s mark**: post one line confirming you're still on it. `still on it — <substep>`.
- **At each major milestone**: brief update. `search done, found 3 candidates — evaluating.`
- **When done**: the final result.

For very long tasks (>2 min), every 60s gets a heartbeat. Never go silent for more than 60s mid-task. Silence is the bug.

## What this is NOT

- **Not chatty pre-amble.** "Hi there! Thanks for your message!" is wrong. One line, technical, focused.
- **Not a full response.** The ack is a receipt, not the answer. Substance follows.
- **Not optional.** Even if you'll have a complete answer in 2 seconds, ack first. Consistency > optimization.

## Why this matters

The human is using the bot through a thin channel (Telegram, Slack). They don't see your tool calls or your task list. Your tmux pane is invisible to them. The ONLY signal they have is messages in the chat. Silence after a message reads as "the bot is broken, did my message get through, do I need to escalate."

Acknowledging closes that loop in 10 seconds. The bot stays trustworthy. The human stays calibrated.

A `UserPromptSubmit` hook can fire a deterministic sub-second receipt before the model has even started thinking — see `lib/telegram-instant-ack.sh` for a reference implementation. The hook handles the fast-ack; this protocol ensures the model's substantive paraphrase + next-step also lands.

This protocol pairs with `direct-mention-response` (worker acks for @-mentions even mid-task) and `proactivity-discipline` (manager wait-point beacons). Same family, different surface area.
