---
title: Dimension-First dbt Architecture
description: Treat each dimension as a folder with its full lifecycle (seeds, SCD2, time-travel views), not a single overlapping file.
---

Legacy dbt architectures often have one big file per "thing" — `dim_chains.sql` with hardcoded entries, `dim_tokens.sql` with overlapping rows from multiple sources. This breaks down at scale: late-arriving entities, slowly-changing attributes, and historical analysis all need code that the single-file pattern doesn't support.

**Dimension-first treats each dim as a folder:**

```
models/dimensions/<entity>/
├── seeds/                # static reference data (CSV in dbt seeds/)
├── stg_<entity>.sql      # staging from source(s)
├── int_<entity>__scd2.sql  # SCD2 history (optional)
├── dim_<entity>.sql      # current view (point-in-time = latest)
└── views/
    ├── vw_<entity>_current.sql      # current state
    ├── vw_<entity>_as_of.sql        # parameterised time-travel
    └── vw_<entity>_enriched.sql     # joined with attributes
```

**What this gives you:**

- **Late-arriving rows** land in seeds/staging without disturbing existing dim rows.
- **Slowly-changing attributes** track via SCD2 — historical analyses replay correctly.
- **Time-travel** is a view, not a re-run. `vw_<entity>_as_of` takes a date param.
- **Enrichment** layers stack as views. Downstream models pick the level they need.

**What to flag in PR review:**

- A new dim added as a single overlapping file → request changes; ask for the folder shape.
- A dim that mutates rows in place (no SCD) when downstream uses historical → request SCD2.
- A dim that hardcodes entries instead of seeds → request the seed.

This pattern doesn't apply universally — for one-off fact tables or genuinely simple lookups, a single file is fine. But for any entity that downstream models join on a time dimension, dimension-first prevents a class of bugs that have no good post-hoc fix.
