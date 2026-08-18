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

Each subdirectory has an INDEX.md that lists its contents. The indexing skill is the **sole writer** of INDEX.md files — bots create or update knowledge docs, then run it to regenerate the index. Use whichever one you have (clauDNA offers `/claudna:index`).

INDEX.md format (one line per doc, scan-friendly):

```
- [Title](filename.md) — description (status: X, owner: Y, tags: a, b)
```

## Lifecycle

- **Creating:** write the doc with frontmatter → run the indexing skill to update INDEX.md.
- **Updating:** edit the doc, bump `updated:` in frontmatter → run the indexing skill.
- **Completing:** change `status:` to completed/superseded → move from `active/` to `completed/` if applicable → run the indexing skill in both directories.
- **Stale docs:** knowledge docs default to 90-day TTL via `expires:` field. Surface expired docs by scanning frontmatter directly; when a Claudron release with `claudron review --json` is wired, a weekly librarian job takes this over.

## Promotion Flow

Two separate tracks graduate content out of a fleet.

**Knowledge** (facts, learnings, decisions, runbooks) promotes by audience:

1. `memory/` — single bot (preferences, feedback)
2. `shared/` — fleet-wide (repo knowledge, workflow patterns)
3. Vault `_shared/` — visible to every fleet on the vault (only when a Claudron vault is wired; fleets without one stop at rung 2)
4. Claudron packs — cross-deployment, shared via git (only once Claudron ships packs; until then the ladder ends at rung 3)

**Reusable building blocks** (skills, protocols, guardrails, expertise) are not knowledge-tier content. When a fleet-local pattern proves useful to any claudlobby deployment, promote it to `library/` via a standard branch + PR to the open-source repo — a separate track, not a rung above `shared/`.
