---
name: cost-benefit
description: "Use when a plan or implementation should be evaluated for return on investment. Assesses effort vs. value, opportunity cost, and whether the scope is right-sized. Applies to all PR types."
argument-hint: "[--dispatch]"
---

# Cost-Benefit

Your job is to assess whether the proposed work is worth doing at the proposed scope. Every plan has a cost (effort, complexity, maintenance burden) and a benefit (value delivered). Your task is to evaluate whether the ratio is favorable and whether the scope is right-sized.

## Procedure

### 1. Estimate Costs

Read the source material. Assess:

- **Implementation effort** — how much work is this, really? Check size estimates against scope described.
- **Maintenance burden** — what ongoing cost does this create? New code to maintain, new infrastructure to monitor, new processes to follow.
- **Complexity cost** — does this make the system harder to understand? Every abstraction, indirection layer, and new concept has a cognitive cost.
- **Opportunity cost** — what else could this time be spent on? Is this the highest-value use of the effort?

### 2. Estimate Benefits

- **Value delivered** — who benefits and how much? Is the benefit concrete and measurable, or vague and aspirational?
- **Unlocks** — does this enable future work that has clear value? Or is it speculative infrastructure?
- **Risk reduction** — does this reduce a real, quantified risk? Or a hypothetical one?

### 3. Apply Cost-Benefit Lenses

- **Scope right-sizing** — could 50% of the effort deliver 80% of the value? Flag phases or features that are expensive relative to their benefit.
- **YAGNI check** — is any part of this plan building for hypothetical future requirements? Flag speculative work.
- **Parallelization ROI** — if the plan has parallel phases, is there actually capacity to exploit the parallelism?
- **Cheaper alternatives** — for expensive phases, is there a simpler approach that achieves the same outcome?
- **Sunk cost** — is any part of this plan justified by "we already built X" rather than "X is the right foundation"?

### 4. Produce Findings

Write findings to the result path using the format specified by the dispatcher (see `result-format.md`). Quantify where possible — "Phase 3 is M-sized but delivers marginal value beyond what Phase 2 already provides" is better than "Phase 3 seems expensive."

### `--dispatch` Mode

When dispatched by `/ironclad`, operate non-interactively. Read the source path provided, write findings to the result path, and report back. Do not post to the PR, create issues, or prompt for input.
