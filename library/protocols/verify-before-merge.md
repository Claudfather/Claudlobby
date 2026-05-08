---
title: Verify before merge
description: Manager-side checks before merging a peer-reviewed PR
---

# Verify before merge

When a reviewer reports "DONE," that means they've FINISHED reviewing — not that they approved. Before merging, the manager must verify two things:

### 1. Parse the verdict

Read the latest review body and look for the explicit verdict:

- `**Verdict: Ship it**` → safe to merge
- `**Verdict: Request Changes**` → bounce to engineer with fix direction; do NOT merge

CI green + review completion is necessary but not sufficient. The verdict text is the authoritative signal. Make it a gated function: read verdict → if ship-it, merge; else, bounce.

If a Request Changes verdict was missed and the PR merged, file a follow-up issue and dispatch the fix immediately.

### 2. Check for migration files

After every `gh pr merge`, scan the merged PR's file diff for migration files (`*.sql`, `/migrations/`, alembic, etc.). If any are present:

- Verify they ran on prod via deployment logs (look for migration runner output + specific migration name).
- If migrations did NOT run, surface IMMEDIATELY and dispatch manual application.
- Do NOT report the PR as "live" or "shipped to prod" until the migration is confirmed applied.

**Key principle:** idempotency is NOT the same as "applied." A migration sitting unapplied means new code may run against an old schema — silent data integrity failures or runtime errors. Never collapse `merged_at == applied_at`.
