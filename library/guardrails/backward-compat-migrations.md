---
title: Backward-Compatible Migrations
description: Schema changes must roll out in steps that keep readers running through the transition.
---

# Backward-Compatible Migrations

A migration that breaks readers — whether dbt models, downstream syncs, or services querying the table — is a production incident dressed up as a PR. Schema changes that aren't backward-compatible go out in stages.

**The pattern:**

1. **Add the new column / table / shape.** Readers ignore it; writers populate both old and new.
2. **Migrate readers** one at a time to the new shape. Each migration is its own PR with its own rollback path.
3. **Stop populating the old shape** once all readers are off it.
4. **Drop the old shape** in a final PR, after a grace period (≥1 release cycle).

**What this prevents:**

- A column rename that breaks every model selecting `*` (yes, this happens).
- A type narrowing (VARCHAR → INTEGER) that fails on existing rows.
- A NOT NULL added without a default + backfill.
- A primary-key change that defeats incremental joins.

**If the change is genuinely simultaneous** (e.g., a one-shot data backfill that touches both shapes atomically), it can ship as a single PR — but the PR body must call out: "this migration is not backward-compat; coordinated downtime: <X minutes> at <Y time>."

Reviewers: any DDL touching a table with multiple readers needs the rollout plan in the PR body, or it gets request-changes.
