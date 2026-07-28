---
title: Knowledge Graduation
description: "How the librarian moves knowledge up, down, and out of the promotion ladder"
---

# Knowledge Graduation

Extends the Shared Documentation protocol — the ladder (memory → shared → vault `_shared/` → packs), the writing conventions, and the 90-day `expires:` TTL are defined there. This protocol adds the graduation process on top: who decides a doc's placement, by what criteria, on what cadence.

The librarian role owns this process. Graduation is advisory: the librarian proposes, a ratifier approves, then the librarian executes. No doc moves, renews, or dies without a recorded verdict.

## Verdicts

Every doc the librarian examines gets exactly one verdict:

| Verdict | Meaning |
|---------|---------|
| `promote` | Audience outgrew the rung — move up one rung |
| `refresh` | Right rung, still true — re-verify, bump `updated:`, extend `expires:` |
| `demote` | True but over-placed — audience is narrower than the rung implies; move down |
| `retire` | No longer current — `status: superseded` (a successor exists) or `archived` (none), per the Shared Documentation lifecycle |

## Promotion criteria — per rung

When in doubt, a doc stays where it is.

**memory → shared** (bot-private → fleet-wide):

- Another bot would act differently knowing this — it changed a real decision at least once
- Topic is fleet-owned (a repo, a workflow, an integration), not bot-personal
- Survives its author: written so a bot without the author's context can apply it

**shared → vault `_shared/`** (fleet-wide → every fleet; only when a Claudron vault is wired):

- Useful outside the fleet that learned it — no fleet-specific paths, repos, tokens, or roster assumptions in the payload
- Stable: survived at least one staleness review without a content correction

**Wrong-track check (before any promote):** a reusable building block does not climb this ladder — route it to the library track, per Shared Documentation's two-track rule.

## Staleness sweep

- **Cadence: weekly** — the librarian job Shared Documentation anticipates; surfacing is a direct frontmatter scan until `claudron review --json` is wired.
- **Stale means:** `expires:` past or within 14 days, or a `status:` that contradicts location (a completed plan still in `active/`).
- Every hit gets a verdict — `refresh`, `demote`, or `retire`. A sweep never blanket-renews.
- The sweep report states its coverage; a capped or partial sweep says so.

## Dedup

Before any promote, search the target rung for an existing doc on the topic. A collision turns the proposal into a merge — fold the candidate into the incumbent, then `retire` the candidate. After any promote, exactly one canonical copy remains: the source copy becomes a pointer or retires.

## Ratification

- Proposals batch into a graduation report to the manager (report-back), one verdict per doc with evidence.
- The manager may ratify fleet-internal moves: `refresh`, `demote`, and `promote` into `shared/`.
- A human ratifies anything that leaves the fleet (`promote` to the vault or beyond) and every `retire`.
- Ratification is explicit — a reply naming the doc and verdict; silence is not approval. Unratified proposals lapse and reappear in the next sweep's report.
- Executed moves write only within the librarian's own fleet tree; the vault is reached only through the capture door (`/claudna:capture`).
