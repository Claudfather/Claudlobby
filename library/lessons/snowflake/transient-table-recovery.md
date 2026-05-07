---
title: Transient Table Time-Travel Recovery
description: Transient tables have a short Time Travel window (≤1 day). Zero-copy clone is the recovery path.
---

Snowflake's `TRANSIENT` tables (and dbt's incremental models materialised as TRANSIENT) skip Fail-safe and have a Time Travel retention of 0 or 1 day, depending on `DATA_RETENTION_TIME_IN_DAYS`. That's not "no recovery" — it's "recovery only if you act fast."

**The recovery pattern (zero-copy clone):**

```sql
-- within the retention window:
CREATE OR REPLACE TABLE recovered_table CLONE original_table
  AT (TIMESTAMP => '2026-04-25 14:30:00'::TIMESTAMP_NTZ);

-- or by query ID:
CREATE OR REPLACE TABLE recovered_table CLONE original_table
  BEFORE (STATEMENT => '01b3a2c4-...-query-id-...');
```

The clone is **zero-copy** — no storage cost, instant — until either side is modified, at which point only the diverged micro-partitions cost storage.

**When this applies:**

- A bad incremental model run dropped or duplicated rows. Clone to before the run, swap.
- A migration ALTER corrupted shape. Clone to before the ALTER, drop the bad version, rename the clone.
- A bad DELETE removed rows that should be there. Clone to before the DELETE, INSERT the missing rows back.

**The 1-day rule:**

For TRANSIENT tables (most dbt incremental models in Snowflake), the window is 1 day max. After that, the data is **gone** — Fail-safe doesn't apply. Recovery becomes "re-run from source," which for multi-billion-row models is a multi-hour incident.

**Discipline:**

- If a bad run is suspected, clone the pre-run state *immediately* into a backup table — even before diagnosis. Cloning is free; the window isn't.
- Document the clone in the incident notes so cleanup happens after recovery validates.
- For models large enough that re-run-from-source isn't viable, consider PERMANENT instead of TRANSIENT to extend the window.

**What to flag in review:**

- A new model materialised as TRANSIENT where downstream cannot tolerate a multi-hour recovery → ask whether PERMANENT is justified.
