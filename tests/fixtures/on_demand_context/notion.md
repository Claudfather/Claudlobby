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

`API-post-page` declares `children` as an array of **strings** (`items: {type: string}`). The API rejects JSON-encoded strings with `400 validation_error` — it requires actual block **objects**. Pass objects; that schema is wrong.

`API-patch-block-children` declares `children` correctly (`items: $ref blockObjectRequest`) and is not affected.

#### Block Edit Gotcha

`API-update-a-block` cannot change a block's text. Its `type` payload lands nested under `body.type`, which Notion rejects with `body.type should be not present`. And `archived` defaults to **true**, so a `block_id`-only call archives the block instead of inspecting it.

To revise a block: `API-patch-block-children` with `block_id` set to the **parent container** and `after` set to the block being replaced, then `API-delete-a-block` on the original. Passing the replaced block as `block_id` nests the replacement inside it rather than inserting a sibling — no error, just the wrong shape.

`blockObjectRequest` is `anyOf [paragraph, bulleted_list_item]` only, so this recipe cannot express a heading, code or callout block.

#### Common Ops

- **Query a database:** `mcp__notion__API-query-data-source` with `data_source_id`, `filter`, `sorts`
- **Create a page:** `mcp__notion__API-post-page` with `parent.database_id`
- **Update a page:** `mcp__notion__API-patch-page` with `page_id` and `properties`
- **Search:** `mcp__notion__API-post-search` with `query` and optional `filter`

#### Failure Modes

- `404 object_not_found` on query → likely using database_id instead of data_source_id
- `400 validation_error` on property → property name or type doesn't match schema; use `API-retrieve-a-database` to check current schema
- `400 validation_error` on `children` → `API-post-page` blocks passed as JSON strings; pass block objects (see Block Children Schema Gotcha)
- `body.type should be not present` on `API-update-a-block` → that tool cannot edit block text; append-then-archive instead (see Block Edit Gotcha)
- block appeared nested inside the block it should have replaced → `block_id` was the replaced block, not its parent container (see Block Edit Gotcha)
- `401 unauthorized` → token expired or database not shared with integration
