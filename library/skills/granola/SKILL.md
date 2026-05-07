---
name: granola
description: "Use when the user asks about meetings, meeting notes, action items from calls, or wants to query Granola. Also used by /briefing for meeting digest."
argument-hint: "[recent|query <question>|folders|<meeting-id>] [--actions] [--contacts]"
---

# Granola

Query and interact with Granola meeting notes. Supports listing meetings, querying content, extracting action items, and optionally creating Notion tasks from action items.

When sending results via Telegram, use `format: "markdownv2"`. See [_telegram-formatting.md](../_telegram-formatting.md) for formatting rules.

## Tools Available

| Tool | Use |
|------|-----|
| `mcp__granola__list_meetings` | List meetings by time range or folder |
| `mcp__granola__list_meeting_folders` | List all folders |
| `mcp__granola__get_meetings` | Get full details for specific meeting IDs |
| `mcp__granola__get_meeting_transcript` | Get verbatim transcript |
| `mcp__granola__query_granola_meetings` | Natural language query across all meetings |

## Modes

### 1. Recent Meetings (default / `recent`)

List recent meetings with summaries.

```
Tool: mcp__granola__list_meetings
time_range: "last_30_days"
```

Output: list of meetings with date, title, participants. If `--actions` flag, also fetch details for each and extract action items.

### 2. Natural Language Query (`query <question>`)

Pass the user's question directly to Granola's query tool.

```
Tool: mcp__granola__query_granola_meetings
query: "<user's question>"
```

**Important:** The response includes numbered citation links `[[0]](url)`. Preserve these in the Telegram output — they let the user click through to the original notes. Escape the URLs properly for MarkdownV2.

### 3. Meeting Details (meeting ID or title match)

If the user references a specific meeting by name or ID:

1. If ID provided, call `get_meetings` directly
2. If name/keyword, call `list_meetings` first, find the best match, then `get_meetings`

### 4. Folders (`folders`)

```
Tool: mcp__granola__list_meeting_folders
```

List folders with note counts. Can then drill into a folder:

```
Tool: mcp__granola__list_meetings
folder_id: "<folder_id>"
```

### 5. Action Items (`--actions`)

Extract action items from recent meetings. Steps:

1. `list_meetings` for the time range
2. `get_meetings` for all returned meeting IDs (batch up to 10)
3. Parse summaries for action items (look for "Action Items" sections, bullet points with names)
4. Group by assignee
5. If `--contacts` flag also set, cross-reference assignees with Notion contacts

### 6. Create Tasks from Actions (`--tasks`)

After extracting action items, create Notion tasks for the user's items only:

```
Tool: mcp__notion__API-post-page
```

For each action item assigned to the user:
- Title: action item text
- Due date: if mentioned in notes, otherwise leave blank
- Tags: "meeting-action"
- Related Contact: link to the contact if they exist in Notion

Confirm with the user before creating tasks.

## Output Format

Send via `mcp__plugin_telegram_telegram__reply` to chat_id `<your-chat-id>` with `format: "markdownv2"`.

### Recent Meetings List

```
🎙️ *MEETINGS* — Last 30 Days

• *Mar 24* — Acme Corp: Dr\. Jane Doe
  Participants: <user>, Jane Doe, <Person>
• *Mar 11* — Acme Corp: John Smith
  Participants: <user>, Jane Doe, John Smith
```

### Query Response

```
🎙️ *MEETINGS*

<query answer with preserved citation links>

Sources: [[0]](url), [[1]](url)
```

### Action Items View

```
🎙️ *ACTION ITEMS* — Last 30 Days

*<user>*
━━━━━━━━━━━━
• Review sample dataset \(Acme Corp, Mar 24\)
• Build feature module \(Beta Corp, Feb 2\)

*<Person>*
━━━━━━━━━━━━
• Send tooling invite \(Acme Corp, Mar 24\)
• Send sample data \(Acme Corp, Mar 24\)
```

### Folders List

```
🎙️ *MEETING FOLDERS*

• Project Alpha — 8 notes
• Data / Warehouse — 4 notes
• Hiring — 4 notes
• Squad Standups — 3 notes
• Personal — 1 note
• Demos — 1 note
• 1:1s — 1 note
• Customer X — 1 note
```

## Arguments

| Argument | Behavior |
|----------|----------|
| (none) / `recent` | List meetings from last 30 days |
| `query <question>` | Natural language search across all meetings |
| `folders` | List meeting folders with note counts |
| `week` | Meetings from this week only |
| `--actions` | Extract and group action items by assignee |
| `--contacts` | Cross-reference with Notion contacts |
| `--tasks` | Create Notion tasks from the user's action items (requires confirmation) |

## Instructions

1. **Preserve citations** — Granola query responses include `[[N]](url)` links. Always include these so the user can click through to the original notes.
2. **Concise** — one line per meeting in list view, one line per action item
3. **Parallel queries** — when fetching details for multiple meetings, batch into a single `get_meetings` call (up to 10 IDs)
4. **Don't dump raw content** — summarize meeting details, don't paste the full summary unless specifically asked
5. **Time context** — always show meeting dates so the user knows how recent the info is
6. **Confirm before creating tasks** — never auto-create Notion tasks without explicit approval
7. Send via Telegram to chat_id `<your-chat-id>`

## Usage by Other Skills

`/briefing` includes a meeting digest section. Queries recent meetings (last 7 days) and shows:
```
🎙️ Recent: 2 meetings this week
• Mon — Acme Corp: data partnership follow-up
• Wed — Beta Corp: dashboard review
Action items for the user: 3 pending
```

`/calendar` cross-references upcoming Google Calendar events with past Granola meeting notes for the same participants — helpful for prep context.

`/triage` is the action counterpart — processes meeting data into Notion tasks, contacts, and calendar events.

$ARGUMENTS
