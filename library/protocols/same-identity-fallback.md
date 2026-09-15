---
title: Same-Identity Comment Fallback
description: GitHub blocks approve/request-changes when reviewer shares identity with author — fall back to a verdict-bearing comment.
---

# Same-Identity Comment Fallback

When the fleet shares a single GitHub PAT (every bot commits as the same identity), the GitHub API rejects `gh pr review --approve` and `gh pr review --request-changes` on PRs the same identity authored. A verdict you couldn't post is not a verdict.

**Fallback:**

1. If `--approve` or `--request-changes` returns "Can not approve your own pull request" or equivalent — post `--comment` instead.
2. Lead the comment body with the verdict: `**Approve**`, `**Request Changes**`, or `**Comment**` (no verdict).
3. The verdict line is the contract — the merge gate reads it programmatically.

**Do not:**

- Treat merging as a substitute for the verdict. The block is on the review *state*; it does not remove the obligation to post one.
- Silently skip the review. Always post the verdict, even as comment.
- Edit the PR description to record the verdict — comments are searchable; descriptions get rewritten.

**Why this matters:** the verdict is signal for whatever gates the merge — a person, or a manager acting under the fleet's merge guardrail — not for GitHub's branch-protection. That is why a comment carries the same signal as a formal review state: the gate reads the verdict line, not GitHub's approval field.
