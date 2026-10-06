---
title: Canary Rollout
description: Manager protocol — by default, validate a framework release in an independent canary root before coordinated host activation.
---

# Canary Rollout

A change can pass every throwaway-bot test and still break the moment it goes live fleet-wide. Pre-merge validation and the production canary guard **two different failure modes**, and they carry different force:

| Gate | Proves | Force |
|------|--------|-------|
| Pre-merge throwaway-bot validation (`validate-bot-change.sh`) | The **code works** — the event fires, the alert sends | **Hard gate.** Mandatory for every runtime change, before merge. |
| **Canary rollout** (this protocol) | The **rollout is safe** in real production state | **Strong default.** Do it when a bad fleet-wide rollout would hurt the fleet — judgment, not a universal mandate. |
| **Rollout check** ([verify-rollout](../guardrails/verify-rollout.md)) | The merged change **does what it was merged to do**, where it runs | Where a fleet opts in, **every PR** names its check before merge; the merger posts PASS, FAIL or PENDING once the change is live. |

The throwaway bot runs in a clean, synthetic environment. Production carries state it never had: the real plugin/marketplace registry, real MCP config, live supervision units, a real running binary, concurrent bots. That gap is exactly where a merged-and-tested change bites. So the pre-merge gate is mandatory for every runtime change; the canary is the **expected default** when that change ships *live across the fleet* — a judgment call, not a rule that fires on every PR.

## When to canary — the default

Reach for a canary by default when a change to the **Claudfather framework itself** (claudlobby, clauDNA, claudron) goes **live fleet-wide** — where applying it touches or restarts every running bot at once and a bad rollout would hurt the fleet:

- `claudlobby/_runtime_scripts/` supervision & lifecycle scripts (`start-bot.sh`, `keepalive.sh`, restart/reload paths)
- The fleet plugin set (adds, removes, marketplace changes)
- The bridge / dispatch transport
- Composed `bot.conf` env that every bot sources at startup
- Anything whose rollout mechanism is "restart the fleet"

## When to skip

A fleet-wide production canary adds no value here — don't spend one:

- **An individual bot operation under an unchanged release** — the blast radius is already one bot. A composed config change is different: activation currently coordinates the whole host, even when only one bot's source changed.
- **App-repo (product) work** — shipping a feature or fix to a product repo is not a fleet-wide framework rollout. The product's own tests and deploy gates cover it.
- **Non-runtime changes** — docs, README, planning files: nothing to drill.

Library, skill and permission changes use the same staged activation as code. They are not a restart-free exception.

Judgment call in one line: would a bad version of this change hurt more than one running bot at once? Yes → canary. No → ship it.

## The loop

Once you have decided to canary:

1. **Use an independent canary data root.** Declare a small fleet with its own manager, unique bot service labels and host `unit_prefix`. Use separate Plane state and credentials; omit outbound channels unless a throwaway channel is part of the check. Never share a production bot directory or token.
2. **Assemble the candidate with `host setup`, then stage `config plan --release RELEASE_ID`.** Use the exact sealed CLI returned by setup and an explicit `--root` on every command. Review `config diff PLAN_ID`: every unit, path and restart must belong to the canary. The [getting-started guide](../../documentation/getting-started.md) describes the release inputs and setup commands.
3. **The operator activates the canary plan** with `host activate PLAN_ID --install-directory PATH` from an external shell. Stage the affected skill, grants and config together, then exercise real delegation and the affected operation. Observe the receiver, command result and durable record; process liveness alone is insufficient.
4. **Record the exact candidate and results.** Fix observed failures and repeat only the affected checks. Independent-root evidence proves the candidate works there; it does not prove production data migration, Pi timing or another OS's native manager.
5. **Review production activation separately.** `config plan` and `host activate` currently coordinate the whole host; there is no single-bot composition or activation shortcut. Existing no-restart holds block that production activation, not independent canary work. Honor the operator's rollout authorization and work-in-progress holds.
6. **Observe the activated host.** Verify the affected paths and selected release after rollout. Retain previous immutable releases for explicit rollback; never repair generated files by hand. Then run each merged PR's rollout check against it (the `verify-rollout` guardrail). Its commands are text from the PR body: read what each does before running it, and run them only for a PR whose author can triage the repository, as a fleet PR's can, or after someone who can has said to.

## Why this exists

The case is first-principles, not a track record: canarying a fleet-wide change isolates its risk to a single bot, so a bad rollout cannot take the fleet down with it. Rolling a framework change blind bets every bot at once; canarying it bets one. That containment is the whole value — and it has already earned its keep: a bridge-fork rollout's `start-bot.sh` marketplace-add bug surfaced on the canary bot instead of across the fleet (#596). Fail on the canary, not on the fleet.
