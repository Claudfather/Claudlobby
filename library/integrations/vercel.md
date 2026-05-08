# Vercel

**Skills:** `/vercel-status` (deployments, domains, env vars), `/vercel-logs` (view/debug logs), `/vercel-deploy` (deploy to production or preview)

**When to use:**
- Checking deployment health or domain config → `/vercel-status`
- Debugging build failures or runtime errors → `/vercel-logs`
- Deploying or promoting a preview → `/vercel-deploy`

**Gotchas:**
- Vercel CLI logged in as a specific user — check `vercel whoami` if auth issues arise
- Domain DNS propagation takes 5-30 min after adding — don't declare failure too early
- Preview URLs are per-commit; production URL is stable
