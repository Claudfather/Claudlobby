# MCP Server Integrations Guide

Every bot connects to external services via MCP (Model Context Protocol) servers configured in `.mcp.json`. This guide covers setup for each integration.

## Core Integrations

### GitHub

Access repos, PRs, issues, code search. The most common integration — nearly every bot needs it.

```json
{
  "github": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "env": {
      "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_your_token"
    }
  }
}
```

**Setup:** GitHub Settings → Developer Settings → Personal Access Tokens → Generate. Scopes: `repo`, `read:org`, `read:user`.

### Notion

Task management, databases, kanban boards. See [notion-integration.md](integrations/notion-integration.md) for full guide.

```json
{
  "notion": {
    "command": "npx",
    "args": ["-y", "@notionhq/notion-mcp-server"],
    "env": {
      "NOTION_TOKEN": "ntn_your_integration_token"
    }
  }
}
```

**Setup:** [notion.so/profile/integrations](https://www.notion.so/profile/integrations) → New integration → copy token. Share target pages with the integration.

### Gmail + Calendar (via `gws.json` / workspace-mcp)

Read, search, draft, and send emails. Calendar events, free/busy, reminders. Uses the `workspace-mcp` tool, registered as server name `gws` in the MCP fragment `library/mcp/gws.json`.

> **Note:** The older `gmail.json` fragment has been removed. `gws.json` bundles both Gmail and Calendar in a single server instance.

```json
{
  "gws": {
    "command": "uvx",
    "args": ["workspace-mcp", "--tools", "gmail", "calendar"],
    "env": {
      "GOOGLE_OAUTH_CLIENT_ID": "your_client_id.apps.googleusercontent.com",
      "GOOGLE_OAUTH_CLIENT_SECRET": "GOCSPX-your_secret",
      "WORKSPACE_MCP_CREDENTIALS_DIR": "/home/user/.google_workspace_mcp/my-email/credentials",
      "USER_GOOGLE_EMAIL": "you@yourdomain.com",
      "WORKSPACE_MCP_PORT": "8000"
    }
  }
}
```

**Setup:**
1. Google Cloud Console → Create OAuth Client (Desktop type)
2. First run triggers OAuth flow — open URL in browser (use SSH tunnel for headless Pi)
3. Credentials saved to `WORKSPACE_MCP_CREDENTIALS_DIR`

**Multiple accounts:** Use different ports and credential dirs for each:
```
Account 1: port 8000, ~/.google_workspace_mcp/personal/credentials
Account 2: port 8001, ~/.google_workspace_mcp/work/credentials
Account 3: port 8002, ~/.google_workspace_mcp/business/credentials
```

**Google Calendar** is included in the `gws` server above — add `"calendar"` to `--tools`. No separate MCP entry needed.

### Slack

Read channels, post messages, reply in threads, mark as read.

```json
{
  "slack": {
    "command": "slack-mcp-server",
    "args": ["--transport", "stdio"],
    "env": {
      "SLACK_MCP_XOXP_TOKEN": "xoxp-your-token",
      "SLACK_MCP_ADD_MESSAGE_TOOL": "true",
      "SLACK_MCP_MARK_TOOL": "true"
    }
  }
}
```

**Setup:** Create a Slack app with appropriate scopes (`channels:history`, `chat:write`, etc.) and install to workspace. Copy the user token (`xoxp-...`).

**Use cases:** Monitor alert channels, reply to threads, post status updates.

## E-Commerce Integrations

### Shopify

Orders, products, customers, inventory. Essential for any e-commerce bot.

```json
{
  "shopify": {
    "command": "npx",
    "args": ["-y", "@ajackus/shopify-mcp-server"],
    "env": {
      "SHOPIFY_ACCESS_TOKEN": "shpat_your_admin_token",
      "SHOPIFY_STORE_DOMAIN": "yourstore.myshopify.com"
    }
  }
}
```

**Setup:** Shopify Admin → Settings → Apps → Develop apps → Create app → Configure Admin API scopes (`read_orders`, `read_products`, `read_customers`, `read_inventory`).

### Printify

Print-on-demand fulfillment, product management, order tracking.

```json
{
  "printify": {
    "command": "npx",
    "args": ["-y", "printify-mcp"],
    "env": {
      "PRINTIFY_API_KEY": "your_printify_token",
      "PRINTIFY_SHOP_ID": "your_shop_id"
    }
  }
}
```

**Setup:** Printify → Settings → Connections → Generate Personal Access Token. Get shop ID via `curl -s 'https://api.printify.com/v1/shops.json' -H 'Authorization: Bearer YOUR_TOKEN'`.

## Smart Home & IoT

### Home Assistant

Control lights, switches, sensors, automations. Requires HA running on the same network.

```json
{
  "homeassistant": {
    "command": "uvx",
    "args": ["hass-mcp"],
    "env": {
      "HA_URL": "http://localhost:8123",
      "HA_TOKEN": "your_long_lived_access_token"
    }
  }
}
```

**Setup:** HA dashboard → Profile → Security → Long-Lived Access Tokens → Create.

### Docker

Manage containers on the Pi — list, start, stop, logs, images.

```json
{
  "docker": {
    "command": "uvx",
    "args": ["mcp-server-docker"]
  }
}
```

**Setup:** User must be in `docker` group: `sudo usermod -aG docker $USER`. No token needed — uses local Docker socket.

## Productivity

### Spotify

Playback control, search, playlists, queue management.

```json
{
  "spotify": {
    "command": "uv",
    "args": ["--directory", "/path/to/spotify-mcp", "run", "spotify-mcp"],
    "env": {
      "SPOTIFY_CLIENT_ID": "your_client_id",
      "SPOTIFY_CLIENT_SECRET": "your_client_secret",
      "SPOTIFY_REDIRECT_URI": "http://127.0.0.1:8080/callback"
    }
  }
}
```

**Setup:** [developer.spotify.com](https://developer.spotify.com) → Create app → copy Client ID/Secret. First run triggers OAuth.

### Granola (Meeting Transcripts)

Access meeting notes and transcripts from Granola.

```json
{
  "granola": {
    "command": "npx",
    "args": ["-y", "mcp-remote", "https://mcp.granola.ai/mcp", "--port", "8002"]
  }
}
```

**Setup:** Requires Granola account. The MCP remote connection handles auth.

## DevOps / Infrastructure

These are CLI-based integrations, not MCP servers — there's no `library/mcp/*.json` fragment for any of them. Each ships a set of clauDNA skills plus a token env-contract instead of an interactive CLI login: a bot is headless, so there's no browser around to complete an OAuth flow in. List the service under the bot's `integrations:` key in `fleet.yaml` (it isn't auto-paired via `mcp:` the way GitHub/Notion/etc. are) — see [library/integrations/README.md](../library/integrations/README.md) for the full mechanism.

### Vercel

Deployments, domains, environment variables.

**Skills:** `/claudna:vercel-status` (deployments, domains, env vars), `/claudna:vercel-logs` (view/debug logs), `/claudna:vercel-deploy` (deploy to production or preview).

**Setup:** the host's Vercel CLI needs to already be logged in as the right user (`vercel whoami` to check) — this is a one-time host-level login, not a per-bot token. See [library/integrations/vercel.md](../library/integrations/vercel.md).

### Railway

Services, deployments, environments, logs.

**Skills:** `/claudna:railway-status` (service overview), `/claudna:railway-logs` (view/debug logs), `/claudna:railway-deploy` (deploy/update services).

**Setup:** set `RAILWAY_API_TOKEN` (work workspace) and/or `RAILWAY_PERSONAL_TOKEN` (personal workspace) — fleet-tier env vars, no `railway login` needed. See [library/integrations/railway.md](../library/integrations/railway.md).

### Modal

Serverless GPU/CPU compute — deployed apps, functions, containers.

**Skills:** `/modal-status` (workspace overview), `/modal-logs` (view/debug logs), `/modal-deploy` (deploy apps).

**Setup:** host-level `modal` CLI auth (workspace-scoped secrets/volumes). See [library/integrations/modal.md](../library/integrations/modal.md).

### Neon (PostgreSQL)

Database branches, queries, project management.

**Skills:** `/claudna:neon-info` (schema overview), `/claudna:neon-query` (run SQL), `/claudna:neon-branch` (create/list/delete branches — copy-on-write, cents per branch).

**Setup:** set `NEON_API_KEY` — fleet-tier env var, no `neonctl auth` needed. See [library/integrations/neon.md](../library/integrations/neon.md).

### DigitalOcean

Droplets, apps, databases.

Raw `doctl` commands (`doctl compute droplet list`, `doctl apps logs <id>`, `doctl databases list`) — no clauDNA skill wrapper yet. Assumes `doctl` is already authenticated on the host; see [pi-setup-guide.md](runbooks/pi-setup-guide.md) for the install/auth steps. See [library/integrations/digitalocean.md](../library/integrations/digitalocean.md).

### Snowflake

Warehouse queries and schema exploration.

**Skills:** `/claudna:snowflake-query` (run SQL / explore schema), `/claudna:snowflake-cutover` (migrate a project's connection to RSA key-pair auth).

**Setup:** RSA key-pair auth via a 7-var env-contract — `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_ROLE`, `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_DATABASE`, `SNOWFLAKE_PRIVATE_KEY_PATH`, `SNOWFLAKE_PRIVATE_KEY` — no dbt or `profiles.yml` involved. **READ ONLY by default:** SELECT queries only, unless a human explicitly approves DDL/DML. See [library/integrations/snowflake.md](../library/integrations/snowflake.md).

## Integration Patterns

### Lean Bots (1-2 MCP servers)

Worker bots that do one thing well:
- **Code reviewer:** GitHub only
- **Engineer bot:** GitHub + Slack (for context)

### Medium Bots (3-5 MCP servers)

Specialist bots with domain focus:
- **Business bot:** Shopify + Printify + Gmail + Notion
- **Work engineer:** GitHub + Slack + Notion

### Full-Stack Bots (6+ MCP servers)

Manager/assistant bots with broad capabilities:
- **Personal assistant:** GitHub + Notion + Gmail + Calendar + Home Assistant + Slack + Spotify + Docker

### Resource Impact

Each MCP server adds ~50-100 MB RAM. Keep worker bots lean:

| MCP Count | Approx RAM Impact | Recommendation |
|-----------|-------------------|----------------|
| 1-2 | ~100-200 MB | Worker bots |
| 3-5 | ~200-400 MB | Specialist bots |
| 6-10 | ~400-800 MB | Manager bots only |
