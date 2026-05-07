---
title: Root Cause, Not Symptom
description: A fix that makes the alert stop without explaining the layer it lived in is a future incident.
---

The pull to "make the alert green again" is strong. Re-run the failed sync, force-refresh the model, increase a timeout. The dashboard goes green. The team moves on. Three weeks later, the same alert fires, and nobody remembers what was tried last time.

**Root-cause discipline:**

- Trace the failure upstream until you find the **first** layer where the data was wrong, not just stale.
- Cite that layer in the fix PR body: "Layer 3 — the incremental predicate evaluated at parse time, not execute time, so the bridge dim change wasn't reflected. Fix: switched to subquery against staging."
- The fix lives in the layer where the cause lives. A Layer 3 fix in a Layer 4 PR is symptom-treatment.

**What to flag in review:**

- "Re-ran the job and it worked" → ask which layer was wrong; treat the re-run as a symptom mask.
- "Increased the timeout" → ask why the underlying op is slow; timeouts hide perf regressions.
- "Added a NULL filter" → ask why NULL is in the data; filters hide data integrity issues.
- "Manually backfilled the missing rows" → ask why the rows went missing; backfills hide ingestion bugs.

**The dual rule:** silent coverage is worse than the original problem. A test that only catches the symptom but not the cause is a decoy that gives false confidence — the cause re-emerges in a different shape and the silent test doesn't fire.

**When symptom-treatment is correct:**

- The cause is genuinely external (vendor outage, RPC node down) — flag-and-wait is the right move.
- The cause is identified, the long-fix is in flight, the symptom-fix unblocks downstream — but the PR body must say so explicitly: "temp workaround for #X; long-fix in #Y."

Otherwise: trace, cite, fix the layer.
