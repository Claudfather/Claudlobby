---
title: dbt Parse-Time vs Execute-Time
description: Incremental predicates are locked at parse — runtime detection needs a bridge.
---

dbt compiles SQL at parse time. The values inside `{{ }}` Jinja blocks (vars, refs, env_var, this, target.name) are resolved when dbt parses the model graph, *before* any model has run. The resulting SQL is then executed.

**The trap:**

```sql
{% set changed_dims = run_query("SELECT id FROM staging WHERE updated_at > current_date - 1") %}
DELETE FROM {{ this }} WHERE dim_id IN ({{ changed_dims.rows | join(",") }})
```

This looks like "delete dims that changed today." It is not. `run_query` runs at parse time, which means `current_date - 1` is whichever day dbt was first invoked — *not* the day this model ran. If parse cache lasts across days (it does, in CI), the predicate is wrong.

**The general rule:** anything that needs to reflect runtime state must come from a SQL expression evaluated at execute time, not from Jinja evaluated at parse time.

**Common fixes:**

1. **Bridge table at execute time.** Write a session-scoped temp table in a pre-hook with the runtime values. Reference it in the model SQL: `WHERE dim_id IN (SELECT id FROM bridge_changed_dims)`.
2. **Subquery against source.** If the runtime decision is cheap to compute, embed it: `WHERE dim_id IN (SELECT id FROM staging WHERE updated_at > current_date - 1)`. Predicate evaluates at execute time.
3. **Avoid the pattern entirely.** Many "parse-time computes the predicate" cases are wrong from the start — refactor toward incremental_strategy=delete+insert with a materialized staging window.

**What to flag in review:**

- Any `run_query` that resolves data values used in DML predicates → parse-time landmine.
- Any Jinja that computes a date used as a filter → confirm it's intended-frozen, not runtime.
- Any incremental model where `is_incremental()` and `{{ this }}` interact with `run_query` → step through the parse-time vs execute-time order carefully.
