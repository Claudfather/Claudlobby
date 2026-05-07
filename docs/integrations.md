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

Task management, databases, kanban boards. See [notion-integration.md](notion-integration.md) for full guide.

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

### Gmail (via workspace-mcp)

Read, search, draft, and send emails. Supports multiple accounts on different ports.

```json
{
  "gmail": {
    "command": "uvx",
    "args": ["workspace-mcp", "--tools", "gmail"],
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

### Google Calendar (via workspace-mcp)

Events, free time, reminders. Same OAuth client as Gmail.

```json
{
  "calendar": {
    "command": "uvx",
    "args": ["workspace-mcp", "--tools", "gmail", "calendar"],
    "env": {
      "GOOGLE_OAUTH_CLIENT_ID": "your_client_id",
      "GOOGLE_OAUTH_CLIENT_SECRET": "your_secret",
      "WORKSPACE_MCP_CREDENTIALS_DIR": "/home/user/.google_workspace_mcp/my-email/credentials",
      "USER_GOOGLE_EMAIL": "you@yourdomain.com"
    }
  }
}
```

**Note:** Add `"calendar"` to the `--tools` args alongside `"gmail"` to get both from one server.

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

### Vercel

Deployments, domains, environment variables.

```bash
# CLI setup (not MCP — used via Bash tool)
npm install -g vercel && vercel login
```

### Railway

Services, deployments, environments, logs.

```bash
npm install -g @railway/cli && railway login
```

### Neon (PostgreSQL)

Database branches, queries, project management.

```bash
npm install -g neonctl && neonctl auth
```

### DigitalOcean

Droplets, apps, databases.

```bash
# See pi-setup-guide.md for install
doctl auth init
```

### dbt + Snowflake

Data modeling and warehouse queries.

```bash
pip install dbt-snowflake
# Configure profiles.yml with Snowflake credentials
```

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
