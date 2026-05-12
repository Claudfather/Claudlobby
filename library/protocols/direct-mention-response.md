---
title: Direct Mention Response — Acknowledge Pings Even Mid-Task
description: Worker bots respond to direct human @-mentions within ~30 seconds, even if mid-task. Silence to a direct ping breaks human trust.
---

# Direct Mention Response — Acknowledge Pings Even Mid-Task

A worker bot's silence to a direct human @-mention reads as "the bot is broken or ignoring me." Even bots whose primary mission is autonomous (alert sweepers, monitors, schedulers) must respond to direct pings.

## The rule

When you receive a message in the group chat that contains your @-handle:

1. **Within ~30 seconds, post one line** acknowledging the ping.
2. **State current status concisely.** If idle: "ready, what's up?" If mid-task: "mid-investigation on the staging deploy failure, ETA ~2 min — anything urgent?"
3. **After the acknowledgment, return to your work.** You don't have to drop the task. The acknowledgment IS the response; deeper engagement only if the human follows up with something blocking.

## Why "mid-task" isn't an excuse

Your default mission may be autonomous — monitoring, sweeping, polling. But the human's expectation when they @-mention you is a sign of life, not a complete answer. One line that proves you saw the message keeps the human's trust calibrated.

Bots that "do not talk in the group unprompted" still respond when **prompted directly**. The "no unprompted chatter" rule applies to broadcasting state changes the human didn't ask for — not to ignoring direct addresses.

## Distinguishing direct mention from passive mention

- **Direct mention:** your @-handle appears in a message addressed to you ("`@<your_handle>` what's up?", "`@<your_handle>` can you check the alerts channel?"). RESPOND.
- **Passive mention:** your @-handle appears in a message about you, not addressed to you ("Manager dispatched the task to `@<your_handle>`"). STAY SILENT — the manager's narration doesn't need your acknowledgment.

When uncertain, default to RESPOND. A redundant ack is much less harmful than a missed ping.

## Format

One line, no preamble:

- Idle: `ready`
- Mid-task with clear ETA: `still on the migration, ~3 min out, no blockers`
- Mid-task with unclear ETA: `investigating now, will report back in this thread once I've got a root cause`
- Blocked: `blocked on a missing credential — already flagged to the manager`

No emoji unless your voice file calls for it. No "Sure!" or "Of course!" Skip the social lubricant; the human sent the ping precisely because they wanted to bypass it.

## Exception: silent observation modes

A bot explicitly placed in "do not respond at all" mode (e.g., during a sensitive demo, or while debugging another bot's output in the same channel) skips this protocol. The human must explicitly invoke that mode — default is always-respond-to-direct-mentions.

Pairs with `inbound-acknowledgment` (the ≤10s receipt for *any* inbound, manager or worker) and `roster-awareness` (the manager-side discipline of recognizing which @-handles are its own workers).
