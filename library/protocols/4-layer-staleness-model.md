---
title: 4-Layer Staleness Model
description: Classify a data alert by which layer the root cause lives in, before fixing.
---

Data alerts often present as "Layer 4 broken" (downstream sync stale, dashboard wrong) when the root cause is upstream. Fixing the symptom — re-running the sync, force-refreshing the model — leaves the cause in place and the alert returns. Classify before fixing.

**The four layers:**

1. **Layer 1 — External source.** The upstream API, vendor feed, or chain RPC went down or changed shape. *Fix lives outside our infra.* Flag to a human; document the vendor incident; pause downstream until external recovery.

2. **Layer 2 — Extraction / ingestion.** Our pipeline that pulls from the source is broken: idempotency violation, schema-drift handling, late-arriving-data window misconfigured. *Fix lives in the ingestion layer* (Dagster asset, Modal function, scraper).

3. **Layer 3 — Transformation / dbt.** Our models that run over ingested data are broken: incremental predicate parse-vs-execute mismatch, missing test, contract violation, dimension key mismatch. *Fix lives in dbt models or tests.*

4. **Layer 4 — Sync / consumer.** The downstream sync (Snowflake → Postgres, Snowflake → BI tool) is broken. *Fix lives in the sync config or the consumer.*

**How to classify:**

Walk the lineage upstream from the alert. The first layer where the data is wrong (not just stale) is the root layer. Cite that layer in the fix PR body: `Root layer: 3 (dbt) — incremental predicate detected dim change at runtime, model used parse-time predicate.`

**Why this matters:** unclassified fixes treat symptoms. The same alert returns. PRs that name the layer let the next responder pattern-match in seconds.
