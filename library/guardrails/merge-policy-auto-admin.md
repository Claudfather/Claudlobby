---
title: Auto-merge with --admin after peer review
description: Manager auto-merges PRs using --admin after a real peer review verdict + CI green. For same-identity fleets where regular merge is blocked by branch protection.
---

The manager auto-merges PRs using `--admin` when ALL of:

1. **Peer review posted** — a reviewer has posted an `APPROVE` verdict, or a `COMMENT` with `**Approve**` verdict line (same-identity fallback). The review must be from a different bot than the PR author — no self-reviews.
2. **CI green** — all required status checks pass.
3. **No conflicts** — mergeable state is `clean` or `unstable` (not `dirty`).

Merge command: `gh pr merge <n> --squash --admin --delete-branch`

**Why --admin:** Same-identity fleets share one GitHub PAT. Branch protection's "required approvals" check counts only formal `APPROVE` state, which GitHub blocks for same-identity. `--admin` bypasses the branch protection gate — but the **real gate is the peer review verdict**, not GitHub's checkbox.

**Red lines (even with --admin):**
- Never `--admin` merge without an actual peer review on the PR.
- Never `--no-verify` — hooks exist for a reason.
- Never force-push main.
- Never merge a PR with `Request Changes` verdict outstanding.
- Never merge a PR where CI is failing.
- Never merge a PR the manager itself authored without a separate reviewer.

The manager posts "Merging #NN (--admin, reviewed by <reviewer>)" to Telegram before executing.

**This guardrail replaces both `merge-policy-human` and `no-merge-admin`.** Do not stack with either.
