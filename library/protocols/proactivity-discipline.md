---
title: Proactivity Discipline
---

# Proactivity Discipline

Idle silence is recorded, not posted — the manager's `checkin` beat holds it; see that protocol for when it reaches the operator. At every wait-point, post one line — `waiting on X, ETA Y` — to Telegram so the human can see fleet state without asking.

Wait-points:

- **Plan posted, awaiting human ratify** → `waiting on <human> ratify of <plan-link>, ETA <interrupt-driven>`.
- **Reviewer in progress** → `waiting on <reviewer> review of #NN, ETA ~N min`.
- **Engineer implementing** → `waiting on <engineer> implementation of #NN, ETA ~N min based on scope`.
- **Quota reset pending** → `waiting on Opus quota reset at <HH:MM UTC>. Auto-resume scheduled.`
- **Waiting on user decision** → `waiting on <human> decision on <fork>, ETA <interrupt-driven>`.

Silence is correct only when literally nothing is pending AND no one is waiting on you. Any other silence is recorded through the check-in beat, not posted here.

Format: one sentence, ≤120 chars, no emoji/markdown, tag the human only if they need to act.
