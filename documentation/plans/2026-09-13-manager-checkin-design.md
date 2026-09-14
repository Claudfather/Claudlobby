# Manager check-in — the idle-manager ignition loop

**Status:** DESIGN, 2026-09-13. Phase-5 step one, toward the full intake pipeline (§15).
Approved in-session by the operator; awaiting spec review → implementation plan.
Line references are to `origin/main` at `8ff1c8d`.

## 0. TL;DR

The fleet is idle **94.7% of the week**, and the reason is specific: the intake chain
that decides "what should we work on" already exists — mission, scorer, even a
generator — but **nothing ignites it.** This spec adds the missing ignition as a
first-class door. When a manager is idle, a composed fleet job injects `/checkin`;
the manager reads the SSOT (the plane, Claudron, the mission, the issue backlog),
**decides exactly one action** from a toolkit, and **records the decision with its
rationale in the plane**. The human edge becomes conversational and thin. It ships
dormant, is validated empirically, canaries on one fleet, and then becomes a
**manager default via a single registry line** — because a new `requires:`
frontmatter lets the check-in *protocol* bring the check-in *skill* with it.

## 1. The finding

Measured 2026-09-12 over the plane db of one host (two fleets, 18 bots, last 7 days):

| measure | result |
|---|---|
| fleet-active time (any bot busy) | **5.3%** — 532 of 10,066 observed minutes; cross-validated by two methods |
| who is busy *within* active time | the two managers: **43% and 26%**; workers 2–18% |
| worker activity stints | 1–10 stints per bot per *week*, 15–25 min each; one worker had exactly one 14-min stint |
| wall-clock busy % (`utilization.py` today) | ~1–3% — true, and misleading: it reads as laziness where the ledger shows **starvation** |
| task `repo` / `project_key` populated | **0 of 374** assignments |

**Routing was investigated first and shelved.** A title-keyword-vs-declared-expertise
heuristic flagged 25.5% of assignments as "mismatched," but the dominant pattern
(engineers and frontend bots assigned PR reviews) is *intentional peer review*, not
error — and `assignment.assignee` records the *result* of the decision, never the
*criteria*, so the ledger cannot tell a choice from a mistake. A capability registry
plus resolver would actively fight legitimate cross-functional assignment. **Not
built.** What survives of that thread is one small, cheap piece: record the routing
rationale (§7), so choice and error become distinguishable going forward.

**Root cause: an unlit chain.** Every "what to work on" piece exists:

- a mission chain — `fleet.mission` → `PROJECT_MISSION.md` (north star, in-bounds,
  success metrics); per-project rigor tiers in `projects.yaml`;
- a scorer — `/autonomous-sprint` ranks open issues mission 40 / impact 25 /
  effort 20 / deps 15 (`library/skills/autonomous-sprint/SKILL.md:74-81`);
- a generator for an empty backlog — *"If fewer than 5 mission-aligned open issues
  exist: run a product-vision pass to generate new issues… then re-evaluate"*
  (`SKILL.md:59-66`).

And nothing starts it. `lib/sprint-trigger.sh` is hand-wired cron only, never
composed, and defaults `MANAGER_TMUX` to a stale name (`:15`). `autonomous_runner.cadence`
renders into CLAUDE.md (`templates/claude.md.j2:87`) but **`lib/runner-tick.sh` does
not exist** — it is unfireable. Every wired trigger (keepalive, fleet-pulse) is
health-only. Bots boot into `STARTUP_PROMPT` *"Idle and await Telegram messages"*
(`composer.py:1212`). The prose triggers ("fleet idle + backlog non-empty → auto-fire
a sprint", `library/expertise/orchestration.md:59`) need an already-running LLM turn
and so can never fire from idle. The goal-aware plan's own re-audit says it plainly:
*"P6 ignition… Not shipped… neither sprint-trigger nor runner-tick is a composed
job"* (`documentation/plans/2026-07-06-goal-aware-fleet-portfolio.md:25,51`).

**Two constraints the design must honour:**

- *Fatigue is already at the ceiling.* Protocols say *"Idle silence is a bug"*
  (`proactivity-discipline.md:7`); workers post a Telegram milestone every 2–3 min
  of work (`worker-lifecycle.md:117,195`); sprints *"emit everything to Telegram"*;
  the runner beacons "no eligible work" every tick. `token-efficiency` governs
  density *"never frequency"* (`:39`) — **no primitive today reduces message count.**
  Naive ignition would flood the operator.
- *Memory cannot yet say "worth doing now."* Claudron recall runs at session start;
  no note carries an outcome/payoff field (`Claudron/SCHEMA.md:76-112`);
  `CLAUDRON_QUERY_BEFORE` is off by default; the one "what worked / would change"
  rubric (transcript-digest) is dormant and joined to nothing.

## 2. Decisions (ruled by the operator, 2026-09-12/13)

1. **Build B** — the idle-manager check-in as a first-class door, checking against the
   SSOT. **A** (ignite the existing sprint chain) is *in the toolkit*, fired only
   when stated conditions hold, never the reflex — it would lean to mass sprinting.
   Document the build toward **C** (the full intake pipeline, §15).
2. **Ground-and-record.** The LLM keeps the judgment (which bot, which work); the
   *inputs* are data and the *decision* is a recorded fact with rationale.
3. **SSOT, two-lane (F4).** The plane is the single source of truth for every
   runtime fact and operational commitment — **every new thing here lands in the
   plane as an event or construct, never as a side file.** Git stays the SSOT for
   declared configuration; Claudron for knowledge. The plane references both,
   never copies them (consume by contract).
4. **Team loud, human edge thin.** Bot↔bot communication stays constant — that is
   the work happening. The **manager** is the channel to the operator (Telegram),
   in clean updates and requests for direction. Workers may post to Telegram,
   *really* thin.
5. **Ships as a manager default** (the primary carrier), after the rollout ladder.
6. **Equipment linking.** A library item may `requires:` another; the compositor
   brings it. Improve the system as we go.

## 3. Goals and non-goals

**Goals**
- Ignite intake: an idle manager reliably asks "what next?" against the SSOT.
- Every check-in is a recorded, inspectable decision (what it saw, what it chose, why,
  what came of it) — so the behaviour can be judged quickly and tuned on data.
- Fix the human edge: manager→operator is one line + one ask + one pointer, only
  when it matters; workers are thin.
- Ship as a manager default through the repo's own ladder, and make the library
  express "this protocol needs that skill."

**Non-goals (explicitly out)**
- A capability registry or routing resolver (shelved, §1).
- Payoff memory / outcome scoring (C).
- A formal approval door for proposed work (C); per-task human approval in v1.
- Reducing bot↔bot communication.
- Any new plane *table* or task-vocabulary migration.

## 4. Architecture — the loop

```
manager goes idle
  └─ [trigger] the `manager-checkin` fleet job finds it idle, rate-limits via the plane,
     injects /checkin into the manager's pane
       └─ [skill] the manager READS, all SSOT:
             brief --json        recent work · open tasks · stalls · unacked   (plane)
             claudron lookup     knowledge for the current projects            (vault)
             mission + tiers     the goal and each project's rigor             (git)
             gh issues           the external backlog, mission-filtered        (GitHub)
            └─ DECIDES exactly one action:  dispatch | propose | ask | sprint | nothing
                 └─ [record] checkin_decision lands in the plane BEFORE the action runs
                      └─ [act] through existing doors (dispatch-task, emit-batch, tg-post)
                           └─ [protocol] the human edge is shaped: thin, conversational
```

Six pieces: the **trigger** (§5), the **skill** (§6), the **decision record** (§7),
the **intake store** (§8), the **protocol** (§9), and **equipment linking** (§10),
plus the **read door** (§11). Claudron is consulted, never duplicated; the plane holds
every fact; git holds every declaration.

## 5. The trigger — `manager-checkin`, one fleet job

**Not** a unit per manager. The closer precedent is `task-recheck` on main: one
composed fleet job (`system.yaml:483-485`, `interval`) whose script groups by
`assigned_by` and sends per manager via `dispatch.sh` (`commands/task.py:230-276`),
**rate-limiting by a plane read** — `source_ref=task-recheck:<asg>` stamped and read
back (`:465-500`), *"no timer state file to lose or to lie."* Reuse that shape.

`lib/manager-checkin.sh <fleet>`, run by the fleet job `<prefix>.manager-checkin`
(`interval: 900`, **`enroll: false`**), per bot dir:

1. `bot_is_manager` (lib-common; reads the composed `MANAGER_TMUX == BOT_ID`) — else skip.
2. Session up? else skip (`checkin_skipped_down`).
3. `bot_is_busy` → skip and record `checkin_skipped_busy`. **Never inject into a
   live turn** (the `briefing-trigger.sh:56-63` rule).
4. Rate limit: read the newest `checkin_triggered` for this manager from the plane;
   if younger than `CHECKIN_MIN_GAP_S` (default **2700**, 45 min) → skip,
   `checkin_skipped_ratelimit`. **If the plane is unreachable, do not fire** —
   record `checkin_skipped_unreachable` on stderr and exit 0. A money-spending
   action must not run blind; unreachable ≠ nothing-to-do (`source_state.py`).
5. `dispatch.sh <mgr> "/checkin"` (slash-aware, `pane_send_verified` underneath),
   then emit `checkin_triggered` with `source_ref manager-checkin:<bot>:<epoch>`.

**Arming.** A `Switch` row in `claudlobby/switches.py`:
`Switch(name="manager-checkin", polarity=OPT_IN, carrier=ENROLL_FLEET,
why_opt_in="spends money — a manager turn per idle interval")`. The composer stamps
the resolved arming as `Environment=` on the unit (`composer.py:4072-4076`); the
script gates with `switch_is_on` and no-ops loudly (`task-recheck.sh:19-42`);
doctor, status and the validator all render from the registry. That is what
satisfies *no silent switches*.

**Retirement.** `sprint-trigger.sh`'s hand-cron role is superseded by the skill's
`sprint` action; its stale `claude-bot` default goes with it.

Hooks were considered and rejected: no hook fires on idle; a per-call hook has no
15-minute clock and no rate state; it cannot inject a slash command without blocking
every turn; and per-call hooks sit in the "no deployment gate" category that
`switches.py:9,15-20` forces to OPT_IN. Every idle-injection in the estate is
timer-driven through `bot_is_busy` + `dispatch.sh`.

## 6. The `/checkin` skill — the reasoning contract

A fleet-ops skill, `library/skills/checkin/SKILL.md` (claudlobby's by the boundary
placement test: it operates the fleet). The contract is fixed; the judgment is the
manager's. **The skill is a thin reasoning wrapper, tightly coupled to the scripts
that anchor it:** every read goes through a named door (`brief`, `claudron lookup`,
`gh`, the previous check-in row) and every write through `checkin-record.sh` and the
existing dispatch/emit doors — it never freelances a read or a write. That coupling
is what makes the reasoning inspectable and the edges deterministic, and it is what
ships with managers by default (§10).

**READ** — in order, all cheap, all SSOT; each step's failure is *recorded*, never
guessed around:

0. **The previous check-in** — this manager's newest `checkin_decision` row (§7):
   its `inputs_seen` snapshot is *the state at the last check-in*, its `action` and
   `raise` are what was done and what was held. A pure plane read, no state file.
1. `claudlobby brief --bot $BOT_ID --json` — recent work, open tasks, stalls, unacked
   reports (the plane's one read door).
2. `claudron lookup --limit 5 <project>` for the fleet's active projects (knowledge).
3. `PROJECT_MISSION.md` + `PROJECT_TIER_*` / `PROJECT_REPOS_*` from `bot.conf` (goal
   and rigor).
4. `gh issue list --state open` over the fleet's repos, mission-aligned filter (the
   external backlog).
5. **The delta** — now vs. the previous check-in: tasks opened / completed / stalled /
   cleared, issues appeared, messages arrived, held items still pending. **The delta
   is the primary signal** for both the action and the surfacing judgment: an
   unchanged world argues for `nothing` and silence; what changed is what may be
   worth acting on or saying.

**DECIDE — exactly one action:**

| action | when | through |
|---|---|---|
| **dispatch** | an open/backlog task fits an idle worker; routed by judgment, rationale recorded | `dispatch-task.sh` |
| **propose** | no well-defined work exists; generate task(s) from mission + knowledge + recent work into the intake store (§8); **must carry repo · project · rigor tier** | `emit-batch` (work_item) + `task_proposed` |
| **ask** | the **surfacing judgment** (below) concludes the operator should hear something — a fork only they can resolve, or nothing worthwhile can be found ("ask for tasks") | one Telegram post, shaped by §9 |
| **sprint** | **only if** ≥ `SPRINT_MIN_ISSUES` (default 5) mission-aligned issues are open **and** ≥ 1 worker is idle **and** no sprint ran within `SPRINT_MIN_GAP_S` (default 24 h) | `/autonomous-sprint` (inherits §9) |
| **nothing** | all work in flight, nothing worthwhile — **recorded**, so "checked and chose nothing" is a fact, not silence | — |

**RECORD before ACT** (§7): the decision exists even if the action fails (the
crash-correctness rule the plane's write spine already follows).

**The surfacing judgment — the Instinct gate.** A check-in is the manager's own
re-engagement cycle; the operator never sees it. Whether *anything* reaches the
operator is a separate judgment the manager makes on every check-in, and its
default answer is **no**. Most check-ins end in `dispatch`, `propose` or `nothing`
with no message at all. The judgment weighs, at minimum:

- **Does this genuinely need a human?** — a fork the manager cannot resolve (a
  `requires-approval` boundary in `PROJECT_MISSION.md`, a rigor tier that mandates
  sign-off, conflicting priorities, a proposal on a review/human-tier project).
- **Would the operator want to know?** — a deliverable ready (a PR, a finding), a
  blocker that stalls the fleet, a failure with cost.
- **What has changed since the operator was last told?** — the delta against the
  previous check-in's snapshot (READ step 5), and against the last post's `held`
  items; only new information, never a restatement.
- **Has enough accumulated to be worth one message?** — several small things
  coalesce into one post; one small thing waits, unless urgent.
- **The operator's availability and responsiveness** — read from the plane: time
  since their last message, whether the last `ask` was answered. Two open,
  unanswered asks means the fleet proceeds on its best tier-gated judgment or
  waits quietly — it does not pile on a third.
- **What Claudron knows about how the operator wants to be engaged** — captured
  preferences ("batch these", "never during X") are inputs, not decoration.
- **An urgency floor** — a `blocked` that stalls everything, or a failure with real
  cost, breaks through regardless.

The judgment is **recorded** (§7, `raise`): whether it decided to surface, why, and
what it *held* for later — so "why did it / didn't it tell me" is a row, and the
instinct is tuned on data (ask-rate, held items, whether asks were answered) rather
than on a cadence. **Nothing about the poll interval or the check-in gap touches the
operator; they bound cost, not attention.**

**Failure posture.** Any READ that fails lands in `inputs_seen.unavailable`, and with
degraded inputs the allowed actions narrow to `ask | nothing` — **never propose from
partial information** (the junk guard). Runaway guards: at most `CHECKIN_MAX_PROPOSALS`
(default 3) proposals per check-in; `ask` respects the protocol's budget (§9).

## 7. The decision record — `checkin_decision`

One `system` event per check-in, **registry-governed under F19 — no migration**
(`plane/registries.py`; add `SYSTEM_EVENT_SEVERITY["checkin_decision"] = "notice"`).
Emitted by a small door the skill invokes, `lib/checkin-record.sh`, which calls
lib-common's existing `emit_fleet_event checkin_decision manager-checkin '<json>'
"$BOT_DIR" "$BOT"` (builds the actor-anchored system envelope; dormant on
`PLANE_EMIT_ENABLED` like every door). Subject = the manager.

`detail` (schema 1):

```json
{ "schema": 1, "checkin_id": "ck_<32hex>", "prev_checkin_id": "ck_<32hex> | null",
  "inputs_seen": { "open_tasks": 0, "stalls": 0, "unacked": 0,
                   "issues_considered": 0, "knowledge_hits": 0,
                   "unavailable": ["gh"] },
  "delta": { "tasks_opened": 0, "tasks_completed": 0, "stalls_appeared": 0,
             "stalls_cleared": 0, "issues_new": 0, "messages_new": 0,
             "held_pending": 0 },
  "action": "dispatch | propose | ask | sprint | nothing",
  "rationale": "<= 600 chars, the manager's words",
  "raise": { "decided": false, "reason": "<why it surfaced, or why not>",
             "held": ["<items deferred to a later post>"] },
  "targets": { "assignment_ids": [], "work_item_ids": [], "msg_id": null } }
```

Links: `dispatch` → the assignment it created; `propose` → the work_items; `ask` → the
communication `msg_id`. This is the `decision` seam the v2 schema reserved for Phase 5
(`observable-plane-design-v2.md` §4, §7), used for the first time. The trigger's own
events — `checkin_triggered`, `checkin_skipped_busy`, `checkin_skipped_ratelimit`,
`checkin_skipped_down`, `checkin_skipped_unreachable` — make "did it fire, and why
not" observable.

## 8. The intake store — proposed work, no migration

A proposed task is a `work_item` **with no assignment yet** plus a `task_proposed`
system event (links `work_item_id`, `checkin_id`, proposer). The task vocabulary is
CHECK-constrained (`contracts.py`), so a new *task* verb would need a migration; a
*system* event does not (F19). Rejection is a `task_rejected` system event with a
reason. **The queue is a projection**: work_items carrying `task_proposed` with
neither an assignment nor a `task_rejected`.

**The well-defined bar.** A proposal must carry `title`, `repo` (`owner/name`),
`project_key`, and the project's rigor tier resolved from `projects.yaml`; the skill
refuses to propose otherwise. This is also the fix for today's 0/374.

**Approve** = dispatch it. `dispatch-task.sh` gains `--work-item <id>` so an approved
proposal's work_item is *reused* rather than a second one minted (otherwise the
proposal and the dispatch would be two rows for one piece of work). **Veto** = the
operator replies; the manager records `task_rejected`. In v1 the manager may
self-approve a proposal (dispatch it) with the operator's standing veto; a formal
approval door is C.

## 9. The check-in protocol — `library/protocols/checkin.md`

No Jinja branching exists inside library bodies (`defaults.py:190-193`); the precedent
for two audiences in one file is `dispatch.md` (`## The task loop` for managers,
`## Manager: active-plan monitoring`). So: **one file, two sections.**

**`## Manager`** — **whether to post at all is the surfacing judgment (§6): silence is
the default, and a post is the exception the judgment must justify and record.** A
check-in that dispatches, proposes or chooses `nothing` posts **nothing** — it is in
the plane. When the judgment does say post, the shape is fixed: **one status line,
one ask with named options, one pointer** (plane URL or PR). At most one post per
check-in; several qualifying items coalesce into one message; held items wait for
the next justified post. Never restate the rigor in the message; point to it.

**`## Worker`** — one thin line on start / done / blocked. No milestone cadence. Detail
goes through `report-back.sh` to the manager and the plane, not to Telegram.

**Supersedes** (edits, plus an `EXCLUSIVE_GROUPS` entry where a clean swap applies):
`worker-lifecycle.md` milestone-every-2–3-min → removed; `proactivity-discipline.md`
*"Idle silence is a bug"* → *"idle silence is recorded, not posted"*;
`autonomous-sprint` *"emit everything to Telegram"* → one plan post + one summary,
per-merge posts to the plane; `continuous-autonomous-mode.md` 10–15-min beacons →
removed (the check-in is the beat). This protocol is the enforcement of the existing
principle `simple-outside-rigorous-inside` — plain terms outside, rigor intact inside.

## 10. Equipment linking — `requires:` frontmatter

A library item may declare what it needs to ship with:

```yaml
---
title: Check-in
description: The idle-manager check-in and the thin human edge
requires:
  skills: [checkin]
---
```

**Compositor.** After a bot's protocols are resolved (declared + gated defaults +
role overlay, `composer.py:1776-1791`), collect `requires.skills` across them and
union into the bot's skill set before `link_skills` (`composer.py:1380`). v1 scope is
protocols → skills; the schema is generic (`requires.<entity_type>: [names]`) so
protocols → guardrails or skills → mcp can follow without a format change.
**Validator:** an unresolvable requirement (a skill absent from the library) is an
error; `list-library` shows requirements. **Opt-out semantics:** opting a bot out of a
protocol drops that protocol's requirements unless the skill is declared directly.

**Why this matters here.** The protocol role-default is *already wired*
(`defaults.resolve("protocols", roles)` at `composer.py:1779`; no entry uses it yet),
while skills are `_UNARGUED` and `link_skills` iterates `bot.skills` only — a skill in
the registry does not symlink (measured, `naked-bot-observation-gate.md:234-251`).
With linking, `REGISTRY["protocols"].roles = {"manager": ("checkin",)}` brings the
skill — **the manager default is one line**, and the four skill-default additions
(role-scoped `link_skills`, a `SystemDefaultsConfig.skills` opt-out key, the
INSTRUCT-evidence test line, a manager arm in `naked-bot-observe.py`) are not needed.

**Other QoL noticed, to fold in as we go:** `SystemDefaultsConfig` silently drops
unknown opt-out keys (gate doc `:151-181`) — surface a validator warning;
`sprint-trigger.sh`'s stale default (§5); `dispatch-task.sh` never fills
`repo`/`project_key` (§8 makes the check-in fill them, and the dispatch door should
accept them).

## 11. Instrumentation and the read door

`claudlobby checkins [--fleet F] [--bot B] [--since 7d] [--json]` — registered beside
`workstreams` (`commands/_parsers.py:171-180`) over `collect_plane_events`
(`commands/events.py:57`). Lists each check-in: when, `inputs_seen`, action,
rationale, targets, and **outcome so far** (dispatched task's status; proposal
open/dispatched/rejected; whether an `ask` was answered). `--proposals` lists the
intake queue (§8). `--summary` gives the action distribution, the ask-rate (the
fatigue number), proposal acceptance, mean gap between check-ins, and skip reasons.
Refuses an unreachable plane at rc 3 (`source_state.py`), never a clean empty.

This is the operator's "log how it works" instrument: every check-in is a row.
The same query feeds a "decisions" panel on the operator plane and the analytics page
(#1541). **The outcome measure for the whole feature is fleet-active %** (#1541)
before and after — the same discipline as the routing spike: numbers, not vibes.

## 12. Rollout — five chunks, the ladder the repo mandates

1. **The contract** — `/checkin` skill, `checkin.md` protocol (+ the supersede edits),
   `checkin-record.sh`, the severity entry, and `requires:` linking (compositor +
   validator + `list-library`). For the canary the protocol is declared per manager
   in `fleet.yaml`; dormant on `PLANE_EMIT_ENABLED`.
2. **The trigger** — `manager-checkin.sh`, the fleet job, the `Switch` row, and the
   **mandatory empirical gate**: extend `lib/validate-bot-change.sh` (it already
   drives `task recheck` against a throwaway bot, `:619-655` — the exact template):
   throwaway manager → goes idle → `/checkin` injected → `checkin_decision` lands →
   the Telegram shape holds. Cite the observation in the PR body.
3. **The read door** — `claudlobby checkins` + the panel seam.
4. **Canary** — arm one fleet (`defaults.jobs.manager-checkin: {enroll: true}` +
   `generate` + `setup-fleet`); *"arming one fleet IS the canary"* (`switches.py:15-20`).
   Pick a fleet whose manager is not running the rollout (the canary-rollout
   protocol's "never the manager" rule inverts here). Burn in ≥ 3 days; judge on
   `checkins --summary` + fleet-active %.
5. **Default** — `REGISTRY["protocols"].roles["manager"] = ("checkin",)`; the
   INSTRUCT bar argued in the PR; run `lib/naked-bot-observe.py --baseline`, name the
   delta (a new CLAUDE.md section *and* a new symlink — both surfaces), replace the
   *current* baseline in place, never the frozen anchor. The **job's** polarity stays
   `OPT_IN` (it spends money) unless burn-in argues otherwise — a burn-in decision,
   not a design one.

**Sequencing that matters for the canary:** a skill symlink is live the instant it
lands (no restart, no canary window — `fleet-update-lifecycle.md:32,45`); the protocol
lands at the next session start; the job at `setup-fleet`. Arm the job last.

Each chunk goes through the standing gauntlet: review lenses, committed-code mutants,
the two-leg full-suite gate (0 introduced by names *and* counts), CI, admin-merge,
deploy, live verification, record.

## 13. Testing

**Unit.** Trigger: manager filter, session gate, busy gate, rate-limit read, unreachable
→ no fire, `switch_is_on` gate. `checkin-record.sh`: payload → contract, schema 1.
Intake projection. `requires:` resolution, validator error on a missing skill,
opt-out drops requirements, `list-library` shows them. Protocol composition: both
sections present, the exclusive swap. Read door: rc 3 on unreachable, summary math,
outcome join. Severity registry entry. Naked-bot delta named.

**Empirical (mandatory — this changes bot behaviour).** The `validate-bot-change.sh`
extension above, plus a real-boot sample that `/checkin` injects cleanly into an
idle manager pane. Fixtures for any externally produced shape (the `brief --json`
envelope, `gh` output) are grounded in live captures, identifiers faked.

**Canary data.** Action distribution, ask-rate, proposal acceptance, skip reasons,
fleet-active delta.

## 14. Failure posture

- Trigger: busy / down / missing pane → skip and record; plane unreachable → **do not
  fire** (fail closed for a spending action); every path exits 0; never a state file.
- Skill: degraded inputs → `ask | nothing` only; record before act; a failed action
  leaves the decision row and a `failed` outcome, never a lost decision.
- Emit: non-blocking (`|| log`), the plane's own spool covers the daemon being down.
- Runaway: proposal cap per check-in; the min-gap; one post per check-in.
- Disarm: one `enroll` flip; the doctor shows the switch state; nothing else to undo.

## 15. Path to C (documented, not built)

- **Payoff memory** — task outcomes (completed/failed/abandoned, PR merged, operator
  reaction) scored and captured to Claudron as "what work paid off" notes; the
  check-in's knowledge read then sees them. The outcome field Claudron lacks today.
- **A formal approval door** — proposals approved/rejected through a structured reply
  or the plane's reply-from-page (Phase 6), replacing free-text veto.
- **Richer generators** — the product-vision pass, code-review findings, brainstorm
  outputs feeding the intake store, all subject to the well-defined bar.
- **Right-time scheduling** — quiet hours, batching windows, "wait for the complete
  picture" before a post.
- **Cross-fleet coordination** — managers negotiating loaned workers (#1132).

## 16. Open questions for the operator

1. Poll and gap — **cost and responsiveness only; they never touch the operator.**
   15-min poll, 60-min gap, with backoff-on-`nothing` as cost hygiene?
2. Sprint conditions — ≥ 5 mission-aligned issues, ≥ 1 idle worker, 24-h gap?
3. Proposals — cap of 3 per check-in; manager self-approves with your standing veto in
   v1, or every proposal waits for you?
4. Worker thin updates — to Telegram, or to the plane only?
5. **The surfacing judgment (§6)** — anything missing from its inputs, and is the
   urgency floor (`blocked` that stalls the fleet, a failure with real cost) drawn in
   the right place?
6. Which fleet is the canary?
