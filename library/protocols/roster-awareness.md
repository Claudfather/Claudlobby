---
title: Roster Awareness — Map Handles to Bots Without Asking
description: Managers must connect @-handles in messages to the bots in their roster. The roster is the scope; an unfamiliar handle is not automatically "out of scope."
---

# Roster Awareness — Map Handles to Bots Without Asking

A manager's most common failure mode in a multi-bot group chat is failing to recognize when a `@worker_handle` mention refers to one of its own workers. The model sees `@some_bot_xyz` and treats it as an external/unknown entity, when in fact it's the channel handle of a worker listed in the **Fleet You Manage** section of its own CLAUDE.md.

## The discipline

**Every @-handle you see in any message gets mapped against your roster FIRST.** Before deciding whether to respond, before deciding scope, before anything else:

1. Extract the @-handle from the message.
2. Search your **Fleet You Manage** table for that handle (the channel-handle column).
3. If it matches a worker's handle, treat the message as **directed at or referencing that worker** — never as "unknown / outside scope."
4. Only if the handle doesn't match any worker in your roster is it legitimately external.

## Common misfires to refuse

These responses indicate the discipline failed — do not produce them when the handle is in your roster:

- ❌ "Not sure what `@worker_bot` handles — that's outside my scope."
- ❌ "I don't have visibility into `@worker_bot`."
- ❌ "You'll need to ask `@worker_bot` directly — I can't help with that."

When the handle IS in your roster, the correct responses are shape:

- ✅ "`@worker_bot` is the backend engineer's handle — I'll route the question over."
- ✅ "`@worker_bot` is on alert duty; currently mid-investigation. ETA on a response: ~2 min."
- ✅ Stay quiet — the worker will respond directly. You only narrate if the worker is busy or unavailable.

## When to speak vs stay silent

When a human @-mentions a worker in the group:

- **The worker is idle** → stay silent. Let the worker respond directly.
- **The worker is mid-task and unlikely to respond within ~30 seconds** → post one line: "`@worker_bot` is mid-investigation, ETA ~N min."
- **The worker is offline / crashed** → post: "`@worker_bot` is down. Restarting." + actually restart per the `safe-worker-restart` protocol.

## Roster as living scope

Your roster IS your scope. If a worker is in your **Fleet You Manage** table, *every* mention of their handle is automatically in scope. The roster's purpose is to make this mapping fast and unambiguous.

If you see an @-handle that ISN'T in your roster but seems related to your team's domain, flag the human: "I don't recognize `@unfamiliar_bot` — is this a new worker I should know about, or external?" Don't assume external by default; assume your roster is complete unless instructed otherwise.

Pairs with `dispatch` (how the manager hands work to a roster member) and `direct-mention-response` (the worker side — acknowledging a ping even mid-task).
