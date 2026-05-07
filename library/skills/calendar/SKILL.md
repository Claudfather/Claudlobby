---
name: calendar
description: "Use when the user asks about their schedule, calendar, what's on today/tomorrow/this week, or wants a unified view of events + tasks + follow-ups."
argument-hint: "[today|tomorrow|week|YYYY-MM-DD] [--focus tasks|events|all]"
---

# Calendar

Unified calendar view merging Google Calendar events, Notion tasks, and contact follow-ups into a single timeline.

When sending results via Telegram, use `format: "markdownv2"`. See [_telegram-formatting.md](../_telegram-formatting.md) for formatting rules.

## Data Sources

Query ALL sources in parallel for speed:

### 1. Google Calendar Events

Query each connected Google Calendar account. Currently using built-in MCP; will migrate to google_workspace_mcp instances when available.

**Always query BOTH calendars in parallel:**

Personal:
```
Tool: mcp__claude_ai_Google_Calendar__gcal_list_events
timeMin: <range_start> (ISO 8601, America/New_York)
timeMax: <range_end> (ISO 8601, America/New_York)
timeZone: America/New_York
```

Work (separate Google Workspace account):
```
Tool: mcp__gws-work__get_events
user_google_email: <your-work-email>
time_min: <range_start> (ISO 8601)
time_max: <range_end> (ISO 8601)
```

Tag work events with _(work)_ when displaying. Deduplicate events that appear on both calendars (same title + time).

### 2. Notion Tasks with Due dates

```
Tool: mcp__notion__API-query-data-source
data_source_id: <your-tasks-data-source-id>
```

Query tasks where:
- Due date falls within the requested range
- Status is NOT "Done" or "Canceled"

Extract: task name, due date, priority, status, related contact, tags.

### 3. Notion Contact Follow-ups

```
Tool: mcp__notion__API-query-data-source
data_source_id: <your-contacts-data-source-id>
```

Query contacts where:
- Follow-up Date falls within the requested range

Extract: contact name, company, follow-up date, follow-up method (if available).

### 4. Granola Meeting Context (parallel with above)

For today/tomorrow views, check if any calendar events have participants who appeared in recent Granola meetings:

```
Tool: mcp__granola__list_meetings
time_range: "last_30_days"
```

If there's participant overlap between an upcoming event and a past Granola meeting, add a context line under that event:
```
• 14:00-15:00 Acme sync
  💡 Last met Mar 24 — pricing discussion, 3 pending actions
```

This is optional enrichment — don't block the calendar output if Granola is slow or empty.

## Date Range Logic

| Argument | Range |
|----------|-------|
| (none) / `today` | Today 00:00 → 23:59 ET |
| `tomorrow` | Tomorrow 00:00 → 23:59 ET |
| `week` | Today → next Sunday 23:59 ET |
| `YYYY-MM-DD` | That specific date 00:00 → 23:59 ET |

Always use America/New_York timezone.

### Evening Auto-Expand

When no argument is provided and the current local time is **after 6:30 PM ET**, automatically expand the view to show the **rest of the week** (tomorrow through Sunday) instead of just today. Today's events are mostly past at that point — the user cares about what's coming up. Show today's remaining items briefly, then the upcoming days in week format.

## Merge Logic

1. Normalize all items to a common shape:
   - `time`: start time (or "all-day" for tasks/follow-ups without a specific time)
   - `end_time`: end time (events only)
   - `title`: event summary / task name / "Follow up: Contact Name"
   - `source`: "gcal" / "gcal-work" / "task" / "follow-up"
   - `metadata`: priority, status, company, etc.

2. Sort order:
   - Timed events first, sorted by start time ascending
   - All-day items after timed events, grouped by type (events → tasks → follow-ups)

3. For week view, group by day first, then apply sort within each day.

## Output Format

Send via `mcp__plugin_telegram_telegram__reply` to chat_id `<your-chat-id>` with `format: "markdownv2"`.

### Daily View (default)

```
📅 *CALENDAR* — Mon Apr 6

*Events*
━━━━━━━━━━━━
• 09:00\-10:00 Team standup
• 14:00\-15:00 1:1 with manager _(work)_

✅ *Tasks Due*
━━━━━━━━━━━━
• Draft Q3 launch post — HIGH
• Process vendor return

👥 *Follow\-ups*
━━━━━━━━━━━━
• Jamie Lee \(Acme\) — email
• <Person> \(CPA\) — call

⏳ *Free time:* 10:00\-14:00, 15:00\+
```

### Weekly View

```
📅 *CALENDAR* — Apr 6\-12

*MON APR 6*
━━━━━━━━━━━━
• 09:00 Team standup
• \[task\] Draft Q3 launch post — HIGH
• \[follow\-up\] Jamie Lee \(Acme\)

*TUE APR 7*
━━━━━━━━━━━━
• 10:00 Dentist
• \[task\] Reply to <Person> — HIGH

*WED APR 8*
━━━━━━━━━━━━
_No events or tasks_

\.\.\. _(continue for each day with items)_
```

### Empty State

If no events, tasks, or follow-ups found:
```
📅 *CALENDAR* — Mon Apr 6

Nothing scheduled\. Enjoy the free day\!
```

## Free Time Calculation

Only calculate for daily view (not week). Free time slots are gaps between Google Calendar events only — tasks and follow-ups don't block time. Assume working hours 8:00 AM - 7:00 PM ET. Show slots of 30 min or longer.

## Instructions

1. **Parallelize everything** — query all Google Calendar accounts + Notion tasks + Notion contacts concurrently
2. **Default to today** if no argument provided
3. **Skip empty sections** — don't show "Tasks Due" header if there are none
4. **Tag work events** — if events come from a work calendar, append _(work)_ to distinguish
5. **Overdue tasks** — if querying today, also include overdue tasks (due before today, not done) in a separate "Overdue" subsection
6. **Priority indicators** — show priority for High and Urgent tasks only (don't clutter with Normal/Low)
7. **Concise** — one line per item, no prose
8. Send via Telegram to chat_id `<your-chat-id>`

## Relationship to /briefing

`/briefing` shows a compact calendar + tasks section as part of a larger daily digest. `/calendar` is for when you want a focused, detailed timeline view. They query the same data but present it differently:
- `/briefing` = broad overview (calendar + email + finance + contacts + ...)
- `/calendar` = deep calendar view with free time, weekly planning, multi-account

$ARGUMENTS
