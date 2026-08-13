---
name: graduate
description: "Use when a knowledge doc needs a placement verdict (promote, refresh, demote, retire), when the scheduled staleness sweep is due, when a rung has accumulated duplicate topics, or when a ratified verdict needs executing."
argument-hint: "[<doc-path> | sweep | dedup | apply <doc-path>]"
---

# Graduate

Walk the knowledge-graduation ladder for fleet docs. The policy — rung criteria, verdicts, ratification routing, cadence — lives in the `knowledge-graduation` protocol composed alongside this skill; this skill is the mechanics. Everything it emits is a proposal, even the obvious ones.

## Modes

### 1. Single doc (default): `/graduate <doc-path>`

1. **Read the doc** — body plus frontmatter (`title`, `status`, `owner`, `tags`, `updated`, `expires`).
2. **Locate its rung** from the path (bot `memory/`, fleet `shared/` tree, or vault).
3. **Apply the protocol's criteria** for that rung, in both directions — qualifies up, over-placed, stale, or superseded.
4. **Dedup-check the target rung** before proposing a promote:
   - rung 2 target: scan the fleet `shared/` tree's INDEX.md files first, then Grep titles and tags
   - rung 3 target: search the vault on the topic, using whichever vault-search skill you have (clauDNA offers `/claudna:recall` and `/claudna:claudron lookup`) or the `claudron` CLI directly — vault dedup is title-keyed, so search by topic, not exact title
5. **Emit one proposal block** (format below).

### 2. Staleness sweep: `/graduate sweep`

The scheduled pass.

1. Enumerate every doc under the fleet's shared-docs root — all subdirectories, not a fixed list.
2. Grep frontmatter for docs meeting the protocol's staleness criteria.
3. Run the single-doc walk on every hit. Skip docs another bot is actively editing (INDEX.md ownership) and note the skip.
4. Report coverage: directories scanned, docs checked, hits, skips and why.

### 3. Dedup pass: `/graduate dedup`

1. Scan each rung's INDEX.md files for title/tag collisions; confirm suspected pairs by reading both docs.
2. For each real collision, propose a merge: which doc is the incumbent, what the candidate contributes, `retire` for the loser.

### 4. Execute: `/graduate apply <doc-path>`

Run only after a verdict is ratified; name the ratifying reply in the report.

- **memory → shared:** write the doc into the right `shared/` subtree with full frontmatter; the memory copy becomes a pointer.
- **shared → vault:** strip the file's frontmatter, capture into the vault through whichever capture skill you have (clauDNA offers `/claudna:capture`) or the `claudron` CLI directly, then the shared copy becomes a pointer.
- **refresh:** re-verify against reality first (run the commands, check the paths) — a refresh without re-verification is fabrication — then bump `updated:` and extend `expires:`.
- **retire / demote:** per the protocol's verdict table and the Shared Documentation lifecycle.
- **Always finish with `/index`** in every touched directory.

## Proposal format

One block per doc, batched into a graduation report:

```
[GRADUATE] <doc-path>
  rung: <memory|shared|vault>   verdict: <promote|refresh|demote|retire>
  evidence: <1-2 lines — what qualifies or disqualifies it>
  target: <destination rung/path, or n/a>
  dedup: <clean | merge into <path>>
```

Send the batch via report-back.

$ARGUMENTS
