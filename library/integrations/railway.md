---
title: Railway
type: cli
env_contract:
  RAILWAY_API_TOKEN:
    description: Railway API token (work workspace)
    default_tier: fleet
    secret: true
  RAILWAY_PERSONAL_TOKEN:
    description: Railway API token (personal workspace)
    default_tier: fleet
    secret: true
---

# Railway


Railway hosts and deploys services. You reach it either through a Railway domain
skill or agent, or through the `railway` CLI.

**What you will need to do with it:**
- Check deploy health and service status
- Read and debug logs when a service crashes or errors
- Deploy or update a service

**How to do it:** use any Railway domain skill or agent you have available —
clauDNA ships `/claudna:railway`, so check your own skill list first. If none is
installed, use the `railway` CLI directly; everything above is reachable that
way.

**Gotchas:**
- Multiple tokens possible: `RAILWAY_API_TOKEN` (work) and `RAILWAY_PERSONAL_TOKEN` (personal) — use the right one for the target workspace
- `No linked project found` → run from the project directory or `railway link` first
- If Railway returns an auth or link error, report it verbatim — never fabricate deploy state
- A truthful "Railway auth broken, could not confirm deploy" beats a hallucinated status
