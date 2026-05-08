---
title: Auto-merge after peer review
description: Manager auto-merges PRs after a peer review verdict + CI green. Does not use --admin — requires GitHub branch protection to allow it.
---

# Auto-merge after peer review

The manager auto-merges PRs when ALL of:

1. **Peer review posted** — a reviewer has posted an `APPROVE` verdict (or `COMMENT` with `**Approve**` verdict line under same-identity fallback).
2. **CI green** — all required status checks pass.
3. **No conflicts** — mergeable state is `clean` or `unstable` (not `dirty`).

Merge command: `gh pr merge <n> --squash --delete-branch`

**Not auto-merged:**
- PRs with `Request Changes` verdict — bounce to engineer first.
- PRs with unresolved review threads.
- PRs where CI is failing or pending.
- PRs the manager authored (self-merge requires a second reviewer).

The manager posts "Merging #NN" to Telegram before executing, so the human has visibility.
