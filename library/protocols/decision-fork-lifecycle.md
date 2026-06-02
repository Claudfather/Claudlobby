---
title: Decision Fork Lifecycle
description: Governs how decision forks in /forge plans progress from open to locked, including ratification, reopening, evidence trails, and convergence gating.
---

# Decision Fork Lifecycle

Decision forks are the explicit choice-points in a `/forge` plan where multiple viable approaches exist and a ratifier must lock a direction before implementation begins. This protocol governs how forks move through their lifecycle, how ratification is recorded, and how `/ironclad` determines convergence.

## Fork States

```
open  ──►  leaning  ──►  locked (ratified)
  ▲                          │
  │                          ▼
  └──────── reopened ◄───────┘
```

| State | Meaning |
|-------|---------|
| `open` | Fork exists, no preference expressed yet |
| `leaning` | The plan author has a recommended option (the "lean" in the fork definition), but no ratifier has committed |
| `locked` | A designated ratifier has committed to an option. The fork is resolved. |
| `reopened` | A reviewer has surfaced new evidence that challenges a locked fork. Returns to `open` pending re-ratification. |

Every fork starts as `open` when the plan is drafted. The author's `Lean:` field in the plan moves it to `leaning`. Only an explicit ratification comment locks it.

## Ratification (Locking a Fork)

The designated ratifier (specified in the fork's `Ratifier:` field) locks a fork by posting a PR comment:

```
[FORK-LOCK F<N>] <chosen option letter> — <rationale>
```

Examples:

```
[FORK-LOCK F1] (a) — clauDNA skills, claudlobby orchestration. Follows the /adversarial-review precedent and keeps lenses usable standalone.

[FORK-LOCK F3] (a) — PR-comment threading with resolved tags. Simpler than fingerprinting, leverages GitHub's existing model.
```

Rules:

- Only the designated ratifier (or the human, who can ratify anything) may lock a fork.
- The chosen option must match one of the fork's defined options — no ad-hoc fourth options in the lock comment. If a ratifier wants an unlisted option, they reopen the fork with new evidence and the plan author adds the option.
- The rationale is mandatory. A lock without reasoning is not a lock.

## Reopening a Locked Fork

Any reviewer can reopen a locked fork by posting a PR comment with new evidence:

```
[FORK-REOPEN F<N>] <new evidence or changed assumption>
```

Examples:

```
[FORK-REOPEN F4] Extension-check now also verifies file paths (PR #122), which changes the overlap analysis for plan-only vs plan-plus-codebase.

[FORK-REOPEN F2] The fleet now supports webhook-triggered dispatches, which makes push mode viable without persistent state.
```

Rules:

- Reopening requires new information — not a re-argument of the original position. "I still think (b) is better" is not a reopen; "the dependency we assumed in (a) was removed in PR #145" is.
- A reopened fork returns to `open`. The previous lock is historical context, not binding.
- The plan author updates the fork's `Status:` to `open` and adds the new evidence to the `Evidence:` field.

## Evidence Trail

Every state transition is recorded:

| Transition | Evidence |
|-----------|----------|
| `open` → `leaning` | The `Lean:` field in the plan document |
| `leaning` → `locked` | The `[FORK-LOCK]` PR comment (link to comment) |
| `locked` → `reopened` | The `[FORK-REOPEN]` PR comment (link to comment) |
| `reopened` → `locked` | A new `[FORK-LOCK]` PR comment |

The plan document's `Evidence:` field for each fork accumulates links to all relevant comments. After locking:

```markdown
- **Evidence:** [Locked by ratifier](https://github.com/org/repo/pull/118#issuecomment-123456)
```

After reopen + re-lock:

```markdown
- **Evidence:** [Original lock](https://github.com/org/repo/pull/118#issuecomment-123456) → [Reopened](https://github.com/org/repo/pull/118#issuecomment-234567) → [Re-locked](https://github.com/org/repo/pull/118#issuecomment-345678)
```

## Convergence Rule

A plan is **ironclad** (ready for implementation) when:

1. All decision forks are in `locked` state
2. No `[FORK-REOPEN]` comments exist without a subsequent `[FORK-LOCK]`
3. All forks have evidence links in the plan document

`/ironclad` determines fork state by reading PR comments:

1. Scan all comments for `[FORK-LOCK F<N>]` and `[FORK-REOPEN F<N>]` patterns
2. For each fork, the most recent matching comment determines state
3. A fork with no `[FORK-LOCK]` comment is `open` (or `leaning` if the plan has a `Lean:`)
4. A fork whose most recent comment is `[FORK-LOCK]` is `locked`
5. A fork whose most recent comment is `[FORK-REOPEN]` is `open`

## Interaction with Other Protocols

- **pr-comment-hygiene** — fork preference comments from review bots use the `[<bot-name>] [FORK F<N>]` format defined there. These are opinions, not locks. Only the ratifier's `[FORK-LOCK]` comment changes state.
- **plan-synthesis** — when multiple review lenses express conflicting fork preferences, the synthesis protocol governs how the conflict surfaces to the ratifier.
- **report-back** — workers reporting on `/ironclad` cycles include fork state (`forks_open: N, forks_locked: N`) in structured results.
