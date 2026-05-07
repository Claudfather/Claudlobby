---
title: Incremental Models — Unique Key Discipline
description: Every incremental model needs a real unique key, an idempotent `is_incremental()` filter, and a chosen strategy.
---

Incremental dbt models are easy to write wrong in ways that pass tests for weeks before producing duplicates or missing data. Three discipline points catch most failures:

**1. Unique key must be real.**

`unique_key='id'` is wrong if `id` isn't unique across the table over time. Common gotchas:

- `id` is unique per source, but the model unions multiple sources → use `(source, id)`.
- `id` is unique now but historical re-syncs produce the same `id` for new event → use `(id, captured_at)`.
- The model has no natural key → generate a surrogate (`md5(coalesce(...))`) and document why.

**2. `is_incremental()` filter must be idempotent.**

```sql
WHERE event_time > (SELECT max(event_time) FROM {{ this }})
```

This is wrong if late-arriving data with `event_time < max` exists — those rows never re-enter. Better:

```sql
WHERE event_time > (SELECT max(event_time) FROM {{ this }}) - INTERVAL '1 day'
```

The lookback handles late-arrival. Pair with `incremental_strategy='merge'` so the lookback rows merge cleanly instead of duplicating.

**3. Strategy choice is intentional, not default.**

- `merge` — when you have a real unique key + need de-dup. Most common, slightly slower.
- `append` — when every row is new (event streams) + no de-dup needed.
- `delete+insert` — when you want a partition replaced wholesale. Snowflake-friendly.
- `insert_overwrite` — partition overwrite (BigQuery, Spark). Snowflake doesn't support — flag in review.

**What to flag in review:**

- An incremental model without `unique_key` set → request changes.
- A `unique_key` that's a string column from source without explanation → ask if collisions are possible.
- An `is_incremental()` filter without a lookback when the source has late-arriving data → request the lookback.
- `incremental_strategy='insert_overwrite'` on Snowflake → wrong strategy for the warehouse.
