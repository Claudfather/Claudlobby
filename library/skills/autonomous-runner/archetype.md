# Autonomous Worker — Bot Archetype

A worker bot that picks GitHub issues from a target repo per a cadence, classifies risk, and runs a clauDNA `--auto` skill (typically `/claudna:implement-plan`) to resolve them. Never merges. Reports outcomes to Telegram.

This archetype is the recipe for composing a bot around the `autonomous-runner` skill. If/when other archetypes emerge (Manager Bot, Auditor Bot, Reviewer Bot), promote shared archetype documentation to a top-level `library/archetypes/` category.

## When to use

- You have a clauDNA-installed bot fleet
- You have a target repository with a healthy backlog of well-formed issues (each containing an `## Implementation Plan` section, ideally produced by `/claudna:tech-debt --auto` or similar)
- You want overnight or cadence-based PR generation without per-issue human intervention
- You're OK with PRs awaiting a human review before merge (the contract is hard: this bot never merges)

## Composition

- **Expertise:** match the target repo's domain (`data-engineering` for dbt, `software-engineering` for general repos, `frontend-design` for UI repos, etc.)
- **Skills:** include `autonomous-runner`. Other skills like `dispatch` or `delegate` are NOT needed (this is a worker, not a manager).
- **Protocols:** `continuous-autonomous-mode` (for cadence + pause discipline), `report-back` (for Telegram), `verify-before-merge` (defensive — wrapper doesn't merge, but the protocol's mindset matters).
- **Guardrails:** `no-push-main` (worker should never push to main directly; PRs only), `pii-protection` if the target repo touches sensitive data.
- **Telegram:** own bot token. Posts to the squad chat for visibility.

## Example fleet.yaml block

See `fleet.yaml.example` for a complete example (the commented `dbt-auto-bot` entry).

## Cadence sizing

- **15m**: aggressive — useful for repos with rapid issue churn and many small fixes. Risk: noise on Telegram, quota burn.
- **1h** (recommended default): steady throughput, easy to reason about.
- **6h** to **24h**: low-key background presence. Good for repos where issues accumulate over days.

## Picker scoring

- **mission_alignment** (recommended): the bot reads `PROJECT_MISSION.md` from the target repo's default branch and scores issues against the north star. Highest-aligned issue wins each tick. Requires `PROJECT_MISSION.md` to exist in the target repo.
- **recency**: simplest. Picks the most recently created eligible issue. Good for fast-moving repos.
- **priority_label**: respects issue labels (`priority:critical`, `priority:high`, etc.). Good for repos that maintain priority discipline.

## Bypass tuning

- Default (`block_on: [structural]`): conservative. Mechanical and localized changes proceed; structural changes get a `needs-input` label.
- `block_on: []`: trust the wrapper's tripwires and the clauDNA skill's verification. Useful in tightly tested codebases.
- `block_on: [structural, localized]`: most conservative — only pure mechanical changes proceed. Useful for early-stage bots, or repos where headless changes need extra caution.

## On-outcome policy

Recommended defaults:
- `completed: report` — post to Telegram and continue
- `bypassed: report` — post the bypass reason, continue (the issue was labeled `needs-input`)
- `needs_input: report_and_pause` — post the synthesis-unresolvable details, pause the bot until a human resolves them
- `blocked: report_and_pause` — environment failure; surface for investigation before retrying
- `partial: report` — note the partial outcome, continue (the partial PR is already open)

## What this archetype is NOT

- **Not a manager bot.** Doesn't dispatch other bots. Doesn't orchestrate multi-bot work.
- **Not a planning bot.** Consumes plans (issues with implementation details); doesn't write plans. Pair with a separate `claudna-planner` bot if you want auto-planned issues.
- **Not a merge bot.** Opens PRs. Humans (or a separate reviewer bot via the manager protocol) merge.
