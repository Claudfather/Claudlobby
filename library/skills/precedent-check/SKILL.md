---
name: precedent-check
description: "Use when a plan or implementation should be checked against prior decisions, existing patterns, and project history. Surfaces conflicts with ratified decisions, violated conventions, or repeated mistakes. Applies to all PR types."
argument-hint: "[--dispatch]"
---

# Precedent Check

Your job is to check whether the proposed change is consistent with prior decisions, established patterns, and project history. Surface conflicts, regressions, and repeated mistakes before they ship.

## Procedure

### 1. Gather Precedents

Read the source material. Then check:

- **Decision docs** — prior decisions and ADRs for this area, plus the repo's own `documentation/`.
- **Git history** — `git log --all --oneline --grep="<keywords>"` for prior attempts, reverts, and related changes.
- **Existing patterns** — how does the codebase currently handle similar concerns?
- **Open plans** — in-flight work that overlaps.

**Reach the fleet's docs through the door your Shared Documentation section names.** A vault-wired fleet queries Claudron; a fleet with a raw doc tree reads `shared/decisions/` and `shared/planning/active/` directly. Hand-opening the tree on a vault-wired fleet reads files the vault door exists to replace, and misses everything in the vault's other tiers (#1172).

### 2. Apply Precedent Lenses

- **Decision conflicts** — does the change contradict a ratified decision or ADR? If so, the decision must be explicitly revisited, not silently overridden.
- **Pattern violations** — does the change break an established convention without justification?
- **Regression risk** — has something similar been tried and reverted before? Check git history for signals.
- **Scope collision** — does this overlap with another in-flight plan or PR? Flag coordination risk.
- **Lesson violations** — does the change repeat a known mistake documented in `lessons/` or knowledge docs?

### 3. Produce Findings

Write findings to the result path using the format specified by the dispatcher (see `result-format.md`). Link to specific decision docs, commits, or patterns that form the precedent. Claims without evidence are not findings.

### `--dispatch` Mode

When dispatched by `/ironclad`, operate non-interactively. Read the source path provided, write findings to the result path, and report back. Do not post to the PR, create issues, or prompt for input.
