---
title: DigitalOcean
---

# DigitalOcean

**CLI:** `doctl` — droplets, apps, databases, domains.

**When to use:**
- Checking droplet health → `doctl compute droplet list`
- App platform deploys → `doctl apps list`, `doctl apps logs <id>`
- Database management → `doctl databases list`

**Gotchas:**
- Logged in to a specific team (check `doctl account get`)
- Droplet SSH keys must be pre-configured — `doctl compute ssh <name>`
- Maintenance windows may cause brief outages — check email notifications
