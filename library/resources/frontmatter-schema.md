---
title: Documentation Frontmatter Schema
description: Required frontmatter fields for all fleet shared documentation
---

# Documentation Frontmatter Schema

All files in the shared documentation directory must have YAML frontmatter with these fields.

## Required Fields

| Field | Type | Values | Purpose |
|-------|------|--------|---------|
| title | string | — | Human-readable title |
| type | enum | plan, decision, knowledge, runbook, audit, review | Document classification |
| status | enum | type-dependent (see below) | Lifecycle state |
| owner | string | bot name or human | Who maintains this doc |
| created | date | YYYY-MM-DD | Creation date |

### Type-Dependent Status Values

| Type | Status values |
|------|---------------|
| plan | draft, active, completed, superseded |
| knowledge | current, stale, superseded |
| decision | draft, ratified, superseded |
| runbook | current, stale, superseded |
| audit, review | draft, completed |

## Optional Fields

| Field | Type | Values | Purpose |
|-------|------|--------|---------|
| updated | date | YYYY-MM-DD | Last modification |
| expires | date | YYYY-MM-DD | TTL — defaults to +90 days for knowledge, no expiry for decisions |
| last_verified | date | YYYY-MM-DD | When content was last confirmed true |
| repos | list[string] | repo names | Which repos this relates to |
| tags | list[string] | free-form | Discovery tags |
| links | list[string] | paths or URLs | Related docs, PRs, issues |
| supersedes | string | path | Doc this replaces |
| pr | string | URL or #N | Associated PR |
| source_type | enum | url, file, inline | How content was ingested (for /learn) |
| source_url | string | URL | Original source (for /learn dedup) |
| slug | string | kebab-case | Canonical short reference for wikilinks |

## Slug Convention

Generate from title: lowercase, replace spaces and special characters with hyphens, collapse multiple hyphens, truncate at 40 characters on a word boundary.

Examples:
- "Shuffify Auth Rework Plan" → `shuffify-auth-rework-plan`
- "Spotify API Rate Limits & Quirks" → `spotify-api-rate-limits-quirks`

Non-ASCII characters are transliterated. Leading numbers are allowed. Collisions get a suffix: `-2`, `-3`.

## Example

```yaml
---
title: Shuffify Auth Rework Plan
slug: shuffify-auth-rework-plan
type: plan
status: active
owner: greg
created: 2026-05-10
expires: 2026-08-10
repos: [shuffify]
tags: [auth, oauth, spotify]
links: ["#142", "knowledge/shuffify/spotify-api-quirks.md"]
---
```
