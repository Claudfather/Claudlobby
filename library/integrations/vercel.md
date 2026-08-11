---
title: Vercel
type: cli
---

# Vercel

Vercel hosts and deploys frontend projects. You reach it either through a Vercel
domain skill or agent, or through the `vercel` CLI.

**What you will need to do with it:**
- Check deployment health, domains, and env-var config
- Read and debug logs from build failures and runtime errors
- Deploy to production, or promote a preview

**How to do it:** use any Vercel domain skill or agent you have available —
clauDNA ships `/claudna:vercel`, so check your own skill list first. If none is
installed, use the `vercel` CLI directly; everything above is reachable that way.

**Gotchas:**
- Vercel CLI logged in as a specific user — check `vercel whoami` if auth issues arise
- Domain DNS propagation takes 5-30 min after adding — don't declare failure too early
- Preview URLs are per-commit; production URL is stable
