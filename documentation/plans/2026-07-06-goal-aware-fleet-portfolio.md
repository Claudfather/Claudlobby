---
title: "Goal-Aware Fleet: Fleet Mission, Projects Tier, Workstream Registry, Task IDs, Ignition"
type: plan
status: draft
owner: claude
tags: [product-vision, config, orchestration, observability, autonomy]
created: 2026-07-06
updated: 2026-07-06
ironclad: pending — forks F1–F6 open; F2/F5/F6 need the fleet owner, F1/F3/F4 lockable by manager
---

# Goal-Aware Fleet: Fleet Mission, Projects Tier, Workstream Registry, Task IDs, Ignition

## Goal

Make a fleet *goal-seeking* instead of merely *task-executing*: give it a durable fleet-level mission, a first-class registry of the projects it serves (each with its own definition of "done"), a bounded portfolio of workstreams it can track across many unrelated repos without human bookkeeping, task-identity through the dispatch↔report loop, and composed (not hand-wired) ignition so idle bots start mission-aligned work on their own.

This extends the north star — *"trivial to run a fleet of distinct, cooperating bots on cheap hardware"* — from "run" toward "run **toward something**": the fleet knows what the operator is trying to achieve, picks work that advances it, validates that work at the rigor each project demands, and reports the portfolio daily. Five gaps close: (1) no goal object above the per-bot `mission:` field; (2) no cross-project workstream state anywhere; (3) sprint/runner closure rules are per-*bot* (merge-policy guardrails) when they need to be per-*project*; (4) dispatch↔report correlation is stringly `(bot, timestamp)` with no task identity; (5) the self-start machinery exists but nothing composed ever fires it.

**Hard constraint (repo hygiene, MANDATORY):** every mechanism here is generic OSS; every *content* instance (real missions, project names, metrics) lives in the gitignored fleet overlay (`local/<fleet>/`) or `.env`. Examples use placeholder ventures only. This applies to this plan, its issues, and its commit messages.

## Current State

Verified against the repo 2026-07-06 (anchors re-checked by direct read, not inherited from exploration notes):

- **Goal hierarchy stops at the bot.** `BotConfig.mission` exists (`config.py:188`, one-paragraph charter → `## Mission` in CLAUDE.md). Target repos get `PROJECT_MISSION.md` via the `/mission` skill, and `/autonomous-sprint` scores backlog against it (mission 40 / impact 25 / effort 20 / deps 15). There is **no fleet-level mission**: nothing states what the *fleet as a whole* serves, so multi-repo prioritization has no anchor.
- **No workstream state.** `state/fleet-state.json` holds per-bot `{status, current_task, current_repo, last_completed}` only, written by `fleet-state-update.sh` under `with_lock` (`:59,:86`; flock or mkdir-spinlock fallback for macOS). A grep for "workstream" across `documentation/` returns zero hits. `report-back.jsonl` + `dispatch-log.jsonl` record events, not goals.
- **Dispatch has no task identity.** `dispatch-task.sh` appends `{ts,manager,bot,task,dispatched_at,expected_by}` (`:16-17,:92`) where `task` is free text. `report-back.sh` builds `[BOTREPORT] <bot> | <status> | <summary>` (`:6,:67`) with no task reference. `dispatch-overdue.py` matches on lower-cased bot name + `ts >= dispatched_at` (`:109,:117`) — one terminal report closes **every** open dispatch for that bot, and a dispatch that never gets a terminal report is re-flagged until the #460 max-age. Open issues #447 (lens dispatches never emit terminal reports → perpetual overdue false-positives) and #467 (no rotation on either ledger) are symptoms of this shape.
- **Closure rigor is bot-scoped, not project-scoped.** The merge-policy guardrail family (`merge-policy-human`, `merge-policy-auto-after-review`, …) attaches to a *bot*. A bot working two repos gets one bar. Nothing lets one project require a posted preview link + human ack while another auto-closes on green CI.
- **Ignition is unwired.** `library/skills/autonomous-runner/SKILL.md:14` claims the skill "is invoked by the bot's `lib/start-bot.sh` lifecycle on each cadence tick" — **no such invocation exists** (grep across `lib/` and `claudlobby/system.yaml`: zero matches). Phase 4 Part C (#294) never deployed. `lib/sprint-trigger.sh` requires hand-wired cron (header comment) and is not a `system.yaml` job.
- **Three divergent busy heuristics.** Keepalive is marker-first (`data/.last-tool-call`, #472) with an `esc to interrupt` pane fallback; `sprint-trigger.sh:30` still greps a spinner-verb list (`Thinking|Running|…`); `bot-sweep-cron.sh:45` greps a *different* verb list. These drift at different rates against Claude Code UI changes.
- **Config tiers today:** package-owned `claudlobby/system.yaml` (host jobs + fleet defaults; resolved by `_resolve_system_yaml`, `config.py:774-791`) → `fleet.yaml` `defaults:` → bot stanza, merged in `load_fleet` (`config.py:880-898`). The dormant-job pattern exists and works: `weekly-worker-restart` ships `enroll: false` (`system.yaml:112`) and a fleet opts in with `jobs: { weekly-worker-restart: { enroll: true } }` — field-level spread merge. Root ships `fleet.yaml.example` but **no** `system.yaml.example` and no third tier for projects.
- **Prior art this plan mirrors:** (a) the system-defaults tier (`documentation/plans/2026-06-09-system-defaults-tier.md`) for adding a config tier through the load→validate→compose pathway; (b) the code-audit-sweep plan's ledger lesson — its local JSON tracker was sunset for an external authoritative ledger because *local mutable trackers drift and race*; the workstream registry (F2) must answer why its local SSOT doesn't repeat that mistake (answer: single-writer helper + lease/reap lifecycle + privacy: workstream content names operator ventures and cannot live in public GitHub issues); (c) `test_lifecycle_names.py` / `test_lifecycle_sockets.py` as the cross-script-agreement test template.
- **Companion surfaces that consume this plan's outputs:** `/autonomous-sprint` + `/autonomous-runner` (closure gates, work picking), `fleet-pulse.sh` (new checks ride the existing debounced `notify_manager` path), `claudlobby report-back`/`events`/`status` CLIs (new fields), compound-play issues #264 (morning dashboard), #294 (runner deployment), #374 (lifecycle automation).

## Architecture

```
                 system.yaml (package)          HOW the platform runs
                        ▼ merge (existing)
                 fleet.yaml (+ mission)         WHO the bots are, WHAT the fleet serves
                        ▼ ref (new)
                 projects.yaml                  WHAT the work is, what "done" requires per project
                        ▼ compose
   CLAUDE.md (## Fleet Mission, ## Projects) + bot.conf (PROJECT_*, WORKSTREAM_* env)
                        ▼ runtime
   dispatch-task ──mints──▶ task_id ──▶ [BOTCOMMAND …|task:<id>] ──▶ worker
        │                                                              │
        ▼                                                              ▼
   dispatch-log.jsonl ◀──join on task_id──  report-back.jsonl ◀── [BOTREPORT …|task:<id>]
        │                                                              │
        └────────────▶ state/workstreams.json (single-writer) ◀────────┘
                        ▲ lease/renew/close      │
   fleet-pulse: workstream_stalled, gate_pending │ weekly reaper → workstreams-archive.jsonl
                                                 ▼
                        morning-brief job ──▶ Telegram portfolio digest
   ignition: sprint-trigger + runner-tick as composed dormant jobs (enroll:true per fleet),
             all on one busy-check SSOT (marker-first)
```

Goal hierarchy, top to bottom: **fleet mission** (fleet.yaml) → **project** (projects.yaml entry, with validation tier + metrics) → **workstream** (registry entry, bounded, leased) → **task** (minted id through dispatch↔report). Each layer references the one above by id/path; nothing is inferred by name-matching.

## Phases

### Phase 1: `projects.yaml` — the third config tier
New optional per-fleet file beside `fleet.yaml` (overlay: `local/<fleet>/projects.yaml`; root mode: `<root>/projects.yaml`), parsed through the **same pathway** as fleet.yaml: dataclasses in `config.py` (`ProjectConfig`, `ProjectValidationConfig`, `ProjectMetricConfig`), loaded by `load_fleet` alongside the fleet doc, validated in `validator.py` (unknown keys, repo-list shape, tier enum, cross-refs from bots/workstreams), composed by `composer.py`. Schema per project: `title`, `repos: []` (the join key — closure rules resolve by repo match, no new per-bot field), `mission_file` (optional path, overlay-relative), `validation: { tier: auto|review|preview|human, preview: {...}, notes }`, `metrics: [{name, source, target, cadence}]` (schema + composition now; *measurement automation explicitly out of scope*, reserved for the metrics plan). Composition: a `## Projects` table in manager CLAUDE.md; `PROJECT_VALIDATION_<slug>` env in the sprint-owner's bot.conf. Deliverables include **`projects.yaml.example` at repo root** with a dummy project exercising every field, and cross-references from `fleet.yaml.example`.
*Standalone value:* a fleet can declare per-project "done" bars and validate the file — even before anything consumes them.

### Phase 2: Fleet-level mission
`fleet.yaml` gains `mission:` (inline paragraph) and `mission_file:` (overlay-relative path to fuller markdown; mutually exclusive — validator error if both). Composed as `## Fleet Mission` into CLAUDE.md (audience per Fork F6). `FLEET_MISSION_FILE` lands in bot.conf for skills to read at runtime. `/autonomous-sprint` and `/autonomous-runner` SKILL.md gain a "resolve the goal chain: fleet mission → project mission → issue" preamble. Root gains **`system.yaml.example`** — a commented copy of the package tier documenting host jobs, the defaults merge, and the dormant-job opt-in pattern (documentation artifact; the live file stays package-owned, per `_resolve_system_yaml`). The three root examples now tell one story: system (platform) → fleet (bots, mission) → projects (work, done-bars), placeholder content throughout.
*Standalone value:* every bot knows what the fleet serves; the three-tier config story is documented at the root.

### Phase 3: Task-ID'd dispatch
`dispatch-task.sh` mints `task_id` = `t-<epochsecs>-<4hex>` (collision-safe, offline, greppable), appends it to the ledger row, and passes `task:<id>` in the `[BOTCOMMAND]` envelope. `report-back.sh` gains `--task <id>` and emits `| task:<id>` in `[BOTREPORT]` + the ledger row. `dispatch-overdue.py` joins on `task_id` when both sides carry it; legacy `(bot, ts)` matching remains as fallback for old rows (no flag-day). The dispatch protocol + worker-lifecycle protocol document the echo contract ("a worker terminal-reports the task id it was given"). Fixes the #447 class (one report no longer closes all open dispatches for a bot; targeted expiry becomes possible) and unblocks per-task workstream progress events. Add ledger rotation for `dispatch-log.jsonl` mirroring report-back's 7-day self-rotation (closes #467).
*Standalone value:* overdue watchdog correlates on identity; ledgers stop growing unbounded.

### Phase 4: Workstream registry
`state/workstreams.json` as the SSOT (Fork F2), mutated **only** through a new `lib/workstream-update.sh` single-writer helper (same `with_lock` + temp-file + `mv` pattern as `fleet-state-update.sh`; subcommands `open|progress|renew|block|close|prune`). Entry: `{id: ws-<slug>, title, project, status: active|blocked|done|abandoned, owner_bot, next, task_ids[], refs:{issues[],prs[]}, opened_ts, last_progress_ts, lease_expires_ts}`. Anti-rot mechanics, all mechanical (no LLM, no human): **hard cap** on active workstreams (`fleet.workstreams.max_active`, default 12 — opening past the cap fails with "close one first"); **lease** (`lease_days`, default 14 — `progress`/`renew` extend it); fleet-pulse gains `workstream_stalled` (active + no progress past threshold) and `workstream_lease_expired` (manager must renew-or-close), both debounced through the existing `notify_manager` path; weekly reaper (rides the existing `data-sweep` job) moves terminal entries to append-only `workstreams-archive.jsonl`. Manager-facing `/workstream` skill wraps the helper (open/list/update/close with goal-chain refs); `report-back.sh --workstream <id>` stamps progress (updates `last_progress_ts`, appends the task_id). Charter markdown is an *optional attachment* under `shared/workstreams/` for the few that need prose — the registry never requires it (context-bloat control: bots read the one-line-per-entry index; a charter is read only by the bot actively working it). `claudlobby workstreams` CLI for the operator.
*Standalone value:* the fleet tracks a bounded portfolio across unrelated repos, and stalls surface to the manager within a pulse interval.

### Phase 5: Ignition + project-tier closure gates
(a) **Sprint trigger becomes a composed job:** `sprint-trigger` enters `system.yaml defaults.jobs` with `enroll: false` (dormant, mirroring `weekly-worker-restart`); a fleet opts in via `jobs: { sprint-trigger: { enroll: true, schedule: "…" } }`. (b) **Runner tick (closes #294):** new `lib/runner-tick.sh` + dormant `runner-tick` job — iterates bots whose `autonomous_runner.cadence` has elapsed (per-bot `data/.last-runner-tick` stamp), checks idle via the SSOT, injects the wrapper skill invocation; fixes the SKILL.md:14 claim by making it true (via the tick job, not start-bot). (c) **Busy-check SSOT:** extract keepalive's marker-first + `esc to interrupt` fallback into a `bot_is_busy()` helper in `lib-common.sh`; rewire `sprint-trigger.sh:30` and `bot-sweep-cron.sh:45` to it; add a cross-script agreement test in the `test_lifecycle_names.py` mold. (d) **Closure gates:** `/autonomous-sprint` and `/autonomous-runner` resolve the working repo → project → `validation.tier` and refuse to close work below its bar — `auto`: green CI; `review`: reviewer verdict marker; `preview`: preview link posted to Telegram + operator ack recorded; `human`: explicit operator approval. Unknown repo → fleet default tier (`fleet.workstreams.default_tier`, default `review`). fleet-pulse `gate_pending` surfaces work waiting on a human gate so approval latency is visible, debounced like every other pulse check.
*Standalone value:* an opted-in fleet starts mission-aligned work on its own and cannot close it below the project's declared rigor.

### Phase 6: Morning brief + validation + docs
Dormant `morning-brief` fleet job: renders the portfolio (active workstreams with status/next/last-progress, yesterday's completed task_ids + PRs from the ledgers, pending gates, stalls) via a testable Python formatter (`claudlobby/brief.py`, same pattern as `dispatch-overdue.py`), posts through `tg-post.sh`, honoring the telegram-formatting protocol. This is the ship-now slice of #264 (no web UI — the management surface stays Telegram + CLI, per PROJECT_MISSION.md "what we choose not to build"). Extend `lib/validate-bot-change.sh` with the new behavior asserts: task-id round-trip closes exactly its own dispatch (and a second open dispatch survives); `workstream_stalled` + `gate_pending` fire and reach the manager; a dormant→enrolled `sprint-trigger` composes correct units; brief renders from fixture ledgers. Unit tests per composer/validator change throughout every phase (this phase adds the end-to-end pass). Docs: `documentation/architecture/overview.md` gains the goal-hierarchy section; new `documentation/guides/goal-aware-fleet.md` walks mission → project → workstream → task with the placeholder venture; schema docs updated (`fleet-yaml-schema.md` + new `projects-yaml-schema.md`).
*Standalone value:* the operator reads one Telegram digest per morning and trusts it; every new behavior is asserted by the harness, per the mandatory Deliver→Recompose→Observe loop.

## Decision Forks

### Fork F1: Where the projects tier lives
- **Context:** Per-project config needs a home that scales independently of bot topology and stays out of OSS.
- **Options:** **(a)** separate `projects.yaml` beside fleet.yaml (overlay + root modes), loaded/validated/composed through the fleet.yaml pathway; (b) a `projects:` block inside fleet.yaml; (c) per-repo only (extend `PROJECT_MISSION.md` with machine-readable frontmatter).
- **Lean:** **(a).** Mirrors how the system tier was introduced; keeps fleet.yaml about *bots*; (c) fails for repos the fleet doesn't own the conventions of, and puts operator config in target repos.
- **Ratifier:** framework/manager. **Status:** open.

### Fork F2: Workstream SSOT substrate
- **Context:** The code-audit-sweep plan sunset a local JSON tracker for GitHub-as-ledger (drift + select→run→log races). Why is a local file right here?
- **Options:** **(a)** `state/workstreams.json` behind a single-writer locked helper + lease/cap/reaper lifecycle; (b) GitHub issues in a private ops repo; (c) markdown files in `shared/workstreams/` as primary.
- **Lean:** **(a).** The audit tracker's failure modes were *multi-writer no-lock* and *no lifecycle* — both addressed by construction here. (b) leaks operator ventures into GitHub and adds a hosted dependency to a local-first core loop (and a private-repo variant still couples portfolio state to network availability); (c) is unbounded prose — the "getting lost" failure. Markdown demotes to optional attachment.
- **Ratifier:** **fleet owner** (this is the "managed robustly without oversight" requirement). **Status:** open.

### Fork F3: task_id minting authority + format
- **Context:** Identity must exist before the worker sees the task, survive offline operation, and be echo-able through a tmux text envelope.
- **Options:** **(a)** entry point mints (`dispatch-task.sh`; runner/manager mint at their entry) as `t-<epochsecs>-<4hex>`, ledger row is SSOT; (b) GitHub issue/PR number as id; (c) UUIDv4.
- **Lean:** **(a).** (b) fails for non-issue work and offline; (c) is hostile to grep/eyeballs in a text protocol. GitHub refs ride along as foreign keys.
- **Ratifier:** framework/manager. **Status:** open.

### Fork F4: Validation-tier enforcement point
- **Context:** Where is a closure gate actually enforced — skill procedure, lib script, or watchdog?
- **Options:** **(a)** skill-procedure enforcement (sprint/runner resolve tier and follow it) + mechanical `gate_pending` fleet-pulse surfacing as backstop; (b) a lib-script hard gate that refuses to record a terminal report below the bar; (c) pulse-only (detect after the fact).
- **Lean:** **(a).** Matches how every closure behavior works today (skills own procedure; pulse owns visibility); (b) turns `report-back.sh` into a policy engine and blocks legitimate `failed`/`blocked` reports; (c) closes the barn door late. The harness asserts (a)'s observable half.
- **Ratifier:** framework/manager. **Status:** open.

### Fork F5: Ignition default state
- **Context:** Should self-start machinery be live by default for every fleet once merged?
- **Options:** **(a)** dormant (`enroll: false`) with per-fleet opt-in — the `weekly-worker-restart` precedent; (b) enabled by default with a kill switch.
- **Lean:** **(a).** Autonomous work initiation is the single most surprising behavior a default could ship; the dormant-gate pattern exists precisely for this class. Revisit the default after the loop has run for weeks on a reference fleet.
- **Ratifier:** **fleet owner.** **Status:** open.

### Fork F6: Fleet-mission composition audience
- **Context:** Who gets the fleet mission in CLAUDE.md — every bot always, or managers fully and workers minimally?
- **Options:** **(a)** every bot gets `## Fleet Mission` with the inline paragraph; `mission_file` body composes for managers only, workers get the path reference; (b) managers only; (c) full text for everyone.
- **Lean:** **(a).** Workers picking autonomous work need the anchor (a paragraph is cheap); a multi-page mission_file in every worker's context is exactly the bloat Fork F2's cap exists to prevent; (b) starves runner-driven workers of the goal chain.
- **Ratifier:** **fleet owner** (context-budget call). **Status:** open.

## Dependencies

| Dependency | Blocks | Risk |
|---|---|---|
| F1, F3, F4 locked (manager-lockable) | Phase 1 schema; Phase 3 format; Phase 5d gates | Low |
| F2, F5, F6 ratified (fleet owner) | Phase 4 substrate; Phase 5 default; Phase 2 composition | Low — options are well-separated |
| Phase 1 (projects tier) | Phase 5d (gates resolve tiers); Phase 4 (`project` refs validate) | Low — additive schema |
| Phase 3 (task ids) | Phase 4 (`task_ids[]`, progress stamps) | Med — touches dispatch hot path, mitigated by fallback matching |
| Phase 4 (registry) | Phase 6 (brief reads it); Phase 5 (sprint opens/updates workstreams) | Med |
| Phase 2 (mission) | Phase 5 (goal-chain preamble) | Low |
| clauDNA sibling: none required | — | sprint/runner SKILL.md edits are claudlobby-owned |

## Risks

| Risk | Sev | Mitigation |
|---|---|---|
| Registry rots into junk (the operator's stated fear) | High | Cap + lease + reaper are *mechanical*; nothing stays active without a progress or renew event; pulse nags the manager, never the human; archive is append-only JSONL, rotated |
| Registry drifts from reality (audit-tracker failure mode) | High | Single-writer helper is the only mutation path; progress stamps ride existing report-back flow (no separate bookkeeping step for workers); harness asserts stall→surface→close round-trip |
| Task-id echo breaks on old/uncomposed bots | Med | Join prefers task_id, falls back to legacy `(bot, ts)`; protocols updated; no flag-day |
| Gate friction: human-tier work piles up silently | Med | `gate_pending` pulse check + morning-brief "pending gates" section make approval latency visible |
| Ignition fires on a busy/broken bot | Med | Single busy SSOT (marker-first) + runner's existing lockfile/quota; dormant default (F5) keeps un-reviewed fleets inert |
| Venture-specific content leaks into OSS via examples/tests/plan | High | Placeholder-only rule enforced in review; examples ship fake ventures; this plan and its issues follow it |
| Composition surface growth slows `generate` | Low | New sections are O(projects + workstreams-cap) strings; no network, no LLM |
