---
title: Branch off fresh main + verify diff scope
description: Every PR branches off freshly-pulled main; two-dot diff detects stale-base cargo, three-dot answers what the PR changes
---

# Branch off fresh main + verify diff scope

Every new PR must:

1. **Branch off fresh main** — `git checkout main && git pull --ff-only` immediately before `git checkout -b <branch>`. Catches up to peer work that landed since last touch.

2. **Check for stale-base cargo before push** — `git diff origin/main..HEAD --stat` (**two-dot**). Unrelated files here are silent-revert cargo from a stale base; rebase before push.

   **Read that output correctly, because it is not a list of your changes.** Two-dot compares main's *tip* against yours, so peer work that landed after you branched appears as a **deletion of a file you never touched**. That deletion *is* the stale-base signal this step exists to catch — not a fault in your branch.

   Use the form that matches the question:

   | Question | Form |
   |---|---|
   | Is my branch stale? | `git diff origin/main..HEAD` — **two-dot** |
   | What does my PR actually change? | `git diff origin/main...HEAD` — **three-dot** |

   Three-dot is merge-base semantics: what the platform will merge, and the honest answer to "what did I touch". Reading two-dot as though it answered that question makes a peer's commits look like your deletions.

   **Operand order matters as much as dot count.** `HEAD...origin/main` shows what *main* changed — a third question again, and an easy one to run by accident while checking the second.

When running parallel sprint tracks, also confirm `git log --oneline -5` includes recently-merged peer PRs by their merge SHAs.

This prevents a PR from silently reverting another team member's just-shipped work because it branched off an older main.
