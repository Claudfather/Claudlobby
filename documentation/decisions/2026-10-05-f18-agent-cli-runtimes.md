---
title: "Agent-CLI runtimes — Claudlobby composes Claude Code and Codex, by name (#2145 F18)"
type: decision
status: ratified
owner: operator
created: 2026-10-05
tags: [runtime, codex, mission, observability, decision]
links: ["#2145", "#2144", "#2149", "#1997", "#974", "#515"]
supersedes_scope: "PROJECT_MISSION.md's LLM-provider non-goal ('Claudlobby is for Claude Code specifically') and the Claude-Code-only lines of PROJECT_MISSION.md ('What this project is', 'What it's becoming'), README.md (tagline, 'Runs anywhere') and CLAUDE.md (opening line)"
---

# Agent-CLI runtimes — decision

## Junction

Does Claudlobby compose agent CLIs beyond Claude Code? Fork F18 of the runtime-neutral observability epic
(#2145, `documentation/plans/2026-10-04-runtime-neutral-observability-plan.md` §3) put two options:

- **(a)** Claudlobby composes and supervises agent CLIs — Claude Code today, Codex through #2149; a further CLI
  enters by its own ratified fork. Codex is admitted **by name**, not agent CLIs as a class, and the mission's
  "Requires approval" list gains "a new agent CLI runtime".
- **(b)** Claude Code only, as the mission's LLM-provider non-goal read before this decision.

## Context (why this came up now)

- The mission excluded the epic's premise. It described "a fleet of always-on Claude Code bots", "the reference
  runtime for operating Claude Code bots in production", and listed "Per-bot LLM provider abstraction —
  Claudlobby is for Claude Code specifically" under what Claudlobby chooses not to build. `README.md` and
  `CLAUDE.md` opened the same way.
- No fork in F1–F17 decided it. F11 decides **where** Codex launching lives (the execution-adapter companion,
  #2149), not **whether** Claudlobby composes Codex bots, so recording that "F11 supersedes the line" was not an
  option; the question needed its own fork.
- The name clash recorded in #1997: `runtime` already names release activation, the Claude Code binary update
  (`host update runtime`) and the composed-output audit (`config validate --runtime`). #1997 proposed
  `agent_cli:` as the `fleet.yaml` key for this reason; the key name is the epic's §14 Q1, decided separately
  from this fork (answered 2026-10-06 by the operator: `agent_cli:`).

## Decision: **(a)**

Claudlobby composes and supervises agent CLIs — Claude Code today, Codex through #2149; a further CLI enters by its
own ratified fork. The model stays each CLI's concern: there is no LLM-provider abstraction beneath the CLI, and a
bot that is not an agent CLI belongs in a different framework.

**Ratification:** the operator locked F18 (a) on 2026-10-05 —
[FORK-LOCK F18](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-6000050806) on #2144.

## Flip condition

None for Codex. This decision admits Claude Code and Codex **by name**. A further CLI enters only by its own
ratified fork, through the mission's "Requires approval" gate — never by widening this one.

## Impact

- `PROJECT_MISSION.md`: the ratification paragraph (the 2026-07-06 form, #515), the amended "What this project is",
  "What it's becoming" and LLM-provider non-goal lines, the #2145 sprint-focus item and the "A new agent CLI
  runtime" approval gate. `README.md` and `CLAUDE.md` (and its `AGENTS.md` copy) carry the same scope in one line
  each. All land in #2145 P1 Half A.
- The vocabulary (`agent_cli: claude | codex` in `fleet.yaml`, `known_values.KNOWN_AGENT_CLIS`; the plane and the
  session join key call the value the bot's runtime) ships in the same change;
  `codex` is refused by the validator until the execution adapter (#2149) composes and launches it.
- The design-v2 record of the superseded session-identity mechanics is
  `documentation/plans/2026-08-18-observable-plane-design-v2.md` §19 item 9; it is cited here, not restated.
- Coordination: the #974 mission-consolidation branch edits the same mission lines; whichever merges second carries
  this decision's paragraph and line edits onto the other's text.
