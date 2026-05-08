---
title: Never Merge with --admin
description: Admin-bypass merges defeat branch protection — and the human's merge gate.
---

# Never Merge with --admin

Branch protection rules exist for a reason: required reviews, status checks, signed commits, linear history. `gh pr merge --admin` bypasses all of them. Bots never use `--admin`. Reviewers never use `--admin`. Even managers never use `--admin`.

**The merge button belongs to the human.** Bots prepare the merge — passing tests, verdicts posted, conflicts resolved — and stop. The human clicks merge.

**If a merge is blocked**, do not bypass. Diagnose:

- Required reviewer hasn't posted? Ping them.
- Status check failing? Fix the failure.
- Conflicts? Rebase or coordinate.

`--admin` is a confession that one of those steps got skipped. The blocked state is the system working correctly.
