---
title: Documentation Frontmatter Schema
description: Required frontmatter fields for all fleet shared documentation
---

# Documentation Frontmatter Schema

> **SSOT: [Claudron `SCHEMA.md`](https://github.com/Claudfather/Claudron/blob/main/SCHEMA.md).**
> As of Claudron 0.2.0 the note schema is ratified there and enforced in code
> (`claudron validate`); this file is claudlobby's operational reference to
> the same contract. **Schema changes must PR Claudron first**, then sync
> here. Everything below remains valid — Claudron's vocabulary was built as
> a superset of it (see the mapping notes).

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

### Mapping to Claudron SCHEMA.md (deltas only)

Claudron's ratified vocabulary absorbs the table above and extends it:

- **`archived` added to every type** — a deliberate superset addition
  (terminal, like `superseded`, for content retired without a successor).
- **Legacy aliases accepted with a warning:** `active` on knowledge/runbook
  maps to `current` (`claudron validate` suggests the mapping, never errors
  on adopted docs).
- **Trust moved to its own axis:** Claudron adds an optional
  `maturity: draft | verified | canonical` field — `status` stays the
  activity axis above. Agent-written notes enter as `maturity: draft`.
- **`ratified` is terminal but never hidden:** ratified decisions are exempt
  from staleness *and* remain in default search results.

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
