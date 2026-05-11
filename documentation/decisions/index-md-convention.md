---
title: INDEX.md Auto-Discovery Convention
type: decision
status: ratified
owner: clog
created: 2026-05-11
tags: [knowledge-system, indexing, shared-docs]
---

# INDEX.md Auto-Discovery Convention

## Decision

Every directory under `shared/` gets a machine-maintained `INDEX.md` that lists its contents. The `/index` skill is the **sole writer** of INDEX.md files. Bots never edit INDEX.md directly.

## Pattern

Same concept as `MEMORY.md` (one-line pointers to individual files), but machine-maintained rather than manually curated. Each INDEX.md is a scan-friendly manifest that lets bots discover what exists without reading every file in the directory.

## Format

```markdown
# Index: <relative-path>

- [Title](filename.md) — description (status: X, owner: Y, tags: a, b)
- [Title](filename.md) — description (status: X, owner: Y, tags: a, b)
```

**Sort order:** active/current status first, then alphabetical by title. Completed and superseded docs sort to the bottom.

## Maintenance Rules

1. **Bots create and update knowledge docs.** After writing a doc with valid frontmatter, the bot runs `/index` to regenerate the directory's INDEX.md.

2. **`/index` is the sole writer.** No manual edits to INDEX.md. No `flock`-based appends. No `echo >>` hacks. The skill reads all `.md` files in the directory (excluding INDEX.md itself), parses their frontmatter, validates required fields, and regenerates the full INDEX.md from scratch. This eliminates concurrent-write issues entirely.

3. **Validation on every run.** `/index` reports missing or invalid frontmatter fields. A doc without valid frontmatter still appears in the index (with a warning marker) but should be fixed.

4. **Stale doc surfacing.** A librarian bot runs `/index --stale` on a weekly cron. This flags:
   - Knowledge docs past their `expires:` date (default TTL: 90 days from creation)
   - Active plans with no `updated:` change in >7 days
   - Docs with `status: draft` older than 14 days

5. **Status transitions update the index.** When a doc's status changes (e.g., `active` → `completed`), the bot updates the doc's frontmatter and runs `/index`. If the doc moves directories (e.g., `planning/active/` → `planning/completed/`), run `/index` in both the source and destination directories.

## Why Not Manual Maintenance

The earlier plan considered `flock`-based appends so any bot could add to INDEX.md. This contradicts the project's own finding that the Write tool does direct overwrite (not atomic) and `flock` can't protect it. Single-writer via `/index` is the only reliable approach.

## Why Not Content-Addressed Dedup

INDEX.md is a discovery layer, not a dedup layer. Duplicate detection happens at write time (source-URL check in `/learn`, date-stamped filenames in `/reflect`). The index simply reflects what exists on disk.

## Scope

Applies to all directories under `shared/` in fleet overlays. Does not apply to `memory/MEMORY.md` (which remains manually maintained per the existing auto-memory system) or to `library/` directories (which use their own README conventions).
