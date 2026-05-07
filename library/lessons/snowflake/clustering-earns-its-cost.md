---
title: Clustering Earns Its Cost
description: Clustering keys cost credits to maintain. They pay back only if queries actually prune by the key.
---

Snowflake's automatic clustering is a credit drain that runs in the background, re-organising micro-partitions to match the declared cluster key. It's worth it when downstream queries filter on the key and benefit from partition pruning. It's wasted when they don't.

**Before adding `cluster by (col)` to a model:**

1. Check `query_history` for the top 20 queries hitting this table.
2. For each, look at the WHERE clause. Does it filter on `col`?
3. Look at the EXPLAIN plan for those queries. Are partitions being scanned that the cluster key would prune?

If most queries already prune well (selective predicates on existing micro-partition organisation), clustering won't help — you're paying maintenance for no read-side gain.

**When clustering pays back:**

- High-cardinality filter column hit by many queries (chain_id, account_id, region).
- Range queries on a date/time column where loads are non-monotonic (clustering keeps the natural ordering).
- A consistently-joined column that becomes a hash-join build side after clustering reduces scan.

**Cluster-by post-hook pattern:**

For dbt incremental models, declare the cluster key as a post-hook rather than `cluster_by` in the config:

```sql
{{ config(
  post_hook="ALTER TABLE {{ this }} CLUSTER BY (col)"
) }}
```

This costs ≪0.01 credits per dbt run (the ALTER is metadata-only) and lets Snowflake's background re-clustering catch up between runs. It's strictly better than re-declaring `cluster_by` in every incremental insert.

**What to flag in review:**

- New `cluster_by` without a citation showing pruning gain → ask for `query_history` evidence.
- Cluster on `id` (high-cardinality but rarely a filter) → wasted maintenance.
- Cluster on a low-cardinality column (status, type) → defeats the purpose of micro-partitioning.
