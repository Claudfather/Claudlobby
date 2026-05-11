---
title: Modal
type: cli
---

# Modal

**Skills (clauDNA):** `/modal-status` (workspace overview), `/modal-logs` (view/debug logs), `/modal-deploy` (deploy apps)

**When to use:**
- Checking deployed apps and containers → `/modal-status`
- Debugging function failures → `/modal-logs`
- Deploying or updating a Modal app → `/modal-deploy`

**Gotchas:**
- Modal apps are serverless — cold starts are normal, not failures
- Secrets and volumes are workspace-scoped — ensure the right workspace is active
- `modal deploy` is idempotent — safe to re-run
