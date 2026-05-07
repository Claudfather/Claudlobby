---
title: Stacked PR Squash Causes Duplication
description: Squash-merging a stacked PR (where child's base = parent's feature branch) produces commit duplication on rebase.
---

Stacked PRs — where PR-B is opened against PR-A's branch (not main) — work well for incremental review, but interact badly with squash-merge.

**The failure mode:**

1. PR-A is opened: `feature/A` ← `main`. Has 3 commits.
2. PR-B is opened on top: `feature/B` ← `feature/A`. Has 2 commits.
3. PR-A is squash-merged. Main now has 1 squashed commit `[A]`. The 3 original commits are *not* in main's history.
4. PR-B is rebased onto main to update.
5. The rebase replays PR-B's 2 commits *plus* the 3 original PR-A commits (which now look "missing" from main relative to `feature/B`).
6. Main ends up with `[A]` (the squash) **and** the 3 original commits (replayed from `feature/B`'s rebase). The history has the same code twice.

**Recovery:**

- Hard reset `feature/B` to main, cherry-pick PR-B's 2 commits, force-push.
- Or close PR-B, open a new PR with just the 2 commits against main.

**Prevention:**

- For stacked PRs, prefer **rebase-merge** for the parent (preserves the 3 original commits in main's history; the rebase of PR-B is a no-op for those commits).
- Or merge-commit the parent (preserves linearity around the parent merge).
- Or coordinate: merge PR-A first, rebase PR-B, then merge PR-B. Don't merge in the wrong order.

**What to flag in review:**

- A PR opened against a feature branch (not main) → confirm the merge plan with the author. If the parent will squash-merge, surface the duplication risk.
