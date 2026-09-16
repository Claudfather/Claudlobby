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

> **App identity alternative:** for fleet-scope work that shouldn't ride a developer's personal PAT, `mcp: [github-app]` authenticates as a GitHub App installation instead — short-lived (~1h) tokens minted at use time, stable across an org's team changes, and commits land as the App's own bot identity rather than a human's. See [library/integrations/github-app.md](../library/integrations/github-app.md) and the setup runbook, [github-app-setup.md](runbooks/github-app-setup.md). A fleet can equip both `github` and `github-app` side by side during a migration.

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

### Linear

Issue tracking and project management — teams, projects, issues, comments, documents. Linear ships a **hosted** MCP server: nothing to install, no version to track, and every workspace shares the same endpoint — the API key alone decides which one a bot reaches.

```json
{
  "linear": {
    "type": "http",
    "url": "https://mcp.linear.app/mcp",
    "headers": {
      "Authorization": "Bearer your_linear_api_key"
    }
  }
}
```

**Setup:** Linear → Settings → Security & access → API keys → Create key. A key created with the `Read` permission is enough for a read-only bot. Set `LINEAR_API_KEY` in the fleet `.env` — a second workspace is a second `mcp:` instance (e.g. `LINEAR_ACME_API_KEY`), not a different URL. See [library/integrations/linear.md](../library/integrations/linear.md) for the full guide.

### Gmail + Calendar (via `gws.json` / workspace-mcp)

Read, search, draft, and send emails. Calendar events, free/busy, reminders. Uses the `workspace-mcp` tool, registered as server name `gws` in the MCP fragment `library/mcp/gws.json`.

> **Note:** The older `gmail.json` fragment has been removed. `gws.json` bundles both Gmail and Calendar in a single server instance.

> **A second, zero-secret path also exists.** Claude's native `gmail` / `google-calendar` connectors (`library/integrations/gmail.md`, `library/integrations/google-calendar.md`) reach the same data with no `library/mcp/` fragment and no OAuth client — they authenticate through the Claude account running the bot, not a fleet-managed secret. Equip with `integrations: [gmail]` / `integrations: [google-calendar]` instead of `mcp: [gws]` when you don't need multi-account support or self-hosted secret rotation. Don't equip both paths for the same capability on one bot.

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
    "command": "npx",
    "args": ["-y", "slack-mcp-server", "--transport", "stdio"],
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

## Marketing & Advertising

### Meta Ads

Ad-spend ROI across Meta (Facebook/Instagram) ad accounts — campaigns, ad sets, ads, and insights (impressions, clicks, CPC, spend, conversions, ROAS), plus creatives and audience/targeting catalogs. **Read-only by design:** the 19 write tools (create/update/pause campaigns, ad sets, ads) are never registered unless `META_ADS_ENABLE_WRITE_TOOLS` is explicitly set — leave it `false`.

```json
{
  "meta-ads": {
    "command": "npx",
    "args": ["-y", "meta-ads-mcp-server"],
    "env": {
      "META_ADS_ACCESS_TOKEN": "your_meta_ads_access_token",
      "META_ADS_ENABLE_WRITE_TOOLS": "false"
    }
  }
}
```

**Setup:** [developers.facebook.com](https://developers.facebook.com) → Business-type app → add the Marketing API product. In Business Settings → Users → System Users, generate a long-lived token (a non-expiring System User token is strongly preferred over a ~60-day user token) with the `ads_read` scope, then assign the ad account(s) to that System User. Set `META_ACCESS_TOKEN` in the fleet `.env` — the ad-account id (`act_XXXXXXXXX`) is not an env var, it's passed per call as a tool argument. See [library/integrations/meta-ads.md](../library/integrations/meta-ads.md) for the full walkthrough and gotchas.

### Meta Business (Instagram)

Instagram Professional (Business/Creator) account access — profile and account insights, published media performance, comments, hashtag/mention discovery, and **DM conversations** (the customer inbox). Requires the Facebook Login path (a linked Facebook Page), not the newer, Page-less Instagram Login — this server only supports the former.

```json
{
  "meta-business": {
    "command": "npx",
    "args": ["-y", "@mcpware/instagram-mcp"],
    "env": {
      "INSTAGRAM_ACCESS_TOKEN": "your_instagram_access_token",
      "INSTAGRAM_ACCOUNT_ID": "your_instagram_business_account_id",
      "INSTAGRAM_API_VERSION": "v22.0"
    }
  }
}
```

**Setup:** Same Meta for Developers app as above, with the Instagram Graph API (Facebook Login) — the account needs a linked Facebook Page. Generate a long-lived access token (~60-day expiry, no refresh path — renew manually before it lapses) and note the numeric Instagram Business Account ID. Set `INSTAGRAM_ACCESS_TOKEN` and `INSTAGRAM_BUSINESS_ACCOUNT_ID` in the fleet `.env`. **Caution:** unlike Meta Ads, this server has no server-side write-disable switch — its 8 write tools (including `send_dm`) are registered and only the compositor's allow-list keeps them prompt-gated rather than absent. Compose `pii-protection` on any bot equipping this — a customer inbox is customer PII. See [library/integrations/meta-business.md](../library/integrations/meta-business.md) for the full walkthrough and gotchas.

## Analytics & SEO

### Google Analytics

Read-only GA4 reporting — sessions, users, channels, and on-site behavior for a single pinned property, authenticated with a service-account key (no OAuth, no browser). **Read-only by design:** the server exposes six read-only tools and nothing else.

```json
{
  "google-analytics": {
    "command": "uvx",
    "args": ["--from", "google-analytics-mcp", "ga4-mcp-server"],
    "env": {
      "GOOGLE_APPLICATION_CREDENTIALS": "/abs/path/to/ga4-service-account.json",
      "GA4_PROPERTY_ID": "123456789"
    }
  }
}
```

**Setup:**
1. Enable both the **GA4 Data API** and **GA4 Admin API** on a GCP project.
2. Create a service account and download its JSON key.
3. Add the service account as **Viewer** on the GA4 property (GA4 Admin → Property Access Management — a property grant, separate from GCP IAM).
4. Find the numeric property ID (GA4 Admin → Property Settings) — it is **not** the `G-XXXXXXXXXX` measurement ID wired into the storefront; accounts often hold more than one look-alike property, so confirm it against the property's data-stream measurement ID before wiring it.
5. Set `GA4_SA_KEY_PATH` and `GA4_PROPERTY_ID` in the fleet `.env`.

See [library/integrations/google-analytics.md](../library/integrations/google-analytics.md) for the full walkthrough and gotchas.

### Google Search Console

Read-only Google Search Console access — organic search queries, pages, impressions/clicks/CTR/position, sitemap status, and URL index inspection, authenticated with a service-account key (reuses the `google-analytics` SA if desired). **Read-only by design:** the five mutating tools stay disabled both server-side and in the compositor's allow-set.

```json
{
  "google-search-console": {
    "command": "uvx",
    "args": ["--from", "mcp-search-console", "mcp-search-console"],
    "env": {
      "GSC_CREDENTIALS_PATH": "/abs/path/to/gsc-service-account.json",
      "GSC_SKIP_OAUTH": "true",
      "GSC_ALLOW_DESTRUCTIVE": "false"
    }
  }
}
```

**Setup:**
1. Verify the property in [Search Console](https://search.google.com/search-console).
2. Enable the **Search Console API** on the service account's GCP project — a separate API from GA4's Data/Admin APIs.
3. Reuse the `google-analytics` service account (or create a new one) and add its `client_email` as a user on the property (Search Console → Settings — the gear icon at the bottom-left of the sidebar — → Users and permissions; use **Full** permission if the bot needs URL Inspection).
4. Set `GSC_SA_KEY_PATH` in the fleet `.env` — point it at the same key file as `GA4_SA_KEY_PATH` to reuse the SA (Search Console authorizes per-property and does not inherit GA4 access, so it still needs adding here explicitly).

See [library/integrations/google-search-console.md](../library/integrations/google-search-console.md) for the full walkthrough and gotchas.

### PostHog

Read-only product analytics — funnels, trends, saved insights, event/property definitions, and ad-hoc HogQL/SQL queries against a single pinned project, authenticated with a read-scoped Personal API Key. **Read-only by design:** the endpoint is requested with `readonly=true`, so create/update/delete tools are never registered.

```json
{
  "posthog": {
    "command": "npx",
    "args": [
      "-y",
      "mcp-remote",
      "https://mcp.posthog.com/mcp?mode=tools&readonly=true&project_id=12345",
      "--header",
      "Authorization:Bearer phx_your_api_key"
    ]
  }
}
```

**Setup:** PostHog → Settings → Personal API keys → create one with the **"MCP server"** preset, scoped to read-only. Find the numeric project ID under PostHog → Settings → Project (not the `phc_…` web-analytics token, and not the `phx_…` API key itself). Set `POSTHOG_API_KEY`, `POSTHOG_HOST` (`mcp.posthog.com` for US Cloud, `mcp-eu.posthog.com` for EU), and `POSTHOG_PROJECT_ID` in the fleet `.env`. See [library/integrations/posthog.md](../library/integrations/posthog.md) for the full walkthrough and gotchas.

## Smart Home & IoT

### Home Assistant

Control lights, switches, sensors, automations. Requires HA running on the same network. This is an HTTP-type MCP entry that connects straight to HA's own `/api/mcp/` endpoint — there's no separate MCP server process to install or keep updated.

```json
{
  "homeassistant": {
    "type": "http",
    "url": "http://localhost:8123/api/mcp/",
    "headers": {
      "Authorization": "Bearer your_long_lived_access_token"
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
    "command": "npx",
    "args": ["-y", "docker-mcp"]
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
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-spotify"],
    "env": {
      "SPOTIFY_CLIENT_ID": "your_client_id",
      "SPOTIFY_CLIENT_SECRET": "your_client_secret"
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

**Skills:** `/claudna:vercel status` (deployments, domains, env vars), `/claudna:vercel logs` (view/debug logs), `/claudna:vercel deploy` (deploy to production or preview).

**Setup:** the host's Vercel CLI needs to already be logged in as the right user (`vercel whoami` to check) — this is a one-time host-level login, not a per-bot token. See [library/integrations/vercel.md](../library/integrations/vercel.md).

### Railway

Services, deployments, environments, logs.

**Skills:** `/claudna:railway status` (service overview), `/claudna:railway logs` (view/debug logs), `/claudna:railway deploy` (deploy/update services).

**Setup:** set `RAILWAY_PERSONAL_TOKEN` (ACCOUNT-scoped — answers `me`, reaches every workspace) and/or `RAILWAY_PERSONAL_PROJECT_TOKEN` (WORKSPACE-scoped — answers `projects`, cannot answer `me`) — fleet-tier env vars, no `railway login` needed. The split is scope, not which workspace. See [library/integrations/railway.md](../library/integrations/railway.md).

### Modal

Serverless GPU/CPU compute — deployed apps, functions, containers.

**Skills:** `/claudna:modal status` (workspace overview), `/claudna:modal logs` (view/debug logs), `/claudna:modal deploy` (deploy apps).

**Setup:** host-level `modal` CLI auth (workspace-scoped secrets/volumes). See [library/integrations/modal.md](../library/integrations/modal.md).

### Neon (PostgreSQL)

Database branches, queries, project management.

**Skills:** `/claudna:neon info` (schema overview), `/claudna:neon query` (run SQL), `/claudna:neon branch` (create/list/delete branches — copy-on-write, cents per branch).

**Setup:** set `NEON_API_KEY` — fleet-tier env var, no `neon auth` needed. See [library/integrations/neon.md](../library/integrations/neon.md).

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
