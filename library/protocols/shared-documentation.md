---
title: Shared Documentation
description: How bots read, write, and maintain fleet-shared documentation
---

# Shared Documentation

Fleet-shared docs live at the path shown in your Shared Documentation section. All bots on the fleet can read them; any bot can write to them.

## Pre-Work Checks

Before starting a task:

1. Scan `planning/active/INDEX.md` — is there an active plan for the same repo or area?
2. Scan `knowledge/<repo>/INDEX.md` — are there existing learnings relevant to this task?
3. Read only the docs whose title/tags match your current task. **Cap: never read more than 5 knowledge docs before starting work.**

If an active plan conflicts with your task, flag to the manager before proceeding.

## Writing Convention

- **One file per topic, not per bot.** If a knowledge doc about "Spotify rate limits" exists, update it — don't create a second one.
- **Single-writer** — do not edit a doc another bot is actively writing. Check INDEX.md ownership.
- **Frontmatter required** on all shared docs. See the Documentation Frontmatter Schema resource for required fields.

## INDEX.md Maintenance

Each subdirectory has an INDEX.md that lists its contents. `/index` is the **sole writer** of INDEX.md files — bots create or update knowledge docs, then run `/index` to regenerate the index.

INDEX.md format (one line per doc, scan-friendly):

```
- [Title](filename.md) — description (status: X, owner: Y, tags: a, b)
```

## Lifecycle

- **Creating:** write the doc with frontmatter → run `/index` to update INDEX.md.
- **Updating:** edit the doc, bump `updated:` in frontmatter → run `/index`.
- **Completing:** change `status:` to completed/superseded → move from `active/` to `completed/` if applicable → run `/index` in both directories.
- **Stale docs:** knowledge docs default to 90-day TTL via `expires:` field. A librarian cron runs `/index --stale` weekly to surface expired docs.

## Promotion Flow

Knowledge graduates through tiers based on audience:

1. `memory/` — single bot (preferences, feedback)
2. `shared/` — fleet-wide (repo knowledge, workflow patterns)
3. `library/` — any claudlobby deployment (universal learnings, via PR)

When a shared doc proves universally useful, promote it to `library/` via a standard branch + PR.
