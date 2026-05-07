---
title: Railway
env_contract:
  RAILWAY_API_TOKEN:
    description: Railway API token (work workspace)
    tier: fleet
  RAILWAY_PERSONAL_TOKEN:
    description: Railway API token (personal workspace)
    tier: fleet
---

### Railway

**Skills:** `/railway-status` (service overview), `/railway-logs` (view/debug logs), `/railway-deploy` (deploy/update services)

**When to use:**
- Checking deploy health or service status → `/railway-status`
- Debugging crashes or errors → `/railway-logs`
- Deploying or updating a service → `/railway-deploy`

**Gotchas:**
- Multiple tokens possible: `RAILWAY_API_TOKEN` (work) and `RAILWAY_PERSONAL_TOKEN` (personal) — use the right one for the target workspace
- `No linked project found` → run from the project directory or `railway link` first
- If Railway returns an auth or link error, report it verbatim — never fabricate deploy state
- A truthful "Railway auth broken, could not confirm deploy" beats a hallucinated status
