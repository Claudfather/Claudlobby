---
title: Branch off fresh main + verify diff scope
description: Every PR branches off freshly-pulled main; verify diff contains only intended scope before push
---

Every new PR must:

1. **Branch off fresh main** — `git checkout main && git pull --ff-only` immediately before `git checkout -b <branch>`. Catches up to peer work that landed since last touch.

2. **Verify diff scope before push** — `git diff origin/main..HEAD --stat` must show ONLY the intended scope. If unrelated files appear, those are silent-revert cargo from a stale-base branch and must be dropped via rebase before push.

When running parallel sprint tracks, also confirm `git log --oneline -5` includes recently-merged peer PRs by their merge SHAs.

This prevents a PR from silently reverting another team member's just-shipped work because it branched off an older main.
