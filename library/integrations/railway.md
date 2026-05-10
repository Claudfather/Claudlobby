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

# Railway


**Skills:** `/railway-status` (service overview), `/railway-logs` (view/debug logs), `/railway-deploy` (deploy/update services)

**When to use:**
- Checking deploy health or service status → `/railway-status`
- Debugging crashes or errors → `/railway-logs`
- Deploying or updating a service → `/railway-deploy`
