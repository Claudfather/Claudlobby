---
name: pressure-test
description: "Adversarial review of a plan or direction — challenges assumptions, applies first-principles thinking, surfaces edge cases, and asks whether we're extending something suboptimal."
argument-hint: "[plan text, file path, or PR/issue URL]"
---

# Pressure Test

Constructive adversarial review. Makes plans stronger before committing to a direction.

## Step 1: Read the Plan

Accept whatever the user provides:

- **File path** — Read the file.
- **PR URL** — `gh pr view <url> --json body,title,comments` to get the full PR context.
- **Issue URL** — `gh issue view <url> --json body,title,comments`.
- **Inline text** — Use the argument directly.

If the plan references code, **read the code** before challenging. Never speculate about what code does.

## Step 2: Challenge the Direction

Run all five lenses. No skipping.

### a) First Principles

Strip away inherited assumptions. If we were solving this problem from scratch today, would we arrive at this design? What constraints are we inheriting that may no longer apply?

### b) Optimality Check

Are we extending something suboptimal? Is this a local maximum — improving a design that should be replaced, not refined? Would a different foundation make the whole problem simpler?

### c) Edge Cases and Failure Modes

What breaks at scale? What breaks at zero? What happens when the happy path doesn't hold — timeouts, partial failures, concurrent access, empty inputs, adversarial inputs?

### d) Missing Perspectives

What stakeholder, use case, or constraint hasn't been considered? What would a skeptical user say? What would someone maintaining this in 6 months curse?

### e) Scope and Complexity

Is this over-engineered for the actual problem? Is it under-engineered for the real problem? Are we solving the stated problem or the actual problem?

## Step 3: Output

For each lens, output:

```
### <Lens Name>

**Challenge:** <1-2 sentences>
**Risk if ignored:** <1 sentence>
**Verdict:** RETHINK | ADJUST | PROCEED
```

- **RETHINK** — direction may be wrong, structural concern.
- **ADJUST** — direction is right, needs modification.
- **PROCEED** — no concern from this lens.

End with a Verdict section:

```
## Verdict

<one of:>
- "This plan has structural concerns. Recommend pausing to address: [list]"  (any RETHINK)
- "Direction is sound. Tighten: [list]"                                      (only ADJUST)
- "Plan holds up under scrutiny. Ship it."                                   (all PROCEED)
```

## Rules

- Do NOT rewrite the plan. Challenge it.
- Do NOT add scope. Question whether scope is right.
- Keep each lens to 3-5 sentences max. Density over length.
- Take positions. No hedging — say what you think and why.
- The goal is to make the plan stronger, not to block it. Challenge hard but propose alternatives.

$ARGUMENTS
