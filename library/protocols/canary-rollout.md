---
title: Canary Rollout
description: Manager protocol — by default, canary a fleet-wide framework change on one production bot before rolling it to the whole fleet.
---

# Canary Rollout

A change can pass every throwaway-bot test and still break the moment it goes live fleet-wide. Pre-merge validation and the production canary guard **two different failure modes**, and they carry different force:

| Gate | Proves | Force |
|------|--------|-------|
| Pre-merge throwaway-bot validation (`validate-bot-change.sh`) | The **code works** — the event fires, the alert sends | **Hard gate.** Mandatory for every runtime change, before merge. |
| **Canary rollout** (this protocol) | The **rollout is safe** in real production state | **Strong default.** Do it when a bad fleet-wide rollout would hurt the fleet — judgment, not a universal mandate. |

The throwaway bot runs in a clean, synthetic environment. Production carries state it never had: the real plugin/marketplace registry, real MCP config, live supervision units, a real running binary, concurrent bots. That gap is exactly where a merged-and-tested change bites. So the pre-merge gate is mandatory for every runtime change; the canary is the **expected default** when that change ships *live across the fleet* — a judgment call, not a rule that fires on every PR.

## When to canary — the default

Reach for a canary by default when a change to the **Claudfather framework itself** (claudlobby, clauDNA, claudron) goes **live fleet-wide** — where applying it touches or restarts every running bot at once and a bad rollout would hurt the fleet:

- `lib/` supervision & lifecycle scripts (`start-bot.sh`, `keepalive.sh`, restart/reload paths)
- The fleet plugin set (adds, removes, marketplace changes)
- The bridge / dispatch transport
- Composed `bot.conf` env that every bot sources at startup
- Anything whose rollout mechanism is "restart the fleet"

## When to skip

A fleet-wide production canary adds no value here — don't spend one:

- **Single-bot changes** — the blast radius is already one bot; that bot *is* the canary.
- **App-repo (product) work** — shipping a feature or fix to a product repo is not a fleet-wide framework rollout. The product's own tests and deploy gates cover it.
- **Non-runtime changes** — docs, README, planning files: nothing to drill.
- **Library content that only re-composes** — a protocol/skill edit that lands on a bot's next `/reload` without a fleet-wide restart. (If you *do* roll it fleet-wide via a restart, the restart is the risk — canary that.)

Judgment call in one line: would a bad version of this change hurt more than one running bot at once? Yes → canary. No → ship it.

## The loop

Once you have decided to canary:

1. **Pick a low-risk canary bot.** A worker, never the manager running the rollout — if the canary wedges, you still want a live hand on the controls. Prefer an idle bot with no WIP (see `safe-worker-restart` — clean pane, clean `git status`, no pending report). A bad canary should cost nothing.
2. **Deploy to the canary only.** Apply the change and restart/reload that one bot. The rest of the fleet stays on the old path — it is your control group and your fallback.
3. **Observe / drill it in production.** Watch it do the *real* thing, not a stub: does it boot clean, does the path the change touches actually fire against real config? Don't just confirm the process is up.
4. **Evidence gate.** Green-light only on **observed behavior + audit evidence** — cite the pane output, the `data/events/*.jsonl` line, the `keepalive.log` entry. No evidence, no rollout. If the canary surfaces a bug, **halt**: fix it, re-canary from step 2.
5. **Flag the human before the fleet-wide switch.** The fleet-wide roll is the committal, hard-to-walk-back step. Surface it — "canary clean on `<bot>`, rolling to the remaining N?" — and get a go before flipping the fleet.
6. **Roll the rest, then burn in.** Ship to the remaining bots. "Rolled" is not "done" — a slow failure (a leak, a wedged restart on the fourth bot) only shows on the fleet under load. Declare done after the burn-in is quiet.

## Why this exists

A fleet-wide-breaking bug is far cheaper caught on one bot, in the open, than during a fleet-wide restart that hits every bot at once. The fleet has repeatedly paid to keep blast radius at one: per-bot socket isolation, so a dead tmux server drops a single bot instead of every session on the host (#422); and a bridge-fork rollout whose `start-bot.sh` marketplace-add bug surfaced on the canary bot rather than across the fleet (#596). Rolling a framework change blind bets the whole fleet; canarying it bets one bot. That is the whole point: fail on the canary, not on the fleet.
