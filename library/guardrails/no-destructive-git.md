---
title: No destructive git operations
---

Without explicit human confirmation in the same turn, never:

- `git push --force` (any branch)
- `git reset --hard` (against unpushed work)
- `git branch -D <branch>`
- `git clean -fdx`
- `git checkout .` / `git restore .` (discard uncommitted)
- `git rebase -i` (unsupported in non-interactive sessions)

Unfamiliar state may be the human's in-progress work. Investigate before sweeping.
