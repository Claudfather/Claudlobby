# Notion Integration Guide

How to connect bots to Notion for task management, project tracking, and kanban boards.

## Overview

Each Notion workspace needs its own integration token. Bots only see workspaces whose tokens are in their `.mcp.json` — this provides natural isolation. A personal bot can't see a work workspace and vice versa.

## Setup

### 1. Create a Notion Integration

1. Go to [notion.so/profile/integrations](https://www.notion.so/profile/integrations)
2. Click **"New integration"**
3. Name it (e.g., "My Bot")
4. Associate it with the correct workspace
5. Submit and copy the **Internal Integration Secret** (starts with `ntn_`)

### 2. Wire It Up via fleet.yaml

`.mcp.json` is **generated output** — `claudlobby generate` produces it from `fleet.yaml` plus `library/mcp/notion.json`. Never hand-edit `.mcp.json` directly; the next `generate` overwrites hand edits. Instead:

1. Add `notion` to the bot's `mcp:` list in `fleet.yaml`:

   ```yaml
   fleet:
     bots:
       my-bot:
         mcp: [notion]
   ```

2. Run `claudlobby generate --bot my-bot` (root mode) or `claudlobby --fleet <name> generate --bot my-bot` (overlay mode); drop `--bot` to regenerate the whole fleet. This scaffolds a `NOTION_TOKEN=` stub into the fleet's `.env` — idempotent, won't clobber a value you've already set — and writes the resolved `notion` server entry into the bot's `.mcp.json`.
3. Fill in the real token in `local/<fleet>/.env` (or the root `.env` in root-mode): `NOTION_TOKEN=ntn_your_token_here`.
4. Run `claudlobby generate` again so the compositor resolves the token into `.mcp.json`.

For bots that access multiple Notion workspaces, use the `instances:` form instead of hand-picking separate server names — the compositor derives the server names and the canonical env var names for you:

```yaml
fleet:
  bots:
    my-bot:
      mcp:
        - notion:
            instances: [personal, work]
```

This produces two `.mcp.json` entries (`notion-personal`, `notion-work`) and expects two distinct env vars: `NOTION_PERSONAL_TOKEN` and `NOTION_WORK_TOKEN`. (`library/mcp/notion.json`'s `${TOKEN}` placeholder is instance-scoped, so it's namespaced per instance — see `claudlobby/mcp_resolve.py`.) Set both in `.env`, then `claudlobby generate`.

### 3. Share Pages with the Integration

The integration can only see pages explicitly shared with it:

1. Open the page/database in Notion
2. Click `...` (top right) → **Connections** → add your integration
3. The integration now has read/write access to that page and its children

> **If you skip this step, the token still authenticates but every query returns empty results (or a 404), and the bot reports that the database/page "doesn't exist."** The token is fine — the page just isn't shared with the integration. This is the single most common Notion setup mistake.

### 4. Restart the Bot

MCP servers are wired up at Claude Code startup, so applying the new `.mcp.json` needs a real restart, not just a reload. The cross-platform, idempotent way (picks systemd vs. launchd for you):

```bash
lib/spin-up-bot.sh runtime/bots/my-bot
```

Or restart the supervised unit directly:

```bash
systemctl --user restart <BOT_SERVICE>              # Linux
launchctl kickstart -k gui/$(id -u)/<BOT_SERVICE>    # macOS
```

## Creating Databases Programmatically

Once connected, the bot can create Notion databases via MCP tools or the API. Example — tell the bot:

```
Create a database called "Project Tracker" under the Team Hub page with these properties:
- Name (title)
- Status (select: Not Started, In Progress, In Review, Done, Blocked)
- Owner (select: Alice, Bob, Team)
- Priority (select: P0, P1, P2)
- Type (select: Feature, Bug, Tech Debt, Research)
- PR Link (url)
- Notes (rich_text)
- Created (created_time)
```

The bot will use the Notion MCP tools to create the database with the exact schema.

## Recommended Database Structures

### Task/Kanban Tracker

The core database for any bot. Use Board view grouped by Status.

| Property | Type | Values |
|----------|------|--------|
| Name | title | Task description |
| Status | select | Not Started, In Progress, In Review, Done, Blocked |
| Priority | select | P0, P1, P2 |
| Owner | select | Team member names |
| Type | select | Customize per domain |
| Due Date | date | |
| PR Link | url | Associated pull request |
| Notes | rich_text | Context and details |
| Created | created_time | Auto-set |

### Contacts

For bots that interact with people (customers, partners, team members).

| Property | Type | Values |
|----------|------|--------|
| Name | title | Contact name |
| Email | email | |
| Type | select | Customer, Partner, Vendor, etc. |
| Company | rich_text | |
| Notes | rich_text | |
| Related Tasks | relation | → Task Tracker |
| Last Contacted | date | |

### Content Calendar

For bots that manage social media or content publishing.

| Property | Type | Values |
|----------|------|--------|
| Name | title | Post/content title |
| Date | date | Publish date |
| Platform | select | Instagram, Twitter, Email, etc. |
| Status | select | Idea, Draft, Scheduled, Posted |
| Content | rich_text | Post text/description |
| Image URL | url | |

### Cross-Database Relations

Link databases together for richer context:
- Tasks ↔ Contacts (who reported this / who's it assigned to)
- Tasks ↔ Content Calendar (content-related tasks)

## Adding Database IDs to CLAUDE.md

After creating databases, add their IDs to the bot's CLAUDE.md so it knows where to query:

```markdown
## Notion Databases

Key database IDs:
- **Task Tracker**: `your-database-id-here`
- **Contacts**: `your-database-id-here`
- **Content Calendar**: `your-database-id-here`

Use the notion MCP server for all database operations.
```

## Multi-Workspace Isolation

| Bot | Notion Server | Workspace | Can See |
|-----|--------------|-----------|---------|
| Personal Assistant | `notion` | Personal | Personal tasks only |
| Company Bot | `notion` | Company | Company data only |
| Work Engineer | `notion-work` | Work Org | Work tracker only |

Each bot only has tokens for its own workspace(s). There's no way for a bot to access a workspace it doesn't have a token for.

## Direct API fallback

When the MCP layer misbehaves — the server fails to start, a tool hangs, or a fresh `.mcp.json` hasn't been picked up yet — the same `NOTION_TOKEN` authenticates directly against Notion's REST API. This is the escape hatch that keeps a bot productive while the MCP server is down:

```bash
# Auth / connectivity check
curl -s -H "Authorization: Bearer $NOTION_TOKEN" \
     -H "Notion-Version: 2022-06-28" \
     https://api.notion.com/v1/users/me

# Retrieve a page
curl -s -H "Authorization: Bearer $NOTION_TOKEN" \
     -H "Notion-Version: 2022-06-28" \
     https://api.notion.com/v1/pages/<page-id>

# List everything the integration can see (POST /v1/search with an empty body)
curl -s -X POST -H "Authorization: Bearer $NOTION_TOKEN" \
     -H "Notion-Version: 2022-06-28" \
     -H "Content-Type: application/json" \
     https://api.notion.com/v1/search -d '{}'
```

For a multi-instance bot use the instance-scoped token instead (e.g. `$NOTION_WORK_TOKEN`). The token and page-sharing rules are identical to the MCP path — a 404 or empty result still means the page isn't shared with the integration, not that the token is wrong.

## Upstream docs

- Notion API reference — https://developers.notion.com
- MCP server (`@notionhq/notion-mcp-server`) — https://github.com/makenotion/notion-mcp-server
