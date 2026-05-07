---
name: triage
description: "Surface untracked items from emails, meetings, or alerts. Enrich with external data, deduplicate against Notion, and create structured tasks."
argument-hint: "[email|alerts|meetings|all] [query]"
---

# Triage

Surface untracked work items from unstructured inputs and move them into Notion with full context.

## Flow

1. Read source (email inbox, Slack channel, meeting notes)
2. For each item, check if it already exists in Notion (deduplicate)
3. Enrich with external data (order lookups, customer records, PR status)
4. Present net-new items to user for confirmation
5. Create Notion tasks with linked contacts/records

## Sources

| Source | MCP Tool | Enrichment |
|--------|----------|------------|
| Email | Gmail MCP | Order # → Shopify lookup |
| Slack alerts | Slack MCP | Error → repo/file context |
| Meeting notes | Granola MCP | Action items → calendar/tasks |

## Rules

- Always deduplicate before creating
- Link to existing contacts when possible
- Present items for confirmation before bulk-creating
- Include source reference (email ID, thread URL) in task notes
