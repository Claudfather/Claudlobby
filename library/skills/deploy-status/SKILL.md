---
name: deploy-status
description: "Check deployment health across Vercel, Railway, DigitalOcean, and Neon. Shows service status, recent deploys, and failures."
argument-hint: "[vercel|railway|digitalocean|neon|all] [<project-name>]"
---

# Deploy Status

Unified deployment health across all platforms you deploy to. Populate the project lists below with your own services before installing.

## Platforms & Projects

### Vercel
Use the `vercel` CLI. Ensure it's on PATH (via Homebrew, npm global, or similar).

Projects under `<FLEET_ORG>`: `<VERCEL_PROJECTS>` *(e.g., `<api-backend>, <data-dashboard>, <frontend-app>, <secondary-frontend>`)*

```bash
vercel ls --token=$VERCEL_TOKEN 2>&1 | head -30
```

For a specific project:
```bash
vercel ls <project-name> --token=$VERCEL_TOKEN 2>&1 | head -20
```

### Railway
Query via the GraphQL API (`me.workspaces.projects`, NOT `me.projects`). You may have one or multiple tokens for different workspaces.

**Workspace token(s) in `~/claudlobby/.env.shared` or `~/.env`:**
```bash
source ~/claudlobby/.env.shared   # or wherever you keep secrets
curl -s -X POST https://backboard.railway.com/graphql/v2 \
  -H "Authorization: Bearer $RAILWAY_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "{ me { workspaces { id name projects { edges { node { id name services { edges { node { id name } } } } } } } } }"}' | python3 -m json.tool
```

*(Populate the projects list below with your own Railway services once you've wired them up.)*

Known projects & services under `<FLEET_ORG>`:
- `<project-a>`: service list
- `<project-b>`: service list

### DigitalOcean
Use the `doctl` CLI (team: `<DO_TEAM>`).

```bash
doctl apps list --format ID,Spec.Name,ActiveDeployment.Phase,UpdatedAt 2>&1
```

### Neon (Databases)
```bash
neonctl projects list --org-id <NEON_ORG_ID> --output json 2>&1 \
  | python3 -c "import sys,json; [print(f\"{p['name']}: {p['current_state']}\") for p in json.load(sys.stdin)]"
```


## Operations

### 1. Full Status (default)

Check all platforms in parallel:
- Vercel: recent deployments, any failures
- Railway: service status via GraphQL API
- DigitalOcean: app status
- Neon: database health

### 2. Platform-specific

When the user says `/deploy-status vercel` — only check Vercel.

### 3. Project-specific

When the user says `/deploy-status <project>` — find it across platforms and show details.

## Output Formatting

See [_telegram-formatting.md](../_telegram-formatting.md) for Telegram output formatting rules.

Send via `mcp__plugin_telegram_telegram__reply` to chat_id `$TELEGRAM_GROUP_CHAT_ID` with `format: "markdownv2"`.

```
🚀 *DEPLOY STATUS*

*Vercel*
━━━━━━━━━━━━
✅ <project-a> — deployed 2h ago
✅ <project-b> — deployed 4h ago

*Railway*
━━━━━━━━━━━━
✅ <project-a> \(N services\) — 29m ago
✅ <project-b> — pipeline \+ api 12h ago
💤 <project-c> — sleeping

*DigitalOcean*
━━━━━━━━━━━━
✅ <app-a> — active

🗄️ *Neon*
━━━━━━━━━━━━
✅ All N databases healthy

⚠️ *Issues*
━━━━━━━━━━━━
• <service> has no deployment
```

## Instructions

1. Run all platform checks in parallel (multiple Railway tokens in parallel if applicable)
2. Highlight failures, errors, or unhealthy services at the top
3. Show last deploy time for each service
4. Group Railway results by workspace if you use multiple
5. Skip platforms with no projects if checking all
6. For Railway, handle token auth issues gracefully — note which workspace needs re-auth

$ARGUMENTS
