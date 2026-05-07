---
name: contacts
description: "Use when the user wants to view, search, add, update, or check follow-ups for contacts (professional, personal, family, service providers). Also used by /briefing for follow-up queries."
argument-hint: "[action: list|search|add|update|follow-ups] [details]"
---

# Contacts

Manage contacts stored in Notion. Contacts have a Type property: Professional, Personal, Family, or Service Provider.

## Notion Database

- **data_source_id** (for queries): `<NOTION_CONTACTS_DATA_SOURCE_ID>`
- **database_id** (for creating pages): `<NOTION_CONTACTS_DATABASE_ID>`

### Properties

| Property | Type | Notes |
|----------|------|-------|
| Name | title | Required |
| Type | select | Professional, Personal, Family, Service Provider |
| Job Title | select | Recruiter, Headhunter, Hiring Manager, Referrer, DJ/Producer, Music Industry, etc. |
| Tasks | relation | Bidirectional link to Tasks Tracker (auto-populated) |
| Follow-up Date | date | Next follow-up due |
| Last Contact Date | date | When last contacted |
| Contact Method | select | In-person, Phone, LinkedIn, Email, Video Call, WhatsApp, Text |
| Email Address | email | |
| LinkedIn | url | |
| Phone No | phone_number | |
| Companies | relation | Links to Companies database |
| Applications | relation | Links to Applications database |
| Agencies | relation | Links to Agencies database |
| Events | relation | Links to Events database |
| Follow-Up Needed | formula | Read-only, auto-calculated |

## Operations

### 1. Search / List Contacts

Query contacts by name, job title, or show all:

```
Tool: mcp__notion__API-query-data-source
data_source_id: <NOTION_CONTACTS_DATA_SOURCE_ID>
```

**By name** — use a title filter:
```json
{"filter": {"property": "Name", "title": {"contains": "search term"}}}
```

**By job title:**
```json
{"filter": {"property": "Job Title", "select": {"equals": "Recruiter"}}}
```

### 2. Follow-ups Due

Used by /briefing and standalone. Query contacts with follow-ups due on or before a date:

```json
{
  "data_source_id": "<NOTION_CONTACTS_DATA_SOURCE_ID>",
  "filter": {
    "and": [
      {"property": "Follow-up Date", "date": {"on_or_before": "YYYY-MM-DD"}},
      {"property": "Follow-up Date", "date": {"is_not_empty": true}}
    ]
  },
  "sorts": [{"property": "Follow-up Date", "direction": "ascending"}]
}
```

When called from /briefing, use the end of the current week (next Sunday) as the date.

### 3. Add a Contact

```
Tool: mcp__notion__API-post-page
```

```json
{
  "parent": {"type": "database_id", "database_id": "<NOTION_CONTACTS_DATABASE_ID>"},
  "properties": {
    "Name": {"title": [{"text": {"content": "Contact Name"}}]},
    "Job Title": {"select": {"name": "Recruiter"}},
    "Email Address": {"email": "email@example.com"},
    "Contact Method": {"select": {"name": "Email"}},
    "Follow-up Date": {"date": {"start": "2026-04-15"}},
    "Last Contact Date": {"date": {"start": "2026-04-04"}}
  }
}
```

Only include properties that were provided. Name is required.

### 4. Update a Contact

First query to find the contact by name, then update using the page ID:

```
Tool: mcp__notion__API-patch-page
page_id: <from query result>
```

```json
{
  "properties": {
    "Last Contact Date": {"date": {"start": "2026-04-04"}},
    "Follow-up Date": {"date": {"start": "2026-04-18"}}
  }
}
```

Only include the properties being changed. When updating "last contact date" and no date is specified, default to today.

### 5. Clear a Follow-up

To remove a follow-up date (mark as no longer needing follow-up):

```json
{
  "properties": {
    "Follow-up Date": {"date": null}
  }
}
```

## Output Formatting

When sending results via Telegram, use `format: "markdownv2"`. See [_telegram-formatting.md](../_telegram-formatting.md) for formatting rules.

Format results concisely for Telegram:

**Single contact:**
```
John Smith — Recruiter at Netflix
Email: <contact>@example.com | LinkedIn: linkedin.com/in/...
Last contact: Mar 15 | Follow-up: Apr 10
```

**Follow-up list:**
```
Follow-ups due:
- Apr 2: <Contact A> (Netflix, recruiter) — email
- Apr 3: <Contact C> (Netflix, recruiter) — LinkedIn
- Apr 7: <Contact H> (<Recruiting Firm>, headhunter) — LinkedIn
```

## Instructions

1. For **search/list**: query with appropriate filter, format results concisely
2. For **add**: confirm the contact name and any provided details, then create
3. For **update**: find the contact first, then patch. If ambiguous name, ask to clarify
4. For **follow-ups**: default to "due this week" unless a date range is specified
5. When the user says they "talked to" or "met with" someone, update Last Contact Date to today and ask if they want to push the follow-up date

$ARGUMENTS
