---
title: Proactivity Discipline
---

# Proactivity Discipline

On a manager carrying the `checkin` protocol, idle silence is recorded, not posted — the check-in beat holds it, and that protocol decides when it reaches the operator. At every wait-point, post one line — `waiting on X, ETA Y` — to Telegram so the human can see fleet state without asking.

Wait-points:

- **Plan posted, awaiting human ratify** → `waiting on <human> ratify of <plan-link>, ETA <interrupt-driven>`.
- **Reviewer in progress** → `waiting on <reviewer> review of #NN, ETA ~N min`.
- **Engineer implementing** → `waiting on <engineer> implementation of #NN, ETA ~N min based on scope`.
- **Quota reset pending** → `waiting on Opus quota reset at <HH:MM UTC>. Auto-resume scheduled.`
- **Waiting on user decision** → `waiting on <human> decision on <fork>, ETA <interrupt-driven>`.

Silence is correct only when literally nothing is pending AND no one is waiting on you. Any other silence is an observability hole: where a check-in beat runs, the beat records it; where none does, the wait-point line above is the only thing that closes it.

Format: one sentence, ≤120 chars, no emoji/markdown, tag the human only if they need to act.
