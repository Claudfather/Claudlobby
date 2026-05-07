---
title: Consensus Before Escalating to the Human
description: The manager runs a consensus loop with workers before pinging the human for a decision.
---

A manager bot that pings the human at every fork is a manager that hasn't internalised the consensus loop. The loop exists precisely so the human doesn't get pinged for decisions that the fleet can resolve.

**The loop:**

1. Manager identifies a decision that needs a lens. Examples: which of two architectures, which of two timing strategies, whether to ship now or wait for the dependency.
2. Dispatch **two workers** with **non-overlapping mandates** (e.g., "evaluate from a perf angle" + "evaluate from a maintenance angle"). Same context, different lenses.
3. Each worker returns an opinion + reasoning. They do not collaborate.
4. Manager consolidates: if the lenses agree, decide. If they diverge, decide which lens is load-bearing for this decision and follow it.
5. Only escalate to the human if the divergence reveals a constraint the manager cannot resolve (e.g., "this depends on whether we're prioritising launch date or rev").

**What this prevents:**

- The human becoming the load balancer for fleet decisions.
- The fleet stalling on every decision it could have made.
- Worker monocultures — one worker's lens being treated as universal.

**What to flag if it's missing:**

- Manager pings human for a decision the fleet has the data to make → request the consensus loop first.
- Manager dispatches two workers with the **same** mandate (echo chamber) → request distinct lenses.
- Manager reports the consensus result without showing each worker's lens → ask for the divergence in the report, not just the verdict.

**The counter-rule:** for genuinely human-only decisions (priorities, hires, customer-facing tradeoffs), the consensus loop is not the right tool — escalate directly. Consensus loops are for technical decisions where the fleet has the context to reason.
