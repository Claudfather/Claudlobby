---
title: Railway
type: cli
env_contract:
  RAILWAY_PERSONAL_TOKEN:
    description: >-
      Railway ACCOUNT token. Bound to the account, so it answers `me` and
      sees every workspace the account can reach. Use for account-level
      queries and as the general-purpose token.
    default_tier: fleet
    secret: true
  RAILWAY_PERSONAL_PROJECT_TOKEN:
    description: >-
      Railway WORKSPACE token. Scoped to a workspace, so it answers
      `projects` but CANNOT answer `me` — that is a property of the token
      kind, not a fault. Use for project and deploy operations.
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
- **Two tokens, and the axis is SCOPE, not which workspace.** `RAILWAY_PERSONAL_TOKEN` is an ACCOUNT token: it answers `me` and reaches every workspace. `RAILWAY_PERSONAL_PROJECT_TOKEN` is a WORKSPACE token: it answers `projects` and **cannot** answer `me` — not a fault, a property of the kind. Reading the split as work-vs-personal is what led to a workspace token being probed with `me` and declared dead.
- **A rejected Railway token still returns HTTP 200.** The refusal rides in the body as a GraphQL `errors` entry. Anything checking only the status code will call a dead token healthy — measured on three tokens, every failure was a 200.
- `No linked project found` → run from the project directory or `railway link` first
- If Railway returns an auth or link error, report it verbatim — never fabricate deploy state
- A truthful "Railway auth broken, could not confirm deploy" beats a hallucinated status
