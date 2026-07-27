---
title: Never Full-Refresh in Prod
description: "`--full-refresh` destroys incremental state — irrecoverable on multi-billion-row models."
---

# Never Full-Refresh in Prod

dbt's `--full-refresh` flag drops and rebuilds the target table. For an incremental model that has been growing for months, this is hours of compute, terabytes of read, and any data that's no longer reproducible from source (deletes, late-arriving merges, hand-fixes) is gone.

**Never run `--full-refresh` against prod targets.** This includes:

- `dbt run --full-refresh` against `--target prod`
- Any CI/CD job parameterised with `full_refresh=true` for prod
- Any manual rebuild "to clean things up"

**If the incremental is broken**, the right fix is:

1. Diagnose which rows are wrong (a SELECT, not a DDL).
2. Patch with a targeted DELETE + re-incremental, or a one-off backfill model.
3. Document the fix in the PR body.

**If the schema needs to change**, use a migration model + swap pattern:

1. Build the new shape as a separate model.
2. Validate row counts and key invariants match.
3. Swap the alias in a single transaction.

A `--full-refresh` against prod is never the right answer. If a senior engineer thinks it is, the human approves it explicitly per-run.
