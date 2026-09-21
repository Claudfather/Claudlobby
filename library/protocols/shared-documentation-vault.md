---
title: Shared Documentation
description: How bots read, write, and maintain fleet-shared documentation when a Claudron vault is wired
---

# Shared Documentation

Your fleet's shared docs live **inside the vault**. Reach them through the Claudron door — `recall` to find, `lookup` to fetch, `capture` to write. Your Shared Documentation section above names the verbs; this protocol says when to use them and what not to do instead.

**Do not hand-scan `INDEX.md` files, and do not walk the doc tree looking for prior work.** This is the one rule that distinguishes a vault-wired fleet from a raw-tree one, and it is not a style preference: the fleet's `shared/` tree is *inside* `$CLAUDRON_VAULT_PATH`, so those INDEX files are vault files. Opening them by hand is the thing the vault door exists to replace, and it silently misses everything in the vault's other tiers.

## Pre-Work Checks

Before starting a task:

1. **Recall first — with a SHORT query. Two to four keywords, never a sentence.** This is the step that
   replaces scanning a tree. The brevity is not style: **`recall` ranks correctly on short queries and
   collapses on prose** (Claudron #143 — score saturates, and the result set becomes a function of the
   vault rather than of your query). Measured, same door, same subject, only length differing: a
   three-word query returned the exact note first; a one-sentence description of the *same idea*
   returned five notes from an unrelated domain. Name the subject, not the situation —
   `cross-fleet consent`, not `when an incident on one fleet breaks something on another fleet…`.
2. **Look up** directly when you already know the title or tag you want.
3. Read only what the results justify. **Cap: never open more than 5 notes before starting work.**

**Recall covers plans, not just knowledge.** An active plan for the same repo or area surfaces through the same query — you do not need a second, manual pass over `planning/active/`. If a returned plan conflicts with your task, flag it to the manager before proceeding.

**A query that returns nothing is a result, not a failure** — but retry **SHORTER** before you conclude
the fleet knows nothing. Recall is relevance-ranked, so vocabulary mismatch reads exactly like absence;
re-wording at the same length does not help and **re-wording a long query stays broken**, which spends
the one retry you were willing to spend.

**And a degraded result does not look degraded.** Under #143 what comes back is a topically
self-consistent cluster from an adjacent domain — it reads exactly like a real relevance-ranked answer,
which is why it gets accepted instead of retried. Obvious garbage would be safer. **If the results are
coherent but off-subject, suspect the query shape before concluding the vault is empty**, and confirm a
negative with a short `lookup` before capturing a new note: two long recalls reading empty is not
evidence of absence.

> **This section is a MITIGATION, not the fix.** The defect is Claudron #143; this protocol only points
> people at the door. Written assuming #143 is **unfixed**. If it lands such that prose ranks correctly,
> revisit both rungs above rather than leaving them to rot — the short-query rule stays harmless, but the
> reasoning attached to it would no longer be true.

## Writing Convention

- **Capture, don't hand-place.** The capture verb types and dedups the note and puts it in the right tier. Hand-creating a file in the tree bypasses both and is how near-duplicate notes accumulate.
- **One note per topic.** If recall surfaces an existing note on the subject, update that note rather than adding a second.
- **Do not hardcode tier paths.** Which tier a note belongs in is Claudron's contract, not a path you compose yourself.

## Lifecycle

- **Creating:** `capture` the note. Claudron types and places it; there is no INDEX to regenerate.
- **Updating:** edit the note in place and let `capture` reconcile it, or edit a working document directly.
- **Completing:** set the terminal status in frontmatter (`superseded`; plans and audits may close `completed`). The note stays in the vault — recall ranks by relevance, so a closed doc stops surfacing without being moved.
- **Stale docs:** knowledge defaults to a 90-day `expires:` TTL. Surfacing expired notes is a frontmatter scan until `claudron review --json` is wired, at which point a weekly librarian job takes it over.

## Working Documents

Plans, decisions, and runbooks are *working* documents: they change while work is in flight, and they are edited directly rather than captured. Reach them through the door like anything else, and edit them in place once recall has told you where they are.

**Single-writer still applies** — do not edit a document another bot is actively writing.

## Promotion Flow

Knowledge promotes by audience, and the vault owns the rungs:

1. `memory/` — single bot (preferences, feedback). Outside the vault; yours alone.
2. Your fleet's tier — fleet-wide. Where `capture` puts a finding by default.
3. The shared hub — visible to every fleet on the vault. Promotion between tiers is a curation verb, human-gated; propose it, do not perform it.

**Reusable building blocks** (skills, protocols, guardrails, expertise) are not knowledge-tier content and do not promote through the vault. When a fleet-local pattern proves useful to any claudlobby deployment, promote it to `library/` via a branch + PR to the open-source repo — a separate track, not a rung.
