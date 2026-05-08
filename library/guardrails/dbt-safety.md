---
title: dbt safety
---

# dbt safety

Without explicit human approval:

- **Never `dbt run` on prod.** Default `--target dev`.
- **Never `--full-refresh`** — recomputing incrementals can blow up warehouse cost and break downstream consumers.
- **Never `dbt seed --full-refresh`** — same risk on seeds.
- **Never run prod with uncommitted local changes** — commit + PR first.
- `dbt parse` before `dbt run` (catches compile errors cheaply).
- `dbt test` before `dbt run` when modifying existing models.

If a prod model fails, investigate on dev with the same source data.
