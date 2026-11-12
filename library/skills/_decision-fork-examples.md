---
title: Decision Fork Examples (Shared Partial)
description: The decision-fork-lifecycle protocol's state diagram, example lock and reopen comments, and sample Evidence fields. Reference only; the rules are in the protocol.
---

# Decision Fork Examples

Reference for the `decision-fork-lifecycle` protocol, which holds every rule; nothing here adds one. Each heading below is the protocol section its text illustrates.

## Fork States

```
open  ──►  leaning  ──►  locked (ratified)
  ▲                          │
  │                          ▼
  └──────── reopened ◄───────┘
```

## Ratification (Locking a Fork)

Examples:

```
[FORK-LOCK F1] (a) — clauDNA skills, claudlobby orchestration. Follows the /adversarial-review precedent and keeps lenses usable standalone.

[FORK-LOCK F3] (a) — PR-comment threading with resolved tags. Simpler than fingerprinting, leverages GitHub's existing model.
```

## Reopening a Locked Fork

Examples:

```
[FORK-REOPEN F4] Extension-check now also verifies file paths (PR #122), which changes the overlap analysis for plan-only vs plan-plus-codebase.

[FORK-REOPEN F2] The fleet now supports webhook-triggered dispatches, which makes push mode viable without persistent state.
```

## Evidence Trail

After locking:

```markdown
- **Evidence:** [Locked by ratifier](https://github.com/org/repo/pull/118#issuecomment-123456)
```

After reopen + re-lock:

```markdown
- **Evidence:** [Original lock](https://github.com/org/repo/pull/118#issuecomment-123456) → [Reopened](https://github.com/org/repo/pull/118#issuecomment-234567) → [Re-locked](https://github.com/org/repo/pull/118#issuecomment-345678)
```
