---
name: tasks
description: "Use when the user wants to create, view, update, or manage personal tasks and reminders. Also used by /briefing for task queries."
argument-hint: "[action: list|add|update|complete|overdue|due-soon] [details]"
---

# Tasks

Manage personal tasks and reminders stored in Notion.

## Notion Database

- **data_source_id** (for queries): `<NOTION_TASKS_DATA_SOURCE_ID>`
- **database_id** (for creating pages): `<NOTION_TASKS_DATABASE_ID>`

### Properties

| Property | Type | Values |
|----------|------|--------|
| Task name | title | Required |
| Status | status | Not started, In progress, Paused, Done, Canceled |
| Priority | select | High, Medium, Low |
| Due date | date | |
| Effort level | select | Small, Medium, Large |
| Tags | multi_select | <your tag values> |
| Description | rich_text | |
| Related Contact | relation | Links to Contacts database (bidirectional) |
| Assignee | people | |
| Past due | formula | Read-only, auto-calculated |

**When creating tasks that involve a person, always link via Related Contact.** Search contacts by name first. If the contact doesn't exist, create one via /contacts, then link.

## Operations

### 1. List Tasks

Default: show non-done, non-canceled tasks.

```
Tool: mcp__notion__API-query-data-source
data_source_id: <NOTION_TASKS_DATA_SOURCE_ID>
```

**Active tasks (default):**
```json
{
  "filter": {
    "and": [
      {"property": "Status", "status": {"does_not_equal": "Done"}},
      {"property": "Status", "status": {"does_not_equal": "Canceled"}}
    ]
  },
  "sorts": [{"property": "Due date", "direction": "ascending"}]
}
```

**By status:**
```json
{"filter": {"property": "Status", "status": {"equals": "In progress"}}}
```

**By priority:**
```json
{"filter": {"property": "Priority", "select": {"equals": "High"}}}
```

**By tag:**
```json
{"filter": {"property": "Tags", "multi_select": {"contains": "Finances"}}}
```

### 2. Overdue Tasks

Used by /briefing. Query tasks past due that aren't done/canceled:

```json
{
  "data_source_id": "<NOTION_TASKS_DATA_SOURCE_ID>",
  "filter": {
    "and": [
      {"property": "Due date", "date": {"before": "TODAY"}},
      {"property": "Due date", "date": {"is_not_empty": true}},
      {"property": "Status", "status": {"does_not_equal": "Done"}},
      {"property": "Status", "status": {"does_not_equal": "Canceled"}}
    ]
  },
  "sorts": [{"property": "Due date", "direction": "ascending"}]
}
```

Replace `TODAY` with the actual date (e.g., `2026-04-04`).

### 3. Due Soon

Tasks due within N days (default 7):

```json
{
  "data_source_id": "<NOTION_TASKS_DATA_SOURCE_ID>",
  "filter": {
    "and": [
      {"property": "Due date", "date": {"on_or_before": "END_DATE"}},
      {"property": "Due date", "date": {"on_or_after": "TODAY"}},
      {"property": "Status", "status": {"does_not_equal": "Done"}},
      {"property": "Status", "status": {"does_not_equal": "Canceled"}}
    ]
  },
  "sorts": [{"property": "Due date", "direction": "ascending"}]
}
```

### 4. Add a Task

```
Tool: mcp__notion__API-post-page
```

```json
{
  "parent": {"type": "database_id", "database_id": "<NOTION_TASKS_DATABASE_ID>"},
  "properties": {
    "Task name": {"title": [{"text": {"content": "Task description"}}]},
    "Status": {"status": {"name": "Not started"}},
    "Priority": {"select": {"name": "Medium"}},
    "Due date": {"date": {"start": "2026-04-10"}},
    "Effort level": {"select": {"name": "Small"}},
    "Tags": {"multi_select": [{"name": "Finances"}]},
    "Description": {"rich_text": [{"text": {"content": "Details here"}}]}
  }
}
```

Only include properties that were provided. Task name is required. Default Status to "Not started" if not specified.

When the user asks for a **reminder**, treat it as a task with a due date.

### 5. Update a Task

First query to find the task by name, then update using the page ID:

```
Tool: mcp__notion__API-patch-page
page_id: <from query result>
```

```json
{
  "properties": {
    "Status": {"status": {"name": "In progress"}},
    "Due date": {"date": {"start": "2026-04-15"}}
  }
}
```

Only include the properties being changed.

### 6. Complete a Task

Shortcut to mark as Done:

```json
{
  "properties": {
    "Status": {"status": {"name": "Done"}}
  }
}
```

## Output Formatting

When sending results via Telegram, use `format: "markdownv2"`. See [_telegram-formatting.md](../_telegram-formatting.md) for formatting rules.

Format results concisely for Telegram:

**Task list:**
```
TASKS:
Overdue:
- <example task A> (due <date>) — Medium
- <example task B> (due <date>) — Not started

In progress:
- <example task C>
- <example task D>

Due this week:
- <example task E> (due <date>) — High, Small
```

**Single task:**
```
Task: <example task>
Status: Not started | Priority: Medium | Effort: Large
Due: <date> (OVERDUE)
Tags: <tag>
```

## Instructions

1. For **list**: default to active tasks (not done/canceled), sorted by due date
2. For **add**: confirm task name, create with provided details. Ask for due date if it seems time-sensitive
3. For **update**: find the task first, then patch. If ambiguous name, ask to clarify
4. For **complete**: find by name, set status to Done
5. For **reminders**: create a task with the reminder text as the name and the specified time as due date
6. When called from /briefing, run overdue + in-progress + due-this-week queries

$ARGUMENTS
