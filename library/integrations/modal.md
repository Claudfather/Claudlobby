---
title: Modal
type: cli
---

# Modal

**Skills (clauDNA):** `/claudna:modal status` (workspace overview), `/claudna:modal logs` (view/debug logs), `/claudna:modal deploy` (deploy apps)

**When to use:**
- Checking deployed apps and containers → `/claudna:modal status`
- Debugging function failures → `/claudna:modal logs`
- Deploying or updating a Modal app → `/claudna:modal deploy`

**Gotchas:**
- Modal apps are serverless — cold starts are normal, not failures
- Secrets and volumes are workspace-scoped — ensure the right workspace is active
- `modal deploy` is idempotent — safe to re-run
