---
title: Plan Synthesis
description: Governs how findings from multiple review lenses are merged into actionable results — dedup, conflict resolution, severity aggregation, iteration limits, and partial coverage handling.
---

# Plan Synthesis

When a plan passes through multiple review lenses (e.g., `/ironclad` cost-benefit, align-to-mission, risk, feasibility), findings must be synthesized into a single actionable set of results. Raw per-lens output is noise; synthesis is the signal.

## Dedup

Different lenses often surface the same underlying issue from different angles. Merge duplicates into one finding:

1. After all lenses complete, scan findings for overlapping targets (same file, same phase, same requirement).
2. When two or more lenses flag the same issue, collapse into a single PR comment (or thread) that cites **both lenses** and their reasoning.
3. Prefer the more specific framing. If cost-benefit says "phase 3 adds 2 weeks" and feasibility says "phase 3 depends on an unproven integration," the combined finding leads with the feasibility detail and appends the cost impact.

## Conflict Resolution

Lenses will disagree. When they do:

1. Post **both positions in a single thread** — not separate comments. Structure as:
   - `[CONFLICT] <topic>`
   - **Lens A** says: ...
   - **Lens B** says: ...
   - **Overlap:** what both agree on (if anything).
2. Tag the human for resolution. Do not auto-resolve conflicts between lenses — the whole point of multiple lenses is that each has legitimate weight.
3. If one lens recommends cutting scope and another says the scope is mission-critical, surface the tension explicitly. Never silently side with one lens.

## Severity Aggregation

A finding's severity is the **maximum** across all lenses that flagged it:

| Lens A   | Lens B | Result   |
|----------|--------|----------|
| critical | major  | critical |
| major    | minor  | major    |
| critical | —      | critical |

Severity levels (descending): **critical**, **major**, **minor**, **info**. If any lens considers a finding critical, it stays critical. A lens that rates it lower simply didn't weight the dimension that made it critical. The conservative default protects against blind spots.

## Iteration Limit

Multi-lens review can loop indefinitely as each cycle surfaces new findings. Cap it:

- **Maximum 3 `/ironclad` cycles** before escalating to the human.
- Each cycle's output is tagged with its cycle number: `[CYCLE 1]`, `[CYCLE 2]`, `[CYCLE 3]`.
- If cycle 3 still surfaces new critical-severity findings, escalate with: `[CYCLE 3 — ESCALATING] New critical findings still emerging after 3 cycles. Human review required.`
- Between cycles, only re-review findings that were revised or newly introduced — don't re-run the full lens set against unchanged content.

## Partial Coverage

Not every lens will complete successfully. Timeouts, tool failures, and context limits happen. Handle gracefully:

1. **Always declare coverage.** Every synthesis output starts with a coverage block:
   ```
   [SYNTHESIS] Lenses completed: cost-benefit, feasibility
   [SYNTHESIS] Lenses missing: align-to-mission (timed out), risk (context limit)
   ```
2. **Never claim full synthesis when lenses are missing.** If any lens failed, the synthesis is partial — say so explicitly.
3. **Proceed with what you have** — partial synthesis is better than no synthesis. But flag the gap so the human knows which angles were not covered.
4. If more than half the lenses failed, escalate rather than synthesize. The signal-to-noise ratio is too low.

## Output Format

The final synthesis is a single structured comment or document section:

```
[SYNTHESIS] [CYCLE N] Lenses: <completed> | Missing: <failed>

### Findings

1. **<Finding title>** (severity: critical) — lenses: cost-benefit, feasibility
   <Merged description>

2. **<Finding title>** (severity: minor) — lenses: align-to-mission
   <Description>

### Conflicts (human resolution required)

- [CONFLICT] <topic> — <Lens A> vs <Lens B>. See thread.

### Recommendations

<Ordered list of actions, referencing findings by number>
```
