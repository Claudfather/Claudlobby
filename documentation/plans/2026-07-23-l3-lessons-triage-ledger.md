---
title: "L3 Lessons Triage Ledger — library/lessons/ class verdicts"
type: plan
status: completed
owner: chris
tags: [ecosystem, claudron, knowledge, vault, migration, boundary, play:ecosystem]
created: 2026-07-23
updated: 2026-08-28
links: ["Claudfather/Claudron L3 plan 06-l3-corpus-return.md", "#509"]
---

# L3 Lessons Triage Ledger

> **✅ COMPLETED (2026-08-28 docs audit).** Executed in the same PR that authored this ledger
> (#683, "L3 — Return the corpus"). Verified against the current tree: `library/lessons/README.md`
> carries the FROZEN banner and confirms the vault migration; `claudlobby/commands/lessons_migrate.py`'s
> `REFERENTIAL_LESSONS`/`BEHAVIOR_LESSONS` maps match this ledger's verdicts exactly (22 + 3 = 25
> notes, all still present on disk as the non-vault fallback); `library/protocols/messaging-channel-discipline.md`
> exists. Also confirmed as landed, beyond the original consumption epic's planned scope, in EPIC
> #509's 2026-07-24 closing comment. This ledger's per-note verdicts remain the authoritative
> record — `tests/test_lessons_migrate.py` keeps the machine mirror in sync with it — so the body
> below is left as originally written.

The PR-reviewable artifact for L3 ("Return the corpus"). One class verdict per
`library/lessons/*.md` note, plus inventory rows for the other library
categories. **No move happens without a row here.** The machine mirror of the
per-note verdicts is `claudlobby/commands/lessons_migrate.py`
(`REFERENTIAL_LESSONS` / `BEHAVIOR_LESSONS`); `tests/test_lessons_migrate.py`
asserts the two never drift and that every lesson keeps a verdict.

## The method (D2, applied here)

The placement question for each note — *is this Q1 behavior or Q2 referential?*
— answered by the **coupling test**: would this content, rendered as a vault
pointer surfaced at session time, still do its job?

- **Q1 behavior** — imperative bot-steering ("always/never do X"). A pointer
  cannot steer behavior, so these **re-home to `library/protocols/` or
  `library/guardrails/`** and keep rendering in-context. They do **not** migrate.
- **Q2 referential** — incident residue, environment facts, domain knowledge a
  bot *consults* when the topic is live. These **migrate to the vault** (via
  `claudron capture --type knowledge`, default `_shared/`) and reach bots through
  the L2 session-loop recall, relevance-ranked. They are **not** deleted here —
  the files stay as the composition fallback for non-vault fleets and as the
  provenance behind the protocols/guardrails some of them motivated (removal is a
  later, scheduled release).

This matches `library/lessons/README.md`'s own taxonomy: *"Rules to follow →
guardrails; Workflow patterns → protocols; … Lessons should read like postmortem
notes … Not commands; observations."* The behavior-class notes below are the
handful mis-filed as lessons; the freeze + re-home corrects that.

## Verdicts — `library/lessons/` (25 notes)

### Behavior (Q1) — re-home, keep in-context, do NOT migrate

| Note | Coupling answer (why a pointer would be inert) | Home |
|---|---|---|
| `messaging-channel-discipline.md` | Imperative: *every* substantive reply MUST go through the channel tool or it never reaches the user. Must render every session — a recall hit at the wrong moment is a dropped reply. (The plan's named canonical behavior example.) | **new** `protocols/messaging-channel-discipline.md` |
| `tmux-dispatch-shell-expansion.md` | Hard rule: every dispatch with prompt content needs the `set +H;` guard. Already enforced in-context — `protocols/dispatch.md` documents that `lib/dispatch.sh` auto-prepends `set +H;`. | already homed: `protocols/dispatch` |
| `orchestration/consensus-before-escalation.md` | Steers the manager's escalation behavior (consensus loop before pinging the human). Already carried, and more richly, by an existing protocol. | already homed: `protocols/consensus-loop` |

### Referential (Q2) — migrate to the vault (`_shared/`, `type: knowledge`)

| Note | Coupling answer (why recall-at-session-time suffices) | Notes |
|---|---|---|
| `dbt/dim-first-architecture.md` | dbt architecture pattern; a data bot consults it when modelling dims. | |
| `dbt/incremental-unique-key-discipline.md` | Incremental-model discipline; consulted during dbt work. | |
| `dbt/parse-vs-execute-time.md` | Tool fact (parse vs execute time) learned the hard way; consulted when debugging dbt. | |
| `dbt/semantic-layer-discipline.md` | Semantic-layer engineering knowledge; consulted during metric work. | |
| `design/addition-earns-place.md` | A design-review lens; surfaced when doing UI review. | |
| `design/whitespace-earns-weight.md` | A design-review lens; surfaced when doing UI review. | |
| `migration/dotenv-export-prefix.md` | Incident residue (the `export ` prefix parse bug) + the fix. | |
| `migration/preserve-existing-env.md` | Incident residue (env-migrate overwrite bug). The generic "dry-run by default" rule is already the `guardrails/idempotency-mandatory` behavior. | |
| `migration/tmux-server-env-inheritance.md` | Environment fact + incident (one-server-per-user identity bleed). | |
| `railway/fail-loud.md` | Railway operational reference — where the token lives, the error-mode playbook. Its behavior nugget ("never fabricate deploy state") is already the `guardrails/no-fabrication` rule. | behavior nugget already homed |
| `raspberry-pi/sdhci-uhs-quirk.md` | Hardware/kernel environment fact; consulted only on a Pi host. | |
| `review/empirical-verification.md` | Review methodology; consulted when reviewing. | |
| `review/mutation-testing-default.md` | Review methodology; consulted when reviewing. | |
| `review/root-cause-not-symptom.md` | Review/debugging methodology; consulted when reviewing. | |
| `review/stacked-pr-squash-corruption.md` | Git incident/knowledge; consulted when handling stacked PRs. | |
| `snowflake/clustering-earns-its-cost.md` | Snowflake domain knowledge; consulted during Snowflake work. | |
| `snowflake/transient-table-recovery.md` | Snowflake recovery knowledge; consulted during an incident. | |
| `telegram/mcp-drops.md` | Upstream-bug knowledge + workarounds (incident residue). | |
| `telegram/orphaned-poller-single-consumer.md` | Incident + diagnosis knowledge. | |
| `telegram/plain-text-escape-incident.md` | Incident residue (2026-04-18). The behavior rule ("plain text only") is already `protocols/telegram-formatting`, which cites this note as the incident that codified it. | behavior already homed |
| `private-repo-screenshots.md` | Knowledge of GitHub's auth-aware image proxy. The procedure is already `protocols/design-evidence-private-repos`. | procedure already homed |
| `telegram-bot-group-setup.md` | Setup/environment knowledge (BotFather privacy mode); consulted when configuring a bot. | |

**Counts: 3 behavior · 22 referential · 25 total.** Every note is accounted for
(the classification test fails on any unclassified file).

## CONVENTIONS.md promotions (F4 — always-relevant referential): **0**

Step 3 marks the *few always-relevant* referential lessons for promotion into the
vault's always-injected, budget-checked `_shared/CONVENTIONS.md`. **For this
corpus that set is empty**, and manufacturing a promotion would be wrong:

- Every referential note above is **role/topic-scoped** — dbt, Snowflake,
  Telegram internals, review methodology, Pi hardware, Railway ops. None is
  relevant to *every* bot every session. `lessons/README.md` says exactly this:
  *"A bot only needs the lessons relevant to its role and tooling — a designer
  doesn't need to know about Snowflake auth quirks."*
- The genuinely universal rules in the corpus are all **behavior** (channel
  discipline, dispatch escaping) and already render in-context via
  protocols/guardrails — they don't belong in the referential always-inject layer.
- Promoting a domain lesson into `CONVENTIONS.md` would spend its token budget on
  content irrelevant to most bots — the opposite of the budget's intent.

**Delivery for all 22 referential notes is L2 recall**, relevance-ranked at
session time. If a specific operator's fleet finds one of these always-relevant,
`claudron capture` into `CONVENTIONS.md` is available to them — a per-fleet call,
not a corpus-wide default. The `CONVENTIONS.md` budget check therefore passes
trivially (this phase adds nothing to it).

## Inventory — other library categories (no move in this PR)

Per the plan, `resources/`/`integrations/`/`principles/` get inventory rows only
— composition *config* is runtime content and stays; world-truth candidates are
flagged for a later phase, not bulk-moved here.

| Category | Verdict | World-truth candidates (flag only) |
|---|---|---|
| `library/resources/` | **Stays** — reference docs the compositor renders into bot `CLAUDE.md` (timezone tables, schema frontmatter specs). Composition config. | Any pure world-fact resource (e.g. a timezone table) is a future vault-migration candidate; the frontmatter-schema resources are contract-shaped and stay. |
| `library/integrations/` | **Stays** — MCP/connector wiring + `tool_grants`. Pure runtime composition config, coupled to `library/mcp/`. | None — this is wiring, not knowledge. |
| `library/principles/` | **Stays** — short guiding-principle rules composed in-context (`visibility-and-speed`, `consolidate-dont-fork`). Behavior-adjacent (they steer how bots build), so a pointer would be inert — same logic as the behavior lessons. | None for vault migration; these are in-context steering, not referential. |

`library/skills/` is explicitly **out of scope** (§10.1 — fleet-operations
commands, format ≠ ownership; they stay). `library/protocols/` and
`library/guardrails/` are the *destinations* of the behavior re-home, not sources.

## What this ledger authorizes

1. `claudlobby lessons-migrate` migrates exactly the 22 referential notes
   (dry-run by default; `--apply` writes via `claudron capture`, never `--force`).
2. `library/protocols/messaging-channel-discipline.md` is created; the other two
   behavior notes are already homed (no new file).
3. `library/lessons/` is frozen (README banner); all 25 files are retained.
4. Nothing in `resources/`/`integrations/`/`principles/`/`skills/` moves.
