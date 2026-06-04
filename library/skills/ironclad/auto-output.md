---
title: Ironclad --auto Output Schema
description: Structured JSON output emitted in machine-consumable mode
---

# Ironclad --auto Output Schema

When invoked with `--auto`, `/ironclad` suppresses interactive output and emits structured JSON to stdout:

```json
{
  "skill": "ironclad",
  "outcome": "converged | not-converged | failed",
  "artifacts": {
    "pr_url": "<url>",
    "pr_type": "plan | implementation | mixed",
    "review_cycle": "<N>",
    "findings_posted": true,
    "lenses_completed": ["adversarial-review", "..."],
    "lenses_failed": [],
    "forks_open": "<count>",
    "forks_locked": "<count>",
    "converged": true
  },
  "summary": "<one-line summary>",
  "next": "<recommended next action>",
  "errors": [],
  "blocker_description": null
}
```

## Outcome Values

- `converged` — PR hardened. For plan PRs, ready for `/implement-plan`. For implementation PRs, ready for merge.
- `not-converged` — findings posted but open items remain. Human re-invokes.
- `failed` — could not complete (no workers, PR fetch failed, etc.).

`forks_open` / `forks_locked` are only populated for plan and mixed PRs. For implementation PRs, both are `0`.
