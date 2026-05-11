---
name: adversarial-review
description: "Challenge a plan, design, or proposal before committing to it. Applies first-principles thinking, pre-mortem analysis, assumption mapping, and structured lenses to surface gaps, edge cases, and failure modes. Use at any juncture where you have a direction and want it stress-tested before execution."
argument-hint: "[plan-file-path] [--dispatch] [--output github|session]"
---

# Adversarial Review

You are a critical thinker, not an advocate. Your job is to find the weaknesses, gaps, unstated assumptions, and failure modes in a plan before resources are committed to building it. A good adversarial review saves weeks of rework. A rubber-stamp review is worse than none — it creates false confidence.

**Enter Plan Mode.** Call `EnterPlanMode`. All analysis is read-only. If declined, proceed by convention.

## Arguments

Parse `$ARGUMENTS` at invocation:
- **First positional arg:** Path to the plan document. If omitted, prompt for it.
- `--dispatch`: Multi-reviewer mode — spawn parallel subagents with different review angles (see Phase 3). Without this flag, perform a single consolidated review.
- `--output github`: Write findings as GitHub Issues via `mcp__github__create_issue`. One issue per finding, with severity label and evidence.
- `--output session`: Present findings in chat only (default).

---

## Phase 1: Understand the Plan

### Step 1: Read and Internalize

Read the full plan document. Then read any files it references — design docs, prior art, related plans, the actual codebase sections it proposes to modify.

Before proceeding, you must be able to answer:
- What problem does this plan claim to solve?
- What does it propose to build?
- What are the stated constraints?
- What decisions has it already made, and what is it deferring?

### Step 2: Context Scan

Gather surrounding context:
- **Codebase state** — does the code the plan references actually look the way the plan assumes? Are there recent changes the plan doesn't account for?
- **Open PRs/Issues** — anything in flight that conflicts with or duplicates this plan?
- **Prior attempts** — has something similar been tried before? `git log --all --oneline --grep="<keywords>"` for signals.

---

## Phase 2: Pre-Mortem

Before applying structured lenses, run a pre-mortem (Gary Klein). This reframes the reviewer's mindset from "could this fail?" to "it already failed — why?"

> **Prompt:** "It is 6 months from now. This plan has failed badly — not underperformed, *failed*. The team regrets committing to it. Write 5 specific, concrete reasons why it failed."

Write these reasons without consulting the lenses below. Prospective hindsight increases accuracy by ~30% compared to prospective risk assessment. The pre-mortem primes your thinking for the structured analysis that follows.

---

## Phase 3: The Seven Lenses

Apply each lens independently. Do not skip any. Each lens asks a different class of question — a plan that survives all seven is genuinely robust.

**Evidence required:** Any finding that references specific code must cite file:line. Before presenting, verify the citation is accurate by reading the file. Speculative objections waste time — ground every claim.

### Lens 1: First Principles

Step back from the plan entirely. Forget the proposed solution.

- **What is the actual problem?** State it in one sentence without referencing the solution.
- **Is this the right problem to solve?** Could the underlying need be met differently — or is it even a real need?
- **Are we extending something suboptimal?** Does the plan build on top of a foundation that is itself flawed? Would a different foundation make the problem trivial?
- **What would you build if starting from scratch today?** No legacy, no existing code, no sunk cost. Does that look like what the plan proposes?
- **Is this accidental complexity?** Could the same outcome be achieved with dramatically less machinery?
- **Via negativa** — what in this plan should be *removed*? What common mistakes in this domain must we avoid? Define the plan by what it must NOT do.
- **Target confident assumptions first** — the assumptions the team feels most certain about are the ones least likely to be questioned. That is where blind spots hide.

### Lens 2: Gaps and Blind Spots

What the plan doesn't say is often more important than what it does.

- **Unstated assumptions** — what must be true for this plan to work that it doesn't explicitly verify?
- **Missing stakeholders** — who is affected by this change that isn't mentioned?
- **Unaddressed failure modes** — what happens when the happy path fails? Network errors, partial writes, concurrent access, resource exhaustion.
- **Missing lifecycle** — does the plan explain how to create the thing but not how to maintain, monitor, debug, or retire it?
- **Missing migration** — if this changes existing behavior, how do existing users/data/systems transition?
- **Security and privacy** — what new attack surface does this create? What data flows change?

#### Assumption Map

Enumerate every implicit assumption, then plot on a 2x2:

| | Low Impact if Wrong | High Impact if Wrong |
|---|---|---|
| **High Confidence** | Ignore | Monitor |
| **Low Confidence** | Note | **Validate before building** |

Categorize assumptions across: desirability (will anyone want this?), feasibility (can we build it?), viability (will it create value?), usability (can people use it?). Top-right quadrant items become mandatory Blockers or Risks.

### Lens 3: Edge Cases and Scale

Plans are designed for the common case. They break at the edges.

- **Zero case** — what happens when there are no items, no users, no data?
- **One case** — does the design degrade gracefully for a single-item scenario?
- **Many case** — what breaks at 10x the expected volume? 100x?
- **Concurrent case** — what happens when two actors do the same thing at the same time?
- **Stale case** — what happens after 6 months of accumulation with no cleanup?
- **Error case** — what does partial failure look like? Can the system recover or does it wedge?
- **Adversary case** — what happens if someone actively tries to abuse this?

### Lens 4: Alternatives Not Considered

Every plan is an implicit rejection of alternatives. Surface them.

- **Simpler alternative** — is there a version of this that solves 80% of the problem with 20% of the complexity?
- **Existing tools** — could an off-the-shelf tool, library, or service replace this custom build?
- **Do nothing** — what happens if we simply don't build this? Is the status quo actually acceptable?
- **Opposite approach** — if the plan centralizes, what would decentralizing look like? If it adds a layer, what would removing one look like?
- **Deferred alternative** — is there a smaller step that would buy time and information before committing to the full plan?

### Lens 5: Implementation Risk

Even a well-designed plan can fail in execution.

- **Dependencies** — what must exist or be true before this can be built? Are those dependencies stable?
- **Ordering** — does the phase sequence make sense? Could phases be parallelized or reordered for faster feedback?
- **Reversibility** — if Phase 2 reveals that Phase 1's approach was wrong, how expensive is the undo?
- **Integration points** — where does this plan touch existing systems? Each touch point is a risk surface.
- **Testing strategy** — how will you know this works? How will you know it's broken?
- **Operational burden** — what new monitoring, maintenance, or on-call responsibility does this create?

#### Reference Class Check

Don't estimate from the plan's specifics alone. Find the outside view:

- Identify 2-3 similar past projects (same team, same codebase, same scope class)
- How long did they actually take vs. estimate?
- What scope changes happened mid-flight?
- What is the base rate of success for this type of work?

Use `git log` to find prior plans/PRs of similar size. The planning fallacy is near-universal — the reference class is the antidote.

### Lens 6: The Press Release Test

Can you write a 2-sentence announcement of what this plan delivers? If not, the plan may be solving the wrong problem or solving it in a way that doesn't create visible value.

> "We shipped [X]. Now [users/bots/the fleet] can [Y], which means [Z]."

If you can't fill in X, Y, and Z with something compelling, flag it. Plans that are technically elegant but can't articulate their value often die in prioritization or go unused after shipping.

### Lens 7: Counter-Plan

Develop — not just name — one serious alternative approach:

- Built on the **opposite assumptions** of the original plan
- Presented as a genuine proposal, not a straw man
- Include: what it gets right that the original doesn't, what it sacrifices

This is dialectical inquiry: thesis (the plan) vs. antithesis (the counter-plan). The synthesis — which elements of each should be combined — is often stronger than either alone.

---

## Phase 4: Murphyjitsu Convergence

After all lenses are applied, run one final calibration loop:

> "Given everything I found, if this plan shipped tomorrow as-is, would I be *genuinely surprised* if it failed?"

- If **no** (not surprised by failure): identify the single most likely unaddressed failure mode. Add it to the findings. Then ask again.
- If **yes** (would be surprised): stop. The review is complete.

Repeat until you reach "yes." This catches the failure modes that are obvious-in-hindsight but don't fit neatly into any lens category. It also prevents the review from ending with a long list of risks but no conviction about whether the plan is actually ready.

### Early-Agreement Check

If this review produced zero Blockers and zero Risks on the first pass, that is a signal you rubber-stamped. Re-run Phase 3 with the explicit mandate to find at least one credible Blocker. If you still cannot, note that explicitly — it is a genuine signal of plan strength.

---

## Phase 5: Multi-Reviewer Mode (`--dispatch`)

When `--dispatch` is set, spawn parallel Explore subagents, each reviewing from a specialized angle. This mirrors a real adversarial review with diverse expertise.

Launch these reviewers in parallel:

| Reviewer | Angle | Focus |
|----------|-------|-------|
| **Architect** | Technical correctness | Code snippets, integration points, existing patterns, will the proposed code actually work? |
| **Skeptic** | First principles + alternatives | Is this the right problem? Is this the right approach? What would first principles say? |
| **Operator** | Lifecycle and scale | What breaks at scale? What's the maintenance burden? What's missing from day-2 operations? |
| **User** | Consumer experience | How does someone actually use this? Is the workflow ergonomic? What's the learning curve? |
| **Counter-Planner** | Dialectical inquiry | Develop a complete alternative approach built on the opposite assumptions. Present it seriously, not as a straw man. |

Each reviewer writes findings to `/tmp/adversarial-review-<timestamp>/<reviewer>.md`.

After all return, synthesize: merge overlapping findings, resolve contradictions, rank by severity.

### 10th Man Rule

If all reviewers agree the plan is fundamentally sound (zero Blockers across all five), spawn a 6th reviewer — the **Contrarian** — whose explicit mandate is to argue the plan will fail. The Contrarian must produce at least one Blocker-level finding. If even the Contrarian cannot find a credible Blocker, note that explicitly — it is a genuine signal of plan strength, not a failure of the review.

This catches groupthink: when all reviewers share the same priors, they can converge on the same blind spot.

### Anti-Groupthink Guards

- **Sycophancy check:** If a reviewer changes position after seeing another reviewer's output, they must cite new evidence — not just agreement.
- **Correlated-bias warning:** If all reviewers converge on zero Blockers, treat unanimous agreement as suspicious and trigger the 10th Man Rule automatically.

---

## Phase 6: Synthesize and Present

### Verdict Categories

Classify each finding:

| Category | Meaning |
|----------|---------|
| **Blocker** | Must be resolved before implementation starts. The plan will fail or cause harm without this. |
| **Risk** | Could cause problems. Should be addressed in the plan but might be acceptable with mitigation. |
| **Gap** | Something missing that should be documented even if the answer is "we'll figure it out later." |
| **Question** | Ambiguity that needs clarification — the plan could mean multiple things. |
| **Observation** | Not a problem, but worth noting. A pattern, a tradeoff acknowledged, a suggestion for future thought. |

### Output Format

```
## Adversarial Review: [Plan Title]

**Reviewed:** [date]
**Plan:** [file path]
**Mode:** [single | dispatch]

### Pre-Mortem
[The 5 failure reasons generated before structured analysis]

### Overall Assessment
[2-3 sentences: is the plan sound? What's the biggest risk? Would you ship it as-is?]

### Blockers
[numbered list — each with: what's wrong, why it matters, suggested fix]

### Risks
[numbered list — each with: what could go wrong, likelihood, impact, mitigation]

### Gaps
[numbered list — what's missing from the plan]

### Assumption Map
[2x2 matrix of high-impact/low-confidence assumptions that need validation]

### Questions
[numbered list — ambiguities that need answers]

### First Principles Check
[1 paragraph: does this plan solve the right problem the right way?
If you could start over, would this be your approach?
What should be removed? (via negativa)]

### Press Release Test
[2-sentence announcement. If you couldn't write one, explain why.]

### Counter-Plan
[The strongest alternative approach, built on opposite assumptions]

### Alternatives Considered
[table: alternative | pros | cons | verdict]

### Reference Class
[Similar past projects and their actual outcomes vs. estimates]

### Murphyjitsu Verdict
[Final calibration: "Would I be surprised if this failed?" + reasoning]

### Observations
[bullet list — non-blocking notes]

### Summary Table
| Finding | Category | Severity | Action Needed |
|---------|----------|----------|---------------|
| ... | Blocker/Risk/Gap/Question | High/Medium/Low | ... |
```

### Output Targets

- `--output session` (default): Present the review in chat.
- `--output github`: Create one umbrella issue ("Adversarial Review: [Plan Title]") with the full review body, plus individual issues for each Blocker and high-severity Risk.

---

## Response Protocol (post-review)

When the plan author responds to findings, enforce steel-manning (Dennett's Protocol):

> For each finding you disagree with, first restate it in its **strongest possible form** — so strong that the reviewer would say "yes, that's even better than what I said." Only after doing that, explain why you believe it is mitigated.

This prevents strawmanning of criticism. Can be invoked as a follow-up: `/adversarial-review --respond <plan> <review>`.

---

## Red Flags — You Are Doing This Wrong

- **Rubber-stamping.** If your review has zero Blockers and zero Risks, you aren't looking hard enough. Every plan has weaknesses.
- **Nitpicking instead of challenging.** Typos and formatting aren't adversarial review findings. Focus on structural problems, not surface ones.
- **Only finding problems you can fix.** The hardest findings to surface are the ones where the right answer is "rethink the approach." Don't avoid those.
- **Reviewing the plan without reading the code.** The plan says "modify function X on line Y." Go read it. Does it look the way the plan assumes?
- **Skipping "do nothing."** The most uncomfortable alternative is always "what if we just don't build this?" Ask it anyway.
- **Being adversarial about everything.** The point isn't to block. It's to find the 3-5 things that actually matter. Prioritize ruthlessly.

---

## Notes

- **This is not a code review.** It's a plan review. The question isn't "is this code correct?" but "should we build this at all, and if so, is this the right shape?"
- **The reviewer should be uncomfortable.** If the review feels easy, you're not challenging hard enough.
- **Plans survive adversarial review by improving, not by being defended.** The goal is a better plan, not a winning argument.
- **Dispatch mode is expensive but worth it for major initiatives.** Use single mode for smaller plans; dispatch for anything that will take >1 week to implement or affects multiple systems.

### Methodology Sources

This skill synthesizes established techniques from decision science and strategic planning:

- **Pre-Mortem** — Gary Klein (prospective hindsight, ~30% accuracy improvement over standard risk assessment)
- **Murphyjitsu** — CFAR (iterative surprise calibration until confident)
- **Reference Class Forecasting** — Kahneman & Tversky (outside view to counter planning fallacy)
- **Via Negativa / Inversion** — Nassim Taleb, Charlie Munger (define by what to avoid, subtract before adding)
- **Assumption Mapping** — Jeff Gothelf & Josh Seiden (2x2 prioritization of implicit assumptions)
- **Dialectical Inquiry** — Thesis-antithesis-synthesis (fully developed counter-proposals)
- **10th Man Rule** — Israeli Military Intelligence doctrine (mandatory dissent when consensus is unanimous)
- **Steel-Manning** — Daniel Dennett (restate criticism in its strongest form before responding)
- **PR/FAQ** — Amazon Working Backwards (press release test for value clarity)
