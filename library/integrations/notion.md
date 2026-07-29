---
title: Notion MCP
tool_grants:
  - "mcp__notion__*"
---

# Notion MCP

Wire config: `library/mcp/notion.json` (uses `${NOTION_TOKEN}`).

#### Workspace Routing

If a bot has multiple Notion MCP servers (e.g., `notion` for personal, `notion-work` for a work workspace), always route to the correct one:

| Context | MCP Server | When |
|---------|-----------|------|
| Personal tasks, contacts, reminders | `mcp__notion__*` | Personal workspace |
| Work items, team trackers, sprint boards | `mcp__notion-work__*` | Work workspace |

When ambiguous ("check Notion for X"), ask which workspace.

#### API Version Gotcha

The Notion API (`Notion-Version: 2025-09-03`) splits **databases** from **queryable data sources**. Two IDs exist for every database:

- **database_id** — use for `API-post-page` (creating pages) and `API-retrieve-a-database`
- **data_source_id** — use for `API-query-data-source` (querying/filtering)

Passing the database_id to a query endpoint returns `404 object_not_found`. If an ID stops working, re-resolve via `API-post-search` with the database name.

#### Block Children Schema Gotcha

`API-patch-block-children` — and page creation passing `children` — declares `children` as an array of **strings** in its tool schema. The API rejects JSON-encoded strings with `400 validation_error` and requires actual block **objects**. Pass objects; the declared schema is wrong, and following it fails every time.

#### Block Edit Gotcha

`API-update-a-block` cannot change a block's text through this MCP. The payload lands nested under `body.type` and Notion rejects it with `body.type should be not present`. Only the `archived` flag works through that tool.

To revise a block: `API-patch-block-children` with `after` set to the block being replaced, then `API-delete-a-block` on the original. Appending before archiving preserves both position and block count.

#### Common Ops

- **Query a database:** `mcp__notion__API-query-data-source` with `data_source_id`, `filter`, `sorts`
- **Create a page:** `mcp__notion__API-post-page` with `parent.database_id`
- **Update a page:** `mcp__notion__API-patch-page` with `page_id` and `properties`
- **Search:** `mcp__notion__API-post-search` with `query` and optional `filter`

#### Failure Modes

- `404 object_not_found` on query → likely using database_id instead of data_source_id
- `400 validation_error` on property → property name or type doesn't match schema; use `API-retrieve-a-database` to check current schema
- `400 validation_error` on `children` → blocks passed as JSON strings; pass block objects (see Block Children Schema Gotcha)
- `body.type should be not present` on `API-update-a-block` → that tool cannot edit block text; append-then-archive instead (see Block Edit Gotcha)
- `401 unauthorized` → token expired or database not shared with integration
