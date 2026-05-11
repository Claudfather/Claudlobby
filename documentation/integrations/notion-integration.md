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

### 2. Add to .mcp.json

```json
{
  "mcpServers": {
    "notion": {
      "command": "npx",
      "args": ["-y", "@notionhq/notion-mcp-server"],
      "env": {
        "NOTION_TOKEN": "ntn_your_token_here"
      }
    }
  }
}
```

For bots that access multiple Notion workspaces, use separate server names:

```json
{
  "mcpServers": {
    "notion-personal": {
      "command": "npx",
      "args": ["-y", "@notionhq/notion-mcp-server"],
      "env": {
        "NOTION_TOKEN": "ntn_personal_workspace_token"
      }
    },
    "notion-work": {
      "command": "npx",
      "args": ["-y", "@notionhq/notion-mcp-server"],
      "env": {
        "NOTION_TOKEN": "ntn_work_workspace_token"
      }
    }
  }
}
```

### 3. Share Pages with the Integration

The integration can only see pages explicitly shared with it:

1. Open the page/database in Notion
2. Click `...` (top right) → **Connections** → add your integration
3. The integration now has read/write access to that page and its children

### 4. Restart the Bot

```bash
sudo systemctl restart your-bot
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
