---
title: Same-Identity Comment Fallback
description: GitHub blocks approve/request-changes when reviewer shares identity with author — fall back to a verdict-bearing comment.
---

When the fleet shares a single GitHub PAT (every bot commits as the same identity), the GitHub API rejects `gh pr review --approve` and `gh pr review --request-changes` on PRs the same identity authored. A verdict you couldn't post is not a verdict.

**Fallback:**

1. If `--approve` or `--request-changes` returns "Can not approve your own pull request" or equivalent — post `--comment` instead.
2. Lead the comment body with the verdict: `**Approve**`, `**Request Changes**`, or `**Comment**` (no verdict).
3. The verdict line is the contract — the human merge gate reads it programmatically.

**Do not:**

- Use `--admin` to merge over the block. The human merges.
- Silently skip the review. Always post the verdict, even as comment.
- Edit the PR description to record the verdict — comments are searchable; descriptions get rewritten.

**Why this matters:** the merge policy is human-gated regardless. The verdict is signal for *the human reviewer*, not for GitHub's branch-protection. Comments carry the same signal.
