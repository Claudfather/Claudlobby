---
title: Idempotency Mandatory
description: Every ingestion / pipeline asset must be re-runnable without duplication or corruption.
---

# Idempotency Mandatory

If a pipeline asset (Dagster asset, Modal function, dbt incremental, sync job) cannot be re-run safely, it is broken — even if it works the first time.

**Mandatory:**

- Re-running with the same inputs produces the same outputs. No duplicates. No drift.
- Re-running with new inputs merges cleanly with prior runs. No "first run vs subsequent run" branches in code.
- Late-arriving data lands in the correct partition (event-time, not ingest-time).
- Failure mid-run does not leave the asset in a corrupted state. Either the run completes or rolls back.

**Why:** every asset will be re-run. By the engineer debugging it. By the scheduler retrying after a network blip. By a backfill. By a human pushing the "run again" button. An asset that requires "be careful, only run this once" is a landmine waiting for the next on-call.
