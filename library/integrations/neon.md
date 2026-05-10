---
title: Neon (Postgres)
env_contract:
  NEON_API_KEY:
    description: Neon API key for neonctl CLI
    tier: fleet
---

# Neon (Postgres)


**Skills:** `/neon-info` (overview), `/neon-query` (run SQL), `/neon-branch` (create/list/delete branches)

**When to use:**
- Database schema exploration → `/neon-info`
- Running queries or checking data → `/neon-query`
- Safe experimentation before migrations → `/neon-branch` (branches are copy-on-write, cents per branch)
