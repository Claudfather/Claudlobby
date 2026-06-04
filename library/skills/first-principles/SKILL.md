---
name: first-principles
description: "Use when a plan or proposal should be challenged from first principles before committing. Asks whether this is the right problem, the right approach, and whether accidental complexity has crept in. Applies to plan PRs and mixed PRs."
argument-hint: "[--dispatch]"
---

# First Principles

Strip away the proposed solution and examine the foundation. Your job is to determine whether the plan solves the right problem the right way — or whether it's building on flawed assumptions.

## Procedure

### 1. Understand the Problem

Read the source material (plan document or PR diff). Before looking at the solution, state the problem in one sentence without referencing the proposed approach.

### 2. Apply First-Principles Lenses

- **Is this the right problem?** Could the underlying need be met differently, or is it even a real need?
- **Are we extending something suboptimal?** Does the plan build on a flawed foundation? Would a different foundation make the problem trivial?
- **What would you build from scratch?** No legacy, no sunk cost. Does that resemble what the plan proposes?
- **Accidental complexity?** Could the same outcome be achieved with dramatically less machinery?
- **Via negativa** — what should be *removed* from this plan? Define success by what it must NOT do.
- **Confident assumptions** — the assumptions the team feels most certain about are the least likely to be questioned. That's where blind spots hide. Target them first.

### 3. Produce Findings

Write findings to the result path using the format specified by the dispatcher (see `result-format.md`). Each finding must cite specific evidence from the source material. Speculative objections waste time — ground every claim.

### `--dispatch` Mode

When dispatched by `/ironclad`, operate non-interactively. Read the source path provided, write findings to the result path, and report back. Do not post to the PR, create issues, or prompt for input.
