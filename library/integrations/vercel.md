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

**Preview builds, one rule for every site repo:** a pull request gets a Vercel preview only
when it changes what the site renders. Production deployments build exactly as before.
- **Never for a Dependabot branch** (`dependabot/…`): Vercel creates no deployment at all.
- **Never for a push that changes only paths no site renders,** counted from the branch's last
  built preview: CI and GitHub settings (`.github/`), tests, and the docs paths the repo names.
- **Everything else builds,** a branch's first push and whatever the gate cannot judge included.
- **To get a preview the gate skipped,** push an empty commit (`git commit --allow-empty`).

**To apply it to a repo,** copy `"$CLAUDLOBBY_LIBRARY_DIR/integrations/vercel_preview_gate.sh"`
unchanged into it and follow that script's header: two `vercel.json` keys, and the Vercel
project settings only its dashboard can change.

**Gotchas:**
- Vercel CLI logged in as a specific user — check `vercel whoami` if auth issues arise
- Domain DNS propagation takes 5-30 min after adding — don't declare failure too early
- Preview URLs are per-commit; production URL is stable
