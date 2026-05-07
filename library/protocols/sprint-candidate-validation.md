---
title: Sprint candidate validation
description: Pre-dispatch checks for autonomous sprint issue selection
---

Before dispatching sprint work, validate every candidate through three gates:

### 1. Label gating

Filter out issues with gating labels before scoring or dispatching:

- **`requires-review`** — architectural bets, multi-week commitments, or cross-cutting changes that need the owner's direct direction. Never select autonomously, even if mission alignment is high.

Surface gated issues separately as "pending your direction" — they are NOT candidates.

When a lens recommendation surfaces a multi-week architectural bet, file it with `requires-review` by default. Let the owner untag if they want autonomous execution.

### 2. Freshness and dependency checks

For each remaining candidate, run two checks:

**Liveness check** — `gh issue view <N> --json state,closedAt`. Confirm OPEN. Handoff snapshots and backlog caches decay fast; issues may have been closed since the snapshot was written.

**Dependency check** — read the issue body for "depends on #M" / "blocked by #M" references. For each, run `gh pr view M --json state,mergeable,updatedAt`. Skip candidates with dependencies that are OPEN + CONFLICTING + stale (>3 days no update). Surface them separately as "blocked on stale PRs (skip until rebased)."

### 3. Existing work check

Before dispatching on a parent or bundle issue, ALWAYS `gh pr list --search "#<issue>"` first.

Previous sessions may have shipped PRs against sub-items without closing the parent. This leads to dispatching work that's already done.

- Search for matching PRs by issue number. If any are MERGED and recent, verify the remaining scope before dispatching.
- For parent issues with numbered sub-items, search for each sub-item too.
- If a dispatch turns out to be a no-op, close the stale issue with a breakdown comment.
