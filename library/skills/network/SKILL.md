---
name: network
description: "Use when the user wants to manage professional network activity — scan emails for contact updates, suggest new connections, cross-reference job postings with contacts, and maintain Notion contact records."
argument-hint: "[scan|digest|prep|suggest|lint] [options]"
---

# Network

Professional network management — ties together /emails, /contacts, and LinkedIn activity.

## Tools

| Tool | Purpose |
|------|---------|
| `mcp__claude_ai_Gmail__gmail_search_messages` | Scan emails |
| `mcp__claude_ai_Gmail__gmail_read_message` | Read email content |
| `mcp__notion__API-query-data-source` | Query contacts |
| `mcp__notion__API-patch-page` | Update contacts |
| `mcp__notion__API-post-page` | Add contacts |
| `mcp__notion__API-retrieve-a-page` | Get contact details |
| `mcp__notion__API-API-create-a-comment` | Post comment on contact page |

## Notion Database

- **Contacts data_source_id**: `<NOTION_CONTACTS_DATA_SOURCE_ID>`
- **Contacts database_id** (for creates): `<NOTION_CONTACTS_DATABASE_ID>`

## Operations

### 1. Scan (default)

Scan recent emails for network-relevant activity:

```
mcp__claude_ai_Gmail__gmail_search_messages
q: "label:LinkedIn OR label:LinkedIn-Jobs OR from:linkedin.com newer_than:3d"
maxResults: 30
```

Also scan tagged/important inbound:
```
q: "is:important newer_than:3d -from:github.com -from:vercel.com -category:promotions"
```

For each relevant email:
1. **Match sender** against Notion contacts (search by email or name)
2. **Known contact** → Post a comment on their Notion page:
   ```
   Tool: mcp__notion__API-create-a-comment
   parent: {"page_id": "<contact-page-id>"}
   rich_text: [{"text": {"content": "Apr 4: Email re: [subject]. [1-line summary]"}}]
   ```
   Also update Last Contact Date if this is a direct correspondence.
3. **Unknown sender worth tracking** → Suggest adding as new contact
4. **Job postings** → Cross-reference company with existing contacts

### 2. Digest (weekly)

Weekly network activity summary:
- Emails exchanged with known contacts
- New connections suggested
- Follow-ups completed vs overdue
- Job postings at companies where you have contacts
- Contacts going stale (no interaction in 90+ days)

### 3. Prep

Meeting/interaction prep for a specific contact:
```
/network prep <Contact A>
```

1. Query contact from Notion
2. Search emails for all correspondence: `from:contact-a@example.com OR to:contact-a@example.com`
3. Read recent thread content
4. Check for related job postings at their company
5. Present: last interaction, key topics, suggested talking points

### 4. Suggest

Suggest network expansion opportunities:
- People CC'd on emails with known contacts
- Recurring email senders not in contacts
- LinkedIn connections at target companies
- Referral chains: "You know A at Company X, and B at Company X just posted a role"

### 5. Lint

Batch audit of Notion contacts against email history. Notion-first: starts from contacts, enriches from email.

**Scoping options:**
- `/network lint` — all Professional + Service Provider contacts (skips Personal + Family)
- `/network lint --type Professional` — only Professional contacts
- `/network lint --stale 90` — only contacts not updated in 90+ days
- `/network lint --limit 10` — process max N contacts (for testing or partial runs)

**Flow:**

1. **Fetch all contacts** matching the scope:
   ```
   Tool: mcp__notion__API-query-data-source
   data_source_id: <NOTION_CONTACTS_DATA_SOURCE_ID>
   ```
   Filter by Type (Professional/Service Provider by default). **Skip Personal and Family contacts entirely** — they don't need email-based tracking. Sort by Last Contact Date ascending (stalest first).

2. **For each contact**, search BOTH email accounts in parallel:
   ```
   Personal: mcp__claude_ai_Gmail__gmail_search_messages
   q: "from:<email> OR to:<email>"

   Work: mcp__gws-work__search_gmail_messages
   query: "from:<email> OR to:<email>"
   ```
   If no email address on file, search by full name instead.

3. **Classify each contact** into one of:
   - **Active** — email interaction found, Last Contact Date matches or is more recent than latest email → no action needed. Do NOT list these under "stale dates."
   - **Stale date** — email interaction found **strictly more recent** than Last Contact Date → update Last Contact Date + log comment. **Never overwrite LCD if it's already more recent than email** (contact may have been via meeting, call, etc.). Only show contacts here when there's an actual date gap.
   - **Missing fields** — contact exists but email, company, LinkedIn, or Job Title is empty and discoverable from email metadata → suggest fills. **Only flag fields that are actually empty in the fetched properties** — do not assume a field is missing without reading it.
   - **Ghost** — no email interaction found at all → flag for review (wrong email? never actually corresponded?)
   - **No email on file** — can't search. Use name-based email search to try to discover their email: search both accounts for the contact's full name. If found, extract email from headers and suggest adding it, then re-search by email for LCD.

4. **Present results** grouped by category:
   ```
   NETWORK LINT — 45 contacts scanned

   ✅ Active (28): up to date, no action needed

   📝 Stale dates (7): Last Contact Date behind email reality
   - <Contact I> (Acme Corp): Notion says "never", email shows Apr 7
   - <Contact J> (BigCo): Notion says Oct 10, email shows Mar 15
   → Update all 7? [y/n]

   🔍 Missing fields (5):
   - <Person>: missing LinkedIn, Job Title
   - <Person>: missing email
   → Fill from email metadata? [y/n]

   👻 Ghosts (3): no email history found
   - Jamie Doe (Recruiter Co): jamie.doe@... — 0 emails
   - <Person> (Capital LLC): <contact>@... — 0 emails
   → Review manually or archive?

   ⚠️ No email on file (2):
   - <Referrer> (Netflix)
   - <Contact D> (Netflix)
   ```

5. **Conversational execution** — the user decides what to update. Batch updates when approved ("update all 7 stale dates"), individual review for ghosts and missing fields.

6. **When updating**, always:
   - Set Last Contact Date to the most recent email interaction found
   - Post a comment on the contact page: "Lint Apr 7: Updated Last Contact Date from email history. Most recent: [date] re: [subject]"
   - Fill missing Email Address from email headers if found
   - Fill missing Job Title from email signatures if parseable
   - Do NOT overwrite existing fields unless they're clearly wrong

**Completeness requirement:** Every contact with an email MUST be searched on BOTH accounts. Do not skip contacts or accounts due to batching shortcuts. When using bulk queries (OR-ing multiple addresses), verify that every contact got a result or explicit "no match" — don't assume a contact was searched just because it was included in a bulk query that returned results for other contacts.

**Rate limiting:** Each contact requires 2 email searches (personal + work). For large runs, batch searches in parallel groups of 5-10 to avoid overwhelming Gmail API. But completeness > speed — better to take extra batches than miss contacts.

### 6. Job Cross-Reference

When LinkedIn job alerts come in:
```
mcp__claude_ai_Gmail__gmail_search_messages
q: "from:jobalerts-noreply@linkedin.com newer_than:7d"
```

For each job posting:
1. Extract company name
2. Search Notion contacts for people at that company
3. Present: "<Company> Data Scientist 5 posted — you know <Contact A> (recruiter), <Contact C> (recruiter), <Contact D> (recruiter), <Referrer> (referrer)"
4. Suggest which contact to reach out to and draft talking points

## Output Formatting

```
NETWORK SCAN — Apr 4

Correspondence logged:
- <Contact A> (Netflix): replied to your follow-up re: DS5 role
- <Contact E> (<Hedge Fund>): recruiter outreach about quant role

Suggested new contacts:
- <Contact B> (contact-b@example.com) — CC'd on <Contact A>'s email, appears to be hiring manager

Job + Contact matches:
- <Company> Data Scientist 5: 4 contacts (<Contact A>, <Contact C>, <Contact D>, <Referrer>)
- <Company> DS Manager: no contacts

Stale contacts (90+ days):
- <Contact F> (MLP) — last contact Sep 4, 2025
- <Contact G> (<Recruiting Firm>) — last contact Sep 30, 2025

Follow-ups overdue: 5 (see /contacts for details)
```

## Instructions

1. When scanning, always match against Notion contacts before suggesting new ones
2. Post comments on Notion pages to build correspondence history — include date and 1-line summary
3. Update Last Contact Date when direct correspondence is found
4. For job cross-reference, search contacts by company relation or by company name in email/LinkedIn fields
5. **Contact Type awareness**: Contacts have a Type property (Professional, Personal, Family, Service Provider). Only suggest follow-ups and stale-contact nudges for Professional and Service Provider types. Don't flag Family/Personal contacts as "stale" — those relationships don't need CRM-style tracking.
6. When creating new contacts from email scanning, infer the Type: LinkedIn/recruiter/company emails → Professional, everything else → ask the user
5. Never send emails or LinkedIn messages without explicit confirmation
6. When suggesting follow-ups, consider recency and relationship warmth
7. Default to last 3 days for scan, last 7 days for digest

$ARGUMENTS
