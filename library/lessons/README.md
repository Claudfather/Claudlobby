# library/lessons/

> **FROZEN (L3, boundary re-architecture — 2026-07-23).** This directory is closed to new
> content. Durable knowledge — the "we learned this the hard way" corpus — now lives in the
> **Claudron vault**, reached at session time through the knowledge door, not composed from a
> parallel tree here.
>
> **Where new content goes now:**
> - **A referential finding** (an incident, an environment fact, a domain lesson) → capture it into
>   the vault with `/claudna:capture` (or `claudron capture`). It surfaces later through
>   relevance-ranked recall — see the door-stamped **§Shared Documentation** in a vault-wired bot's
>   composed `CLAUDE.md`.
> - **A bot-steering rule** (imperative "always/never do X" behavior — a pointer cannot steer
>   behavior) → a `library/protocols/` pattern or a `library/guardrails/` rule, which keep rendering
>   in-context.
>
> **Existing files are retained** as the composition fallback for **non-vault** fleets (a fleet with
> no `claudron_vault_path` still renders the lessons it lists in `fleet.yaml`). They also carry the
> provenance behind the protocols/guardrails they motivated. The referential subset was
> captured into the vault by a one-time converter, `claudlobby migration lessons`, which is
> now retired (#2959); its per-note verdicts are the **Triage record** at the end of this
> file. Removal of these files is a later, scheduled release — not this one.

"We learned this the hard way" notes. Specific incidents, retro findings, or empirically-discovered failure modes that bots should know about — distinct from rules (guardrails) or workflow patterns (protocols).

## What belongs here

- **Postmortem-style notes** — "On 2026-04-15 the Railway CLI auth failed silently because the token format changed. Now we always probe with `railway whoami` before any deploy operation."
- **Empirical workarounds** — "When the Notion MCP returns a 502, retrying immediately fails. Wait 30s before retry."
- **Subtle bugs in tools we depend on** — "Anthropic API streams sometimes split a tool-use block across two SSE messages; the SDK handles it, but custom parsers must buffer."
- **Internal incidents that motivated current rules** — "issue #NN: PATs silently expire; `creds-check.sh` was added to surface this within 24h."

## What does NOT belong here

- **Rules to follow** — that's guardrails (`no-push-main`, `pii-protection`)
- **Workflow patterns** — that's protocols (`dispatch`, `report-back`)
- **Capability / domain knowledge** — that's expertise

## Structure: flat, or nested by topic

Most lessons live in a topic subdirectory — `dbt/`, `design/`, `migration/`, `orchestration/`, `railway/`, `raspberry-pi/`, `review/`, `snowflake/`, `telegram/` — once a topic accumulates more than one or two lessons. A handful of general, cross-cutting lessons stay flat at `library/lessons/*.md` (e.g. `messaging-channel-discipline.md`, `tmux-dispatch-shell-expansion.md`).

Reference a nested lesson in `fleet.yaml` by its path relative to `library/lessons/`, without the extension: `lessons: [review/empirical-verification, dbt/dim-first-architecture]`. Folder expansion also works — `lessons: [review/]` pulls in every lesson under `review/`.

## Frontmatter and heading convention (deliberately differs from the rest of the library)

Each lesson file has `title:` (+ optional `description:`) frontmatter like most of the library, but **the body has no leading heading at all** — no H1, no H2. This is intentional, not an oversight: the compositor already renders `### <title>` from the frontmatter when it appends the item under `## Lessons` (see Composition below), so a heading in the body would either duplicate that or, worse, read as two stacked headings with different wording. Write the frontmatter title, then go straight into prose — every lesson file in the repo follows this.

## Composition

Each `<lesson>.md` is appended under a `## Lessons` section, in the order listed in `fleet.yaml` `lessons:`. The compositor renders the heading from frontmatter (`### <title>`) followed by the body — don't repeat the title as a heading inside the body itself.

A bot only needs the lessons relevant to its role and tooling — a designer doesn't need to know about Snowflake auth quirks.

## Example

`library/lessons/messaging-channel-discipline.md`:

```markdown
---
title: Messaging channel discipline
description: Substantive replies must go via the messaging channel tool — session output never reaches the user
---

Every substantive response to the user must be sent through the messaging channel tool (Telegram reply, Slack post, etc.). Inline text in the terminal/session output never reaches the user's device.

- Any response beyond a trivial "ack" goes through the messaging tool.
```

A nested lesson follows the same shape, just filed under a topic folder — see `library/lessons/review/empirical-verification.md` (referenced in `fleet.yaml` as `review/empirical-verification`).

## Tone

Lessons should read like postmortem notes — what happened, why it surprised us, what we changed. Not commands; observations.

## Triage record

The verdict each note got when this directory was frozen. A **referential** note was captured
into the vault with the tags `lesson`, its topic folder, and any listed here. A **behavior** note
was not: a vault pointer cannot steer behavior, so it stays in context through the protocol
named. `tests/test_lessons_frozen.py` holds the directory to this table: a note with no row, or
a row with no note, fails.

| Note | Verdict | Extra vault tags, or the in-context home |
|---|---|---|
| `dbt/dim-first-architecture.md` | referential |  |
| `dbt/incremental-unique-key-discipline.md` | referential |  |
| `dbt/parse-vs-execute-time.md` | referential |  |
| `dbt/semantic-layer-discipline.md` | referential |  |
| `design/addition-earns-place.md` | referential |  |
| `design/whitespace-earns-weight.md` | referential |  |
| `migration/dotenv-export-prefix.md` | referential | `env` |
| `migration/preserve-existing-env.md` | referential | `env` |
| `migration/tmux-server-env-inheritance.md` | referential | `tmux`, `env` |
| `railway/fail-loud.md` | referential |  |
| `raspberry-pi/sdhci-uhs-quirk.md` | referential | `hardware` |
| `review/empirical-verification.md` | referential |  |
| `review/mutation-testing-default.md` | referential |  |
| `review/root-cause-not-symptom.md` | referential |  |
| `review/stacked-pr-squash-corruption.md` | referential | `git` |
| `snowflake/clustering-earns-its-cost.md` | referential |  |
| `snowflake/transient-table-recovery.md` | referential |  |
| `telegram/mcp-drops.md` | referential |  |
| `telegram/orphaned-poller-single-consumer.md` | referential |  |
| `telegram/plain-text-escape-incident.md` | referential |  |
| `private-repo-screenshots.md` | referential | `github`, `screenshots` |
| `telegram-bot-group-setup.md` | referential | `telegram`, `setup` |
| `messaging-channel-discipline.md` | behavior | `protocols/messaging-channel-discipline`, written for it |
| `tmux-dispatch-shell-expansion.md` | behavior | `protocols/dispatch`, which already carried `set +H;` |
| `orchestration/consensus-before-escalation.md` | behavior | `protocols/consensus-loop`, which already carried the loop |
