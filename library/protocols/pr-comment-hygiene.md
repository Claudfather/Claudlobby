---
title: PR Comment Hygiene
description: Governs how /ironclad and review bots post structured findings, fork preferences, and verdicts to plan PRs. Ensures comments are parseable, threaded, and resolved before convergence.
---

# PR Comment Hygiene

When `/ironclad` dispatches review lenses to plan PRs, the resulting comments must be structured, threaded, and machine-parseable. This protocol defines the comment formats that bots use and that `/ironclad` reads to determine convergence.

## Thread Discipline

One top-level comment per finding. All follow-up discussion goes in-thread (GitHub reply), never as a new top-level comment.

- Each top-level comment addresses exactly one finding, one fork preference, or one verdict.
- Responses, rebuttals, and clarifications are replies to the original comment.
- A top-level comment that covers multiple unrelated findings must be split into separate comments.

This keeps the comment list scannable and enables `/ironclad` to parse, count, and resolve findings individually.

## Comment Formats

### Finding

A review lens bot posting a finding:

```
[<bot-name>] [<lens>] [<severity>] <finding summary>

<detail — 1-3 sentences of context, evidence, or recommendation>
```

**Fields:**

| Field | Values | Purpose |
|-------|--------|---------|
| `bot-name` | The posting bot's name (e.g., `alex`, `virgil`) | Traceability — who posted this |
| `lens` | The review lens used (e.g., `align-to-mission`, `extension-check`, `precedent-check`, `cost-benefit`) | Which lens produced the finding |
| `severity` | `critical` / `major` / `minor` / `info` | Impact on plan quality |

**Severity guide:**

| Severity | Meaning | Blocks convergence? |
|----------|---------|-------------------|
| `critical` | Plan cannot proceed without addressing this | Yes |
| `major` | Significant gap that weakens the plan | Yes |
| `minor` | Improvement opportunity, not blocking | No |
| `info` | Observation or context, no action needed | No |

Examples:

```
[alex] [extension-check] [major] Phase 2b proposes a new SkillValidator class, but scripts/validate-skills.py already provides this functionality.

Consolidate into the existing validator rather than building a parallel path. The existing script handles SKILL_CONTRACT.md validation, frontmatter checks, and CI integration.
```

```
[virgil] [align-to-mission] [minor] Phase 4d (documentation + changelog) is standard housekeeping — aligned but low mission-impact.

Consider deferring 4d until after the first real /ironclad cycle produces learnings worth documenting.
```

```
[mason] [cost-benefit] [info] Phase 2 skills are all M-sized and fully parallel — good ROI on parallelization if 3+ engineers are available.
```

### Fork Preference

A review bot expressing a preference on a decision fork:

```
[<bot-name>] [FORK F<N>] <option-letter> — <reasoning>
```

This is an opinion, not a lock. Only the designated ratifier locks forks (see `decision-fork-lifecycle` protocol).

Examples:

```
[alex] [FORK F1] (a) — clauDNA is the right home. The lenses are useful standalone — a developer should be able to run /align-to-mission without a fleet.

[virgil] [FORK F4] (a) — plan-only scope. Single responsibility keeps the convergence gate fast and predictable.
```

### Verdict

A review bot's overall assessment of the plan:

```
[<bot-name>] [VERDICT] <approve|request-changes|comment> — <one-line summary>
```

| Verdict | Meaning |
|---------|---------|
| `approve` | No critical or major findings. Plan is ready from this lens. |
| `request-changes` | Critical or major findings exist. Plan needs revision before this lens approves. |
| `comment` | Observations posted but no blocking judgment. |

Examples:

```
[alex] [VERDICT] request-changes — 2 major findings: parallel path in Phase 2b, missing rollback strategy.

[virgil] [VERDICT] approve — All phases align with PROJECT_MISSION.md. One minor deferral suggestion posted.

[mason] [VERDICT] comment — Cost-benefit analysis posted. No blocking issues, but Phase 3a is the highest-risk item.
```

A verdict is always the last comment a bot posts in a review cycle. Post findings first, verdict last.

## Resolution

When a finding is addressed (plan updated, fork locked, or author responds with a valid reason to keep as-is), the finding's top-level comment is updated by prepending `[RESOLVED]`:

```
[RESOLVED] [alex] [extension-check] [major] Phase 2b proposes a new SkillValidator class...
```

Rules:

- The plan author or `/ironclad` orchestrator prepends `[RESOLVED]` — not the original finding poster.
- Resolution means "addressed," not "agreed with." A valid response ("the existing validator doesn't support X, which is why we need a new class") counts as resolution.
- `[RESOLVED]` findings are skipped by `/ironclad` on re-scan. They don't block convergence.

## Convergence Check

`/ironclad` determines comment-level convergence by:

1. Scanning all top-level comments for the structured formats above
2. Counting unresolved findings by severity:
   - Any unresolved `critical` or `major` → **not converged**
   - Only unresolved `minor` or `info` → **converged** (minors are advisory)
3. Checking that every review lens dispatched has posted a `[VERDICT]`
4. Cross-referencing fork preferences against `decision-fork-lifecycle` state

A plan is comment-converged when:
- Zero unresolved `critical` or `major` findings
- All dispatched lenses have posted verdicts
- No verdict is `request-changes` without all its associated findings resolved

Comment convergence is one of two gates for `/ironclad` (the other is fork convergence from `decision-fork-lifecycle`). Both must pass.

## No Orphan Comments

Every top-level comment must reach a terminal state before convergence:

| Comment type | Terminal state |
|-------------|---------------|
| Finding (`critical`/`major`) | `[RESOLVED]` prepended |
| Finding (`minor`/`info`) | No action required — does not block |
| Fork preference | Fork locked via `decision-fork-lifecycle` |
| Verdict (`request-changes`) | All associated findings resolved, or new verdict posted |
| Verdict (`approve`/`comment`) | Terminal as posted |

`/ironclad` flags orphan comments (unresolved critical/major findings, request-changes verdicts with unresolved findings) in its convergence report.
