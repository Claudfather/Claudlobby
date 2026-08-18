---
title: Neon (Postgres)
type: cli
env_contract:
  NEON_API_KEY:
    description: Neon API key for neon CLI
    default_tier: fleet
    secret: true
---

# Neon (Postgres)


Neon is serverless Postgres. You reach it either through a Neon domain skill or
agent, or through the `neon` CLI (plus `psql` for raw SQL).

**What you will need to do with it:**
- Explore database schema
- Run queries and check data
- Branch before risky work — branches are copy-on-write and cost cents, so
  experiment on one rather than on prod

**How to do it:** use any Neon domain skill or agent you have available —
clauDNA ships `/claudna:neon`, so check your own skill list first. If none is
installed, use the `neon` CLI directly; everything above is reachable that way.

**Gotchas:**
- Always branch before destructive migrations — test on the branch, then apply to prod
- `neon` requires `--org-id` for org-scoped projects (configured per fleet in env)
- Connection strings use `?sslmode=require` — don't strip it
