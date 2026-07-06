---
title: "Goal-Aware Fleet: Fleet Mission, Projects Tier, Workstream Registry, Task IDs, Ignition"
type: plan
status: approved
owner: claude
tags: [product-vision, config, orchestration, observability, autonomy]
created: 2026-07-06
updated: 2026-07-06
ironclad: cycle-1 complete (9 lenses, 2 Blockers + 7 Risks + 12 Gaps — ALL folded); B1 resolved (per-fleet registry residence), B2 owner-ratified (mission pairing rule, F6 amended in place); ALL 6 forks locked (fleet owner, 2026-07-06); cycle-2 lock-only: CONVERGED — ready for /implement-plan
---

# Goal-Aware Fleet: Fleet Mission, Projects Tier, Workstream Registry, Task IDs, Ignition

## Goal

Make a fleet *goal-seeking* instead of merely *task-executing*: give it a durable fleet-level mission, a first-class registry of the projects it serves (each with its own definition of "done"), a bounded portfolio of workstreams it can track across many unrelated repos without human bookkeeping, task-identity through the dispatch↔report loop, and composed (not hand-wired) ignition so idle bots start mission-aligned work on their own.

This extends the north star — *"trivial to run a fleet of distinct, cooperating bots on cheap hardware"* — from "run" toward "run **toward something**". Five gaps close: (1) no goal object above the per-bot `mission:` field; (2) no cross-project workstream state anywhere; (3) sprint/runner closure rules are per-*bot* (merge-policy guardrails) when they need to be per-*project*; (4) dispatch↔report correlation is stringly `(bot, timestamp)` with no task identity; (5) the self-start machinery exists but nothing composed ever fires it.

**Honesty note (ironclad):** "goal-seeking" is validated mechanically in-repo (composition, plumbing, gates) and *behaviorally* only by soak observation — no headless assert can watch an LLM pick mission-aligned work. The soak checklist (Phase 6) requires a cited observation per the Deliver→Observe discipline; the guide states the boundary.

**Hard constraint (repo hygiene, MANDATORY):** every mechanism here is generic OSS; every *content* instance (real missions, project names, metrics) lives in the gitignored fleet overlay (`local/<fleet>/`) or `.env`. Examples use placeholder ventures only. This applies to this plan, its issues, and its commit messages.

## Current State

Verified against the repo 2026-07-06; all anchors below re-confirmed by three independent ironclad codebase lenses (each exact or within 2 lines).

- **Goal hierarchy stops at the bot.** `BotConfig.mission` exists (`config.py:188`, one-paragraph charter → `## Mission` in CLAUDE.md). Target repos get `PROJECT_MISSION.md` via the `/mission` skill, and `/autonomous-sprint` scores backlog against it (mission 40 / impact 25 / effort 20 / deps 15). There is **no fleet-level mission**: nothing states what the *fleet as a whole* serves, so multi-repo prioritization has no anchor.
- **No workstream state.** `state/fleet-state.json` holds per-bot `{status, current_task, current_repo, last_completed}` only, written by `fleet-state-update.sh` under `with_lock` (`:59,:86`; flock or mkdir-spinlock fallback for macOS; its own header documents the design as sound below ~50 bots). A grep for "workstream" across `documentation/` returns zero hits.
- **State residence is host-global today.** `dispatch-task.sh:80-82`, `fleet-pulse.sh:47-52`, and `composer.py:371` all resolve one shared `state/` across every fleet on the host, while `report-back.jsonl` resolves per-fleet (`local/<fleet>/runtime/`). Any new fleet-scoped state must pick a side explicitly (ironclad B1).
- **Dispatch has no task identity.** `dispatch-task.sh` appends `{ts,manager,bot,task,dispatched_at,expected_by}` (`:16-17,:92`) where `task` is free text. `report-back.sh` builds `[BOTREPORT] <bot> | <status> | <summary>` (`:6,:67`) with no task reference. `dispatch-overdue.py` matches on lower-cased bot name + `ts >= dispatched_at` (`:109,:117`) — one terminal report closes **every** open dispatch for that bot. Open issue #447 (lens dispatches never emit terminal reports → perpetual overdue false-positives) is a symptom — and also proof that **LLM envelope non-compliance is normal**, which shapes the join rules below. #467's live half is `dispatch-log.jsonl` (never trimmed); `report-back.jsonl` already self-rotates (since #287).
- **Closure rigor is bot-scoped, not project-scoped.** The merge-policy guardrail family attaches to a *bot*. A bot working two repos gets one bar. Nothing lets one project require a posted preview link + human ack while another auto-closes on green CI.
- **Ignition is unwired.** `library/skills/autonomous-runner/SKILL.md:14` claims the skill "is invoked by the bot's `lib/start-bot.sh` lifecycle on each cadence tick" — **no such invocation exists** (zero matches in `lib/` and `claudlobby/system.yaml`); the composer renders `autonomous_runner` into CLAUDE.md only, nothing into `bot.conf`. `lib/sprint-trigger.sh` requires hand-wired cron, reads `MANAGER_TMUX` from its own env with a `claude-bot` default (`:15`) — composed timer units inject only `CLAUDLOBBY_ROOT`/fleet (`composer.py:1505-1508`), so composing it *as-is* would SKIP forever. Issue #294 (autonomous-runner validation deployment) never deployed.
- **Three divergent busy heuristics.** Keepalive is marker-first (`data/.last-tool-call`, #472) with an `esc to interrupt` pane fallback; `sprint-trigger.sh:30` greps a spinner-verb list; `bot-sweep-cron.sh:45` greps a *different* verb list. These drift at different rates against Claude Code UI changes.
- **Config tiers today:** package-owned `claudlobby/system.yaml` (host jobs + fleet defaults; `_resolve_system_yaml`, `config.py:774-791`) → `fleet.yaml` `defaults:` → bot stanza (`load_fleet`, `config.py:880-898`). The dormant-job pattern is proven: `weekly-worker-restart` ships `enroll: false` (`system.yaml:112`); a fleet opts in via field-level spread merge (`config.py:832-844`). **launchd caveat (ironclad):** the OnCalendar→plist translation keeps only HH:MM + weekday (`composer.py:1601-1607`) — sub-daily cron `schedule:` values silently degrade to once-daily on macOS; new jobs that need sub-daily cadence must use `interval:`.
- **Prior art this plan mirrors:** (a) the system-defaults tier (`documentation/specs/system-defaults-tier.md`; an in-flight docs reorg is moving it to `documentation/plans/2026-06-09-…`) for adding a config tier through load→validate→compose; (b) the code-audit-sweep plan's ledger lesson — its tracker died as *a lockless cache treated as source of truth*; Fork F2 records why the registry is not that; (c) `test_lifecycle_names.py` as the cross-script-agreement test template; (d) per-bot socket isolation (`documentation/planning/2026-06-14-per-bot-tmux-socket-isolation.md`, same reorg caveat) for the SSOT-helper-plus-census migration shape.
- **Companion surfaces that consume this plan's outputs:** `/autonomous-sprint` + `/autonomous-runner` (gates, work picking), `fleet-pulse.sh` (new checks ride the debounced `notify_manager` path; debounce is one-shot per episode), `claudlobby report-back`/`events`/`status` CLIs, compound-play issues #264 (morning dashboard), #294 (runner deployment — this plan **unblocks** it; the Phase-4/6 harness asserts substitute for its validation-deployment scope), #374 (lifecycle automation).

## Architecture

```
                 system.yaml (package)          HOW the platform runs
                        ▼ merge (existing)
                 fleet.yaml (+ mission pair)    WHO the bots are, WHAT the fleet serves
                        ▼ ref (new)
                 projects.yaml                  WHAT the work is, what "done" requires per project
                        ▼ compose
   CLAUDE.md (## Fleet Mission, ## Projects) + EVERY bot.conf (PROJECT_TIER_*, PROJECT_REPOS_*,
                                                FLEET_MISSION_FILE, AUTONOMOUS_RUNNER_*)
                        ▼ runtime
   dispatch-task ──mints──▶ task_id ──▶ [BOTCOMMAND …|task:<id>] ──▶ worker
        │   └─(--workstream: manager-side attach at mint)             │
        ▼                                                             ▼
   dispatch-log.jsonl ◀──join on task_id──  report-back.jsonl ◀── [BOTREPORT …|task:<id>|tier:<resolved>]
        │                                                             │
        └──▶ <fleet-state-dir>/workstreams.json (per-fleet, single-writer) ◀──┘
                 ▲ lease/renew/close       │ weekly reaper → workstreams-archive.jsonl
   fleet-pulse: workstream_stalled, lease_expired, gate_pending
                                           ▼
                 morning-brief job ──▶ Telegram portfolio digest (chunked)
   ignition: sprint-trigger + runner-tick as composed dormant jobs (interval-based),
             all on one busy-check SSOT (marker-first)
```

Goal hierarchy, top to bottom: **fleet mission** (fleet.yaml) → **project** (projects.yaml entry, with validation tier) → **workstream** (registry entry, bounded, leased) → **task** (minted id through dispatch↔report). Each layer references the one above by id/path; nothing is inferred by name-matching. Two delivery tracks after the shared foundations: **portfolio** (task ids → registry → brief) and **ignition** (composed triggers + gates), converging on the soak.

## Phases

Sizing: S / M / L per house convention. Sequencing: **P1 ‖ P2 ‖ P3 → P4 → P5 → P7**, with **P6 (ignition) gated only on {P1, P2, P3}** so the soak starts while the portfolio track is still landing. Critical path for the full outcome: P2 → P4 → P5 → P7; earliest soak: P1+P2+P3 → P6.

### Phase 1 (S): Busy-check SSOT — standalone first PR
Extract keepalive's marker-first + `esc to interrupt` fallback into `bot_is_busy()` in `lib-common.sh`, taking the **bot dir** (marker path derives from it; a session-name→bot-dir reverse lookup helper covers `bot-sweep-cron.sh`, whose cron interface passes session names). Rewire `sprint-trigger.sh:30` and `bot-sweep-cron.sh:45` to it. Cross-script agreement test in the `test_lifecycle_names.py` mold: one fixture pane/marker set, every consumer returns the same verdict, including the two regression cases the keepalive harness already guards (esc-pane ⇒ BUSY; idle-looking pane + fresh marker ⇒ BUSY).
*Standalone value:* the live heuristic drift (three divergent regexes) ends now; every later phase inherits one truth. Zero dependencies.

### Phase 2 (M): `projects.yaml` — the third config tier
New optional per-fleet file beside `fleet.yaml` (overlay: `local/<fleet>/projects.yaml`; root mode: `<root>/projects.yaml`), parsed through the **same pathway** as fleet.yaml: dataclasses in `config.py` (`ProjectConfig`, `ProjectValidationConfig`), loaded by `load_fleet` alongside the fleet doc, validated in `validator.py` (unknown keys; repo-list shape; tier enum; **did-you-mean via the existing `closest_match` pattern**, `validator.py:79-84`; cross-refs from bots/workstreams), composed by `composer.py`. Schema per project: `title`, `repos: []` (the join key — closure rules resolve by repo match), `mission_file` (optional, overlay-relative), `validation: { tier: auto|review|preview|human, preview: {...}, notes }`. **No `metrics:` in v1** (ironclad: three lenses, no in-plan consumer) — `projects.yaml.example` carries a commented block marked *reserved for the metrics plan*. Composition: a `## Projects` table in manager CLAUDE.md; **`PROJECT_TIER_<slug>` + `PROJECT_REPOS_<slug>` into EVERY bot's bot.conf** (slug = the project key, validated like bot ids) — every sprint/runner bot can resolve repo→tier locally; there is no "sprint owner" concept. Deliverables include **`projects.yaml.example` at repo root** exercising every v1 field with a dummy project, cross-referenced from `fleet.yaml.example`.
*Standalone value:* a fleet can declare per-project "done" bars, validated with actionable errors — before anything consumes them.

### Phase 3 (S): Fleet-level mission
`fleet.yaml` gains `mission:` (inline paragraph) and `mission_file:` (overlay-relative path). **Pairing rule (B2, owner-ratified):** `mission:` alone ✓; both ✓; `mission_file:` alone ✗ — validator error: *"mission_file requires mission (the one-paragraph anchor every bot receives)."* Composition per locked F6: every bot gets `## Fleet Mission` with the paragraph; the file body composes for managers only; workers get the path reference. `FLEET_MISSION_FILE` lands in every bot.conf. `/autonomous-sprint` and `/autonomous-runner` SKILL.md gain the goal-chain preamble (fleet mission → project mission → issue). Root gains **`system.yaml.example`** — generated-equivalent documentation of the package tier, **kept in sync by a unit test** asserting it matches `claudlobby/system.yaml` (ironclad: hand-copies drift; the repo already has one dead example pointer). **Mission-doc amendment deliverable:** update the framework's own `PROJECT_MISSION.md` north-star line and `CLAUDE.md` blurb to record the ratified "run → run toward something" extension in generic language — this also formally satisfies the `PROJECT_MISSION.md:60` approval gate for dispatch/lifecycle changes, citing the F1–F6 owner locks. The three root examples now tell one story: system (platform) → fleet (bots, mission) → projects (work, done-bars).
*Standalone value:* every bot knows what the fleet serves; the three-tier config story is documented and drift-proofed at the root.

### Phase 4 (M): Task-ID'd dispatch
`mint_task_id()` in `lib-common.sh` (shared by all entry points) returns `t-<epochsecs>-<4hex>`; **grammar pinned:** `^t-[0-9]+-[0-9a-f]{4}$`, survives `sanitize_tmux_input` untouched. `dispatch-task.sh` mints, appends `task_id` to the ledger row, passes `task:<id>` in the `[BOTCOMMAND]` envelope (raw `dispatch.sh` unchanged — dumb transport). `report-back.sh` gains `--task <id>` → `| task:<id>` in `[BOTREPORT]` + ledger. **Join matrix (ironclad ruling):** id↔id joins exactly; an **id-less terminal report closes only id-less (pre-migration) dispatch rows, never id'd ones** — preserving the #447 fix; the overdue nudge for an id'd dispatch tells the worker to re-report *with the id* (self-heal); a `missing-id` counter accumulates for the brief so echo erosion is visible, not silent. `dispatch-overdue.py` joins on `task_id` when present, legacy `(bot, ts)` only among id-less rows. Rotation for `dispatch-log.jsonl` mirroring report-back's self-rotation (closes the live half of #467). Protocols updated (dispatch, worker-lifecycle: "echo the task id you were given"). **In-phase validation:** unit tests for the join matrix; `validate-bot-change.sh` gains the round-trip assert — an id'd dispatch is closed by its own terminal report *and a second open id'd dispatch for the same bot survives*.
*Standalone value:* the overdue watchdog correlates on identity; ledgers stop growing unbounded; echo failures surface instead of silently degrading.

### Phase 5 (M): Workstream registry
**Residence (B1):** per-fleet — `workstreams.json` lives in the fleet's state dir resolved exactly like `report-back.jsonl` (overlay `local/<fleet>/runtime/…`, root-mode `runtime/fleet/…`); cap, pulse, reaper, and brief all read the same fleet scope; a `fleet` field stamps every entry for defense in depth. Mutated **only** through `lib/workstream-update.sh` (single-writer; `with_lock` + temp-file + `mv`, the `fleet-state-update.sh` pattern; subcommands `open|progress|renew|block|close|prune`). Hand-editing is forbidden — the helper is the only writer; the operator CLI wraps it. Entry: `{id: ws-<slug>, fleet, title, project, status: active|blocked|done|abandoned, owner_bot, next, task_ids[], refs:{issues[],prs[]}, opened_ts, last_progress_ts, lease_expires_ts, renewals[]}`. Anti-rot, all mechanical: **cap** `fleet.workstreams.max_active` (default 12 — documented as manager-attention-span on the reference class, per-fleet knob; exceeding it fails with an actionable message naming the cap, the knob, and the current entries oldest-first as close candidates); **lease** (`lease_days`, default 14; `progress` extends; `renew` requires a note and is logged — **≥2 consecutive renewals with zero progress still flag stalled**); fleet-pulse `workstream_stalled` + `workstream_lease_expired` (debounce key includes the workstream id; nag text carries the remediation command; orphaned `owner_bot` falls back to the team manager); weekly reaper rides `data-sweep` → append-only `workstreams-archive.jsonl`. **Attachment (ironclad):** manager-side at mint — `dispatch-task.sh --workstream <ws-id>` writes the minted task id into the entry atomically via the helper; worker `report-back.sh --workstream` is optional enrichment, never the primary path. `/workstream` manager skill wraps the helper; **read-only** `claudlobby workstreams [list|show <id>]` CLI (writes go through the skill/helper only). Charter markdown is an *optional attachment* under the **fleet overlay** `local/<fleet>/shared/workstreams/` — gitignored by construction; never the root `shared/` (operator bright line: workstream content names ventures); root-mode fleets get a doc warning. **In-phase validation:** helper unit tests (cap, lease, renew-loophole); harness asserts `workstream_stalled` + `lease_expired` fire and reach the manager.
*Standalone value:* the fleet tracks a bounded portfolio across unrelated repos; stalls surface within a pulse interval; nothing rots silently.

### Phase 6 (M): Ignition + project-tier closure gates (soak starts here)
Depends only on {P1, P2, P3} — runs in parallel with P4/P5. (a) **Sprint trigger becomes a composable job:** rework `sprint-trigger.sh` to take `--fleet` and resolve the manager + socket from fleet config (`teams`) at run time instead of trusting env (`MANAGER_TMUX` default `claude-bot` dies); busy check via the P1 SSOT; enters `system.yaml defaults.jobs` as `sprint-trigger` with `enroll: false` and an **`interval:`-based default** (survives launchd; validator warns when a fleet overrides with a sub-daily cron `schedule:` on macOS). (b) **Runner tick (unblocks #294):** new `lib/runner-tick.sh` + dormant `runner-tick` job — iterates bots with `autonomous_runner`, per-bot `data/.last-runner-tick` stamp vs cadence, idle via SSOT, injects the wrapper skill; **new composer deliverable:** `AUTONOMOUS_RUNNER_*` (skill, cadence, target_repo) into bot.conf — and fix the false `SKILL.md:14` invocation claim to describe the tick job. Runner-tick enrollment is *explicitly second*: fleets opt into sprint-trigger first; runner-tick after the sprint soak proves the loop (plan text, not code gate). (c) **Closure gates (locked F4):** `/autonomous-sprint` + `/autonomous-runner` resolve working repo → project → tier from the composed env and refuse to close below the bar — `auto`: green CI; `review`: reviewer verdict marker; `preview`: preview link posted to Telegram + operator ack **recorded in the registry via the helper** (reply-to-approve; CLI fallback documented); `human`: explicit operator approval, same recording path. **Resolution is loud, never silent:** the terminal `[BOTREPORT]` carries `tier:<resolved>` (or `tier:default(<tier>)` when the repo matched no project — surfaced by pulse), so a `human` project can never quietly close at `review`. `gate_pending` is *computed* by fleet-pulse from registry + tier map (no separate writer), debounced like every check. **In-phase validation:** harness asserts a dormant→enrolled sprint-trigger composes correct units on both init systems, fires only on an idle manager (SSOT), and `gate_pending` fires + reaches the manager. **Soak checklist (exit criterion for calling the wedge proven):** ≥2 weeks on a reference fleet with (1) a cited observation that sprint picks reference the mission/projects (Deliver→Observe discipline — no headless assert can see this), (2) zero below-tier closes, (3) false-fire rate ≈ 0 on the busy check. Ops note (non-OSS): the fleet owner can hand-wire today's script now to start observing early; the composed job replaces it on land.
*Standalone value:* an opted-in fleet starts mission-aligned work on its own and cannot close it below the project's declared rigor — and the claim gets soak evidence, not vibes.

### Phase 7 (S/M): Morning brief + docs + end-to-end pass
Dormant `morning-brief` fleet job: renders the portfolio (active workstreams with status/next/last-progress; yesterday's completed task_ids + PRs from the ledgers; **pending gates with age**; stalls; the missing-id counter) via testable `claudlobby/brief.py` (the `dispatch-overdue.py` pattern), **chunked to Telegram's 4096-char cap**, posted through `tg-post.sh` honoring telegram-formatting. Ship-now slice of #264 (no web UI, per PROJECT_MISSION.md). Named fast-follow (not built here): a weekly mission-progress rollup variant once `brief.py` exists. Docs: `documentation/architecture/overview.md` goal-hierarchy section; new `documentation/guides/goal-aware-fleet.md` (walkthrough on the placeholder venture, including the honesty note and soak checklist); `projects-yaml-schema.md`; `fleet-yaml-schema.md` mission/workstreams/jobs updates; **entry points updated:** `getting-started.md`, `README.md`, `environment-variables.md` (new `PROJECT_TIER_*`, `FLEET_MISSION_FILE`, `AUTONOMOUS_RUNNER_*`, `WORKSTREAM_*` vars). **Final validation:** one end-to-end harness scenario chaining the pieces (dispatch with workstream → progress → gate → close → brief renders it), plus `brief.py` fixture tests.
*Standalone value:* the operator reads one trustworthy Telegram digest per morning; every behavior this plan added is asserted where it shipped, then chained once here.

## Decision Forks

### Fork F1: Where the projects tier lives — **LOCKED (a: separate projects.yaml)**
- **Context:** Per-project config needs a home that scales independently of bot topology and stays out of OSS.
- **Options:** **(a)** separate `projects.yaml` beside fleet.yaml (overlay + root modes), loaded/validated/composed through the fleet.yaml pathway; (b) a `projects:` block inside fleet.yaml; (c) per-repo only (extend `PROJECT_MISSION.md` with machine-readable frontmatter).
- **Decision:** **(a) separate `projects.yaml`.** Mirrors how the system tier was introduced; keeps fleet.yaml about *bots*; (c) fails for repos the fleet doesn't own the conventions of, and puts operator config in target repos.
- **Ratifier:** **fleet owner — LOCKED (2026-07-06).** **Status:** locked.

### Fork F2: Workstream SSOT substrate — **LOCKED (a: local JSON + lease/cap/reaper)**
- **Context:** The code-audit-sweep plan sunset a local JSON tracker for GitHub-as-ledger (drift + select→run→log races). Why is a local file right here?
- **Options:** **(a)** per-fleet `workstreams.json` behind a single-writer locked helper + lease/cap/reaper lifecycle; (b) GitHub issues in a private ops repo; (c) markdown files in `shared/workstreams/` as primary.
- **Decision:** **(a) local JSON SSOT + lease/cap/reaper.** The audit tracker's failure modes were *multi-writer no-lock* and *derived second copy of truth that authoritatively lived elsewhere* (its own plan calls it "a cache treated as source of truth"), plus no lifecycle — all addressed by construction: single-writer locked helper (the proven `fleet-state-update.sh` pattern, the *successful* local-state precedent alongside the dispatch/report ledgers), and workstreams have **no external authority to defer to** — the file *is* the truth, not a cache of it. (b) leaks operator ventures into GitHub and adds a hosted dependency to a local-first core loop; (c) is unbounded prose — the "getting lost" failure. Markdown demotes to optional attachment. A hybrid JSON+issue-mirror was considered and rejected: it recreates the exact two-copies problem the audit sweep died from. Residual risk accepted: state is host-local until vault sync carries the registry (future Claudron wiring).
- **Ratifier:** **fleet owner — LOCKED (2026-07-06)** after challenging the lean against the audit-tracker precedent; distinction above recorded as the ratification basis. **Status:** locked.

### Fork F3: task_id minting authority + format — **LOCKED (a: entry-point mints t-<epoch>-<4hex>)**
- **Context:** Identity must exist before the worker sees the task, survive offline operation, and be echo-able through a plain-text tmux envelope.
- **Options:** **(a)** entry point mints (`dispatch-task.sh`; runner/manager mint at their entry) as `t-<epochsecs>-<4hex>`, ledger row is SSOT; (b) GitHub issue/PR number as id; (c) UUIDv4.
- **Decision:** **(a) entry point mints `t-<epochsecs>-<4hex>`** via the shared `mint_task_id()` helper. (b) fails for non-issue work and offline; (c) is hostile to grep/eyeballs in a text protocol. GitHub refs ride along as foreign keys. **Granularity ruling (locked with this fork):** a task is **dispatch-level** — one admitted unit of work (one `[BOTCOMMAND]`, one runner issue-pick, or one accepted operator ask) through one *terminal* report; non-terminal `progress` reports reuse the id; sessions are orthogonal (the id lives in the ledger, so a task survives bot restarts); events (`data/events/*.jsonl`) sit below tasks and never get ids. **Echo-integrity addendum (ironclad):** grammar pinned, strict join matrix (id-less terminal reports never close id'd dispatches), self-heal nudge, missing-id counter.
- **Ratifier:** **fleet owner — LOCKED (2026-07-06).** **Status:** locked.

### Fork F4: Validation-tier enforcement point — **LOCKED (a: skill enforces + pulse surfaces)**
- **Context:** Where is a closure gate actually enforced — skill procedure, lib script, or watchdog?
- **Options:** **(a)** skill-procedure enforcement + mechanical `gate_pending` fleet-pulse surfacing as backstop; (b) a lib-script hard gate refusing to record terminal reports below the bar; (c) pulse-only (detect after the fact).
- **Decision:** **(a) skill-procedure enforcement + `gate_pending` pulse surfacing.** Matches how every closure behavior works today (skills own procedure; pulse owns visibility); (b) turns `report-back.sh` into a policy engine and blocks legitimate `failed`/`blocked` reports; (c) closes the barn door late. **Loud-resolution addendum (ironclad):** resolved tier is reported in the terminal `[BOTREPORT]`; unknown-repo fallback is marked `default(<tier>)` and pulse-surfaced — silent downgrade is impossible by construction. The harness asserts the observable half.
- **Ratifier:** **fleet owner — LOCKED (2026-07-06).** **Status:** locked.

### Fork F5: Ignition default state — **LOCKED (a: dormant, per-fleet opt-in)**
- **Context:** Should self-start machinery be live by default for every fleet once merged?
- **Options:** **(a)** dormant (`enroll: false`) with per-fleet opt-in — the `weekly-worker-restart` precedent; (b) enabled by default with a kill switch.
- **Decision:** **(a) dormant (`enroll: false`), per-fleet opt-in**, with `interval:`-based defaults so `enroll: true` alone is a working opt-in on both init systems. Autonomous work initiation is the single most surprising behavior a default could ship; the dormant-gate pattern exists precisely for this class. Revisit the default after the Phase 6 soak checklist passes on a reference fleet.
- **Ratifier:** **fleet owner — LOCKED (2026-07-06).** **Status:** locked.

### Fork F6: Fleet-mission composition audience — **LOCKED (a: paragraph for all, file body managers-only; amended 2026-07-06)**
- **Context:** Who gets the fleet mission in CLAUDE.md? Workers picking autonomous work need the goal anchor; every composed section is permanent context in every session.
- **Options:** **(a)** inline paragraph for every bot; `mission_file` body composes for managers only, workers get the path reference; (b) managers only; (c) full text for everyone.
- **Decision:** **(a)**, with the **pairing amendment (ironclad B2, owner-ratified 2026-07-06):** `mission:` and `mission_file:` are pairable, and `mission_file:` **requires** `mission:` (the paragraph) so the every-bot anchor F6 guarantees can never be starved by a file-only config. Original mutual-exclusivity contradicted this fork and is dropped. (b) starves runner-driven workers of the goal chain; (c) is the bloat the F2 cap exists to prevent.
- **Ratifier:** **fleet owner — LOCKED (2026-07-06; amendment ratified same day).** **Status:** locked.

## Ratifier Decisions (fleet owner, 2026-07-06)

All six forks resolved in one interactive pass, each on its lean: **F1(a)** · **F2(a)** (ratified after an explicit precedent challenge; distinction recorded in-fork) · **F3(a)** + dispatch-level granularity · **F4(a)** · **F5(a)** · **F6(a)**. Post-ironclad, the owner additionally ratified: the **B2 pairing amendment** to F6, and the **full cycle-1 fold set** (7 Risks, 12 Gaps) as proposed. ✓ all locked — no blockers remain.

## Verification Checklist

Every phase carries its asserts in-phase (ironclad: no back-loaded validation); this is the consolidated exit list:

- [ ] P1: cross-script busy-verdict agreement test green (incl. the two keepalive regression cases).
- [ ] P2: `claudlobby validate` catches bad tier / unknown key / misspelled project with did-you-mean; every bot.conf carries the full `PROJECT_TIER_*`/`PROJECT_REPOS_*` map.
- [ ] P3: mission pairing validator (file-without-paragraph fails); worker vs manager CLAUDE.md composition per F6; `system.yaml.example` sync test.
- [ ] P4: join-matrix unit tests; harness round-trip — id'd dispatch closed by own terminal report, sibling id'd dispatch survives, id-less terminal closes only id-less rows.
- [ ] P5: helper unit tests (cap message, lease, renew-loophole); harness `workstream_stalled` + `lease_expired` fire → manager notified.
- [ ] P6: harness — dormant→enrolled sprint-trigger composes on systemd + launchd, fires only on idle manager, `gate_pending` fires; **soak checklist** (2 weeks reference fleet: cited mission-aligned-pick observation, zero below-tier closes, ~0 false fires).
- [ ] P7: `brief.py` fixture tests (chunking, missing-id counter, pending-gates section); end-to-end harness chain (dispatch→progress→gate→close→brief).
- [ ] All: `claudlobby generate && claudlobby diff` clean on the seed fleet; no venture-specific strings anywhere in committed files (grep gate).

## Companion Plans

- **In-flight docs reorg** (uncommitted on main at time of writing): moves `specs/system-defaults-tier.md` and `planning/2026-06-14-per-bot-tmux-socket-isolation.md` into `documentation/plans/`. This doc cites branch-truth paths; update refs if the reorg lands first.
- **Fleet-observability subsystem:** the three new pulse checks ride `notify_manager` + debounce as-is; only debounce keys gain a workstream dimension (P5).
- **Metrics plan (future):** consumes the reserved `metrics:` block in projects.yaml, the registry's progress stream, and the brief — explicitly out of scope here.
- **Claudron wiring (future):** vault sync carries the registry + charters across hosts (F2 residual risk).
- **#264 / #294 / #374:** P7 ships #264's core slice; P6 unblocks #294 (harness substitutes its validation-deployment scope); the composed-jobs pattern advances #374.

## Dependencies

| Dependency | Blocks | Risk |
|---|---|---|
| All forks F1–F6 + B2 amendment + fold set — **LOCKED/ratified 2026-07-06** | — | Closed |
| P1 (busy SSOT) | P6 (trigger idle checks) | Low — additive helper + two rewires |
| P2 (projects tier) | P5 (project refs validate), P6c (gates) | Low — additive schema |
| P3 (mission) | P6 (goal-chain preamble) | Low |
| P4 (task ids) | P5 (`task_ids[]`, attachment at mint) | Med — dispatch hot path; mitigated by strict join + in-phase harness |
| P5 (registry) | P7 (brief reads it) | Med — new state surface; mitigated by single-writer + in-phase asserts |
| P6 (ignition + gates) | soak evidence for F5 default revisit | Med — first composed autonomy; dormant default + soak checklist |
| P7 (brief + docs) | — | Low |
| clauDNA sibling: none required | — | sprint/runner SKILL.md edits are claudlobby-owned |

## Risks

| Risk | Sev | Mitigation |
|---|---|---|
| Registry rots into junk (operator's stated fear) | High | Cap + lease + reaper are mechanical; renewals are logged and ≥2 zero-progress renewals still stall; pulse nags carry the remediation command; archive is append-only, rotated |
| Registry drifts from reality (audit-tracker failure mode) | High | Single-writer helper is the only mutation path (hand-edits forbidden); attachment happens manager-side at mint; harness asserts stall→surface→close round-trip |
| LLM echo non-compliance corrodes task identity (#447 proves it's normal) | High | Pinned grammar; strict join (id-less never closes id'd); self-heal nudge; missing-id counter in the brief; round-trip assert ships with P4 |
| Cross-fleet state bleed on multi-fleet hosts | High | B1: per-fleet residence matching `report-back.jsonl` + `fleet` field stamp; asserted in P5 tests |
| Venture-specific content leaks into OSS (bright line) | High | Charters pinned to gitignored overlay; placeholder-only examples; grep gate in the checklist; applies to plan/issues/commits |
| Silent validation-tier downgrade | Med | Loud resolution: `tier:<resolved>`/`default(<tier>)` in terminal reports + pulse surfacing; no silent path exists |
| Composed ignition misfires (busy bot, wrong manager, macOS schedule) | Med | P1 SSOT; manager resolved from fleet config at run time; `interval:` defaults + launchd sub-daily warning; dormant default; P6 harness asserts |
| Human-gate work piles up silently | Med | `gate_pending` pulse check + brief "pending gates with age"; ack path specified (reply-to-approve, recorded via helper, CLI fallback) |
| Back-loaded validation never ships (reference class: system-defaults tail, #294) | Med | Asserts distributed in-phase; P7 is a chain of already-shipped asserts, not the first validation |
| Composition surface growth slows `generate` | Low | New sections are O(projects + cap) strings; no network, no LLM |
