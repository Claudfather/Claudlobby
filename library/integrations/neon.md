---
title: Neon (Postgres)
type: cli
env_contract:
  NEON_API_KEY:
    description: Neon API key for neonctl CLI
    tier: fleet
---

# Neon (Postgres)


**Skills (clauDNA):** `/neon-info` (overview), `/neon-query` (run SQL), `/neon-branch` (create/list/delete branches)

**When to use:**
- Database schema exploration → `/neon-info`
- Running queries or checking data → `/neon-query`
- Safe experimentation before migrations → `/neon-branch` (branches are copy-on-write, cents per branch)

**Gotchas:**
- Always branch before destructive migrations — test on the branch, then apply to prod
- `neonctl` requires `--org-id` for org-scoped projects (configured per fleet in env)
- Connection strings use `?sslmode=require` — don't strip it
