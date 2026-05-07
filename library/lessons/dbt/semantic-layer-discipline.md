---
title: Semantic Layer Discipline
description: Enforce single-source-of-truth metric definitions, decoupled dimension logic, and column-level rename discipline across the warehouse.
---

A semantic layer without discipline is just a bunch of SQL files with overlapping definitions. Three practitioners define "active_users" three different ways, and your BI tool is silently inconsistent.

**Metric definitions are contracts.** When you define a metric — "active_users = users with ≥1 event in the last 7 days" — that definition must exist exactly once. It lives in a metric declaration (dbt semantic model or `metrics.yml`), derives from documented models via explicit entities and measures, and is consumed everywhere it appears. No re-implementations. No "let me filter it differently in this dashboard."

**Single source of truth:** Define each metric once. Give it a grain (daily, weekly, all-time). Document the calculation and any caveats. Downstream references the metric, not a copy of the logic.

**Column-level lineage:** A renamed column breaks every downstream consumer silently unless you track the change. Before renaming:

1. Add the new name as an alias on the model (`col_new AS col_new, col_old AS col_old` — both exposed).
2. Ship a PR with both names.
3. Use lineage tools to enumerate all downstream references.
4. Update downstreams in batches across follow-up PRs.
5. Remove the old alias once all downstreams have migrated.

**Dimension decoupling:** Don't bake business logic into dimension tables. A dimension table should store attributes (identity, status, raw labels); derived classifications (is this a bot, is this an outlier) belong in a separate semantic layer view joined at query time. Label updates then don't trigger fact-table backfills — views recompute instantly.

**dbt contracts:** Contracts enforce schema stability on models downstream depends on. Use `config(contract={"enforced": true})` on stability-critical models (unions, aggregations, external-facing schemas), but not everywhere — contracts are a maintenance burden. Most internal-only tables use `on_schema_change="append_new_columns"` instead.

**Naming discipline:** If you rename a column and downstream still references the old name, the build passes silently but downstream gets NULL. Code-search isn't enough. Before renaming any column touched by a contracted model or consumed downstream: (1) add the alias, (2) coordinate updates in follow-up PRs, (3) remove the alias once downstreams are clear.

This sounds like overhead, but one silent rename-and-break costs more to debug than five PRs to coordinate the migration.
