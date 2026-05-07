---
title: Human owns the merge button
---

The fleet does not auto-merge PRs. Even with engineer-completed + reviewer-approved + CI-green, the PR sits awaiting human merge. The manager may post "PR #123 ready to merge" — that's it.

Configurable exceptions (per-fleet opt-in via separate guardrail variant): trivial doc-only PRs in marked repos, dependency bumps with full CI green.

Default = human merges. No override without explicit fleet-config opt-in.
