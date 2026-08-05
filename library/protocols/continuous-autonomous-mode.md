---
title: Continuous Autonomous Mode
description: Manager bot stays running, auto-dispatches on common situations, pauses on quota, never goes silent.
---

# Continuous Autonomous Mode

A continuous-autonomous manager doesn't wait for the human to ping. It stays alive in tmux, watches the fleet state, and acts on **ratified patterns** — situations the human has already approved as auto-dispatch. Anything outside the ratified set goes to the human.

**Ratified auto-dispatch patterns** (extend per fleet):

- **Idle worker + open backlog** → dispatch next sprint item to the worker.
- **PR receives "request changes"** → bounce to the original engineer with the verdict body.
- **Reviewer reports `context-degraded`, or shows ~3+ completed rows in a 24h `claudlobby report-back` window** → restart the reviewer (Sonnet-sensitive).
- **Worker reports complete + non-blocking issue surfaced** → file the issue, do not block the worker.
- **Quota threshold hit (shared Anthropic account)** → pause all worker dispatch; resume when quota recovers.
- **Human checks in via Telegram** → respond with current fleet state in <5s.

**Wait-point discipline:**

When the manager has nothing to dispatch and is genuinely waiting:

- Post a wait-point message to Telegram with current state and ETA-to-next-action.
- Never go silent. Silence reads as "stuck" — and the human escalates.
- A "still waiting" beacon every 10–15 min is better than nothing for long waits.

**What is NOT auto-dispatched:**

- Anything involving destructive operations (force-push, prod DDL, prod backfill, full-refresh).
- Anything requiring a judgment call the human hasn't ratified (cross-fleet coordination, scope expansion, hire/fire equivalents).
- First-of-its-kind situations. Surface to human; let them ratify the pattern; *then* auto-dispatch the next occurrence.

**Why this matters:** the value of an always-on manager is *throughput while the human is asleep*. Without ratified patterns it's busy work; with them, it's a fleet that compounds overnight.
