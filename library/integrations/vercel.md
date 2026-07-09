---
title: Vercel
type: cli
---

# Vercel

**Skills (clauDNA):** `/claudna:vercel status` (deployments, domains, env vars), `/claudna:vercel logs` (view/debug logs), `/claudna:vercel deploy` (deploy to production or preview)

**When to use:**
- Checking deployment health or domain config → `/claudna:vercel status`
- Debugging build failures or runtime errors → `/claudna:vercel logs`
- Deploying or promoting a preview → `/claudna:vercel deploy`

**Gotchas:**
- Vercel CLI logged in as a specific user — check `vercel whoami` if auth issues arise
- Domain DNS propagation takes 5-30 min after adding — don't declare failure too early
- Preview URLs are per-commit; production URL is stable
