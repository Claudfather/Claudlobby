---
title: Verify before merge
description: Manager-side checks before merging a peer-reviewed PR
---

# Verify before merge

When a reviewer reports "DONE," that means they've FINISHED reviewing — not that they approved. Before merging, the manager must verify two things:

### 1. Parse the verdict

Read each reviewer's OWN latest verdict, never just the newest comment on the PR — reading only the newest comment can hide another reviewer's live block. `claudlobby --json task reviews OWNER/REPO --pr N` resolves each recorded reviewer's latest verdict: a PR stays blocked while ANY reviewer's own latest verdict is a block, whoever posted most recently. Look for the explicit header on each:

- `**[alex] [VERDICT] ship it** — reviewed at a1b2c3d` → safe to merge
- `**[alex] [VERDICT] request changes** — reviewed at a1b2c3d` → bounce to engineer with fix direction; do NOT merge

`alex`/`a1b2c3d` stand in for the reviewer's name and the commit sha they reviewed — check the sha against the PR's current head before trusting an old approval; `task reviews` marks a mismatch `COMMIT-STALE`. CI green + review completion is necessary but not sufficient. The verdict text and recorded review-role actor must agree. Make it a gated function: read verdict → if ship-it and all independent merge rungs pass, merge; else, bounce.

If a Request Changes verdict was missed and the PR merged, file a follow-up issue and dispatch the fix immediately.

### 2. Check for migration files

After every `gh pr merge`, scan the merged PR's file diff for migration files (`*.sql`, `/migrations/`, alembic, etc.). If any are present:

- Verify they ran on prod via deployment logs (look for migration runner output + specific migration name).
- If migrations did NOT run, surface IMMEDIATELY and dispatch manual application.
- Do NOT report the PR as "live" or "shipped to prod" until the migration is confirmed applied.

**Key principle:** idempotency is NOT the same as "applied." A migration sitting unapplied means new code may run against an old schema — silent data integrity failures or runtime errors. Never collapse `merged_at == applied_at`.
