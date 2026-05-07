---
title: Never Merge Your Own PR
description: Authors do not merge. Reviewers do not merge. The human merges.
---

The author of a PR has the strongest motivation to ship and the weakest visibility into what they got wrong. Merging your own PR collapses the review gate to a self-rationalisation.

**The rule:**

- A bot does not merge a PR it authored, even if reviews approve and checks pass.
- A bot does not merge another bot's PR. Reviewers post verdicts, not merges.
- The human merges. Always.

**Bot responsibilities ahead of merge:**

- Author: tests pass, conflicts resolved, PR body has summary + verification, reviewers tagged.
- Reviewer: verdict posted (Approve / Request Changes / Comment) with traced evidence.

**Then stop.** The PR is ready; the human clicks merge on their own schedule, with their own context (release windows, cross-PR coordination, downstream awareness).

This rule pairs with `merge-policy-human` and `no-merge-admin` — together they make the merge step the one place a human always touches the system.
