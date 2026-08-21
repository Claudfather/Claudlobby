---
name: emails
description: "Use when the user asks about email, inbox, unread messages, or wants to search/draft emails. Also used by /briefing for inbox digest."
argument-hint: "[inbox|search|draft|unread] [query or details]"
---

# Emails

Read, search, and draft emails via Gmail MCP tools.

## Tools

| Tool | Purpose |
|------|---------|
| `mcp__claude_ai_Gmail__gmail_search_messages` | Search/list messages |
| `mcp__claude_ai_Gmail__gmail_read_message` | Read full message content |
| `mcp__claude_ai_Gmail__gmail_create_draft` | Create a draft reply or new email |
| `mcp__claude_ai_Gmail__gmail_get_profile` | Get account info |
| `mcp__claude_ai_Gmail__gmail_list_labels` | List all labels |
| `mcp__claude_ai_Gmail__gmail_list_drafts` | List existing drafts |
| `mcp__claude_ai_Gmail__gmail_read_thread` | Read full email thread |

## Email Categories

When processing inbox for briefings or general review, categorize emails:

| Category | What to show | Gmail query hint |
|----------|-------------|-----------------|
| **Actionable** | Emails requiring a response or action | `is:unread -category:promotions -category:social` then filter manually |
| **Dev/Deployment** | CI/CD failures, build notifications | From: your CI/CD, hosting, and error-tracking providers |
| **Newsletters** | One-liner summaries only | From: substack, newsletters |
| **Job alerts** | Job title + company only | From: your job-alert sender |
| **Noise** | Skip entirely | Amazon reviews, promotions, social |

## Operations

### 1. Inbox Digest (used by /briefing)

Query recent unread emails and categorize:

```
Tool: mcp__claude_ai_Gmail__gmail_search_messages
q: "is:unread newer_than:1d -category:promotions -category:social"
maxResults: 20
```

Then categorize each result by sender/subject into the categories above.

**Briefing output format:**
```
EMAIL
Actionable:
- Meeting follow-up from <Person> (need to confirm time)
- Expense report approval pending from Finance

Dev:
- repo-a #267 PR reviewed by claude[bot]
- repo-b deploy succeeded (Vercel)

Newsletters:
- Industry Weekly: top stories digest

Jobs:
- <Company> <Job Title>
- <Company> <Job Title>
```

### 2. Search Emails

```
Tool: mcp__claude_ai_Gmail__gmail_search_messages
q: "<gmail search syntax>"
```

Common queries:
- `from:someone@example.com` — from specific sender
- `subject:meeting newer_than:7d` — recent subject match
- `has:attachment from:boss` — attachments from someone
- `is:starred` — starred messages
- `label:important is:unread` — unread important

### 3. Read a Message

When the user asks to read a specific email, use the message ID from search results:

```
Tool: mcp__claude_ai_Gmail__gmail_read_message
messageId: <from search results>
```

Summarize the content concisely. Don't dump raw email — extract the key points.

### 4. Read a Thread

For conversation context:

```
Tool: mcp__claude_ai_Gmail__gmail_read_thread
threadId: <from search results>
```

### 5. Draft a Reply

**IMPORTANT: Never send emails without explicit confirmation from the user.**

```
Tool: mcp__claude_ai_Gmail__gmail_create_draft
```

Always:
1. Show the draft content to the user first
2. Wait for explicit "send it" or "looks good, send" confirmation
3. Create as draft (not send) — the user can review and send from Gmail

## Output Formatting

When sending results via Telegram, use `format: "markdownv2"`. See [_telegram-formatting.md](../_telegram-formatting.md) for formatting rules.

Format results concisely for Telegram:

**Email summary:**
```
From: <Person>
Subject: Q2 Planning Meeting
Date: Apr 4, 2:30 PM
Key points: Proposing Wed Apr 9 at 3pm for Q2 kickoff. Needs headcount by Monday.
Action needed: Confirm availability for Apr 9
```

**Search results:**
```
3 emails from contact@example.com (last 7 days):
1. Q2 Planning Meeting (Apr 4) — needs response
2. Re: Budget Review (Apr 2) — FYI
3. Team Offsite Logistics (Mar 31) — resolved
```

## Instructions

1. For **inbox/briefing**: search unread, categorize, summarize concisely
2. For **search**: use Gmail query syntax, show results as a list
3. For **reading**: summarize key points, don't dump raw content
4. For **drafting**: always show draft to user, never auto-send
5. Skip noise (Amazon ratings, promotions, social notifications) unless specifically asked
6. When showing GitHub notification emails, group by PR/issue rather than listing each bot comment

$ARGUMENTS
