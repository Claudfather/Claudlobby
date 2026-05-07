---
title: Consensus Decision-Making
---

When a worker pauses with multiple viable approaches, **triage first** before bouncing the question up.

**Triage:**

- **Sub-choice resolvable by codebase patterns or weighing** (hook placement, helper extraction) → tell worker to `/weigh-development-paths` and decide. No consensus.
- **Architectural fork with multiple defensible picks** (response shape, source-of-truth, state pattern) → run consensus loop.
- **Product-shape / shared-infra / data-model / external-cost** (cron vs session trigger, Snowflake DDL, Railway deploy) → flag the human.

**The loop:**

1. Park the asker — no implementation yet.
2. Dispatch a second opinion to a worker whose domain covers the same code. Same option set, **without** revealing the first lean.
3. Compare against the four lenses: best practice / future-proof / elegant / codebase-consistent.
   - **Convergent** → ship it; note consensus in the PR.
   - **Divergent** → synthesize against the lenses + cited evidence; document why in the thread.
   - **Stuck** → flag the human with both verdicts compiled, not the raw option list.
4. Log the decision trail on the issue or PR, not in tmux.

**Expand the panel** for unusually load-bearing forks (schema decisions, cross-surface patterns) — pull a third worker before going to the human.

**Worker offers A/B/C fork to manager:** default to this consensus loop. The worker's choice to escalate rather than `/weigh-development-paths` and decide themselves is itself a signal that the fork is complex enough to warrant a second reader. Solo-weighing on the manager's side replaces "one person weighs" with "a different one person weighs" without adding independent signal. The extra dispatch round is cheap compared to mid-implementation reversals.

For substrate-shaping decisions needing 2-4 independent lenses, use `multi-angle-orchestration` instead.
