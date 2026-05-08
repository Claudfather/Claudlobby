---
title: Paired-Work Review
description: Two reviewers, independent lenses (logic + cost, or empirical + mathematical), separate verdicts before consolidation.
---

# Paired-Work Review

For PRs where one lens is insufficient, dispatch two reviewers with **distinct, non-overlapping mandates**. Common pairings:

- **Logic + cost** — one reviews correctness (compile vs execute, incremental contracts, tests). The other reviews performance (query plans, clustering, warehouse sizing, credits).
- **Empirical + mathematical** — one reproduces claims locally and diffs measurements. The other mutation-tests assertions and finds counterexamples.
- **Minimalist + maximalist** — one audits by subtraction (remove decoration, tighten spacing). The other audits by composition (escalate hierarchy, add affordances).

**How it runs:**

1. Manager dispatches both reviewers in parallel with the **same** PR URL and **different** mandates.
2. Each reviewer runs independently — no peeking at the other's verdict mid-review.
3. Each posts a separate verdict comment on the PR.
4. Manager consolidates: agreement → ship; disagreement → surface the divergence to the human, don't auto-resolve.

**Why two lenses, not one:** a single reviewer drifts toward what they're best at. Two reviewers with locked mandates produce more total coverage than one generalist reviewer with both lenses.
