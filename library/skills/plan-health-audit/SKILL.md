---
name: plan-health-audit
description: "Use when a plan PR needs structural quality review. Checks phasing, dependencies, validation strategy, risk coverage, and completeness. Applies to plan PRs and mixed PRs."
argument-hint: "[--dispatch]"
---

# Plan Health Audit

Your job is to assess the structural quality of a plan — not whether the idea is good (that's first-principles), but whether the plan is well-constructed enough to execute. A good idea with a bad plan still fails.

## Procedure

### 1. Read the Plan Structure

Read the source material. Identify: phases, dependencies, decision forks, risks, validation strategy, complexity estimates, and sequencing.

### 2. Apply Health Lenses

- **Phasing** — are phases properly sequenced? Could any be parallelized? Are dependencies between phases explicit?
- **Dependency completeness** — does every external dependency have a risk level? Are "Low" ratings justified?
- **Validation strategy** — does every deliverable have a verification criterion? Are the criteria testable, not aspirational?
- **Risk coverage** — does every risk have a mitigation? Are there obvious risks the plan doesn't mention?
- **Fork completeness** — does every decision fork have options, a lean, a ratifier, and evidence? Are there implicit decisions masquerading as assumptions?
- **Size estimates** — are complexity ratings (S/M/L) consistent with the described scope? Is an M-rated phase actually L-sized?
- **Self-audit quality** — does the plan's adversarial self-audit section catch real issues, or is it a rubber stamp?
- **Completeness** — is anything underspecified enough that an implementer would have to guess?

### 3. Produce Findings

Write findings to the result path using the format specified by the dispatcher (see `result-format.md`). Reference specific phases, forks, or sections by name.

### `--dispatch` Mode

When dispatched by `/ironclad`, operate non-interactively. Read the source path provided, write findings to the result path, and report back. Do not post to the PR, create issues, or prompt for input.
