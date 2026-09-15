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
  effort 20 / deps 15 (`library/skills/autonomous-sprint/SKILL.md:76-81`);
- a generator for an empty backlog — *"If fewer than 5 mission-aligned open issues
  exist: run a product-vision pass to generate new issues… then re-evaluate"*
  (`SKILL.md:59-68`).

And nothing starts it. `lib/sprint-trigger.sh` is hand-wired cron only, never
composed, and defaults `MANAGER_TMUX` to a stale name (`:15`). `autonomous_runner.cadence`
renders into CLAUDE.md (`templates/claude.md.j2:87`) but **`lib/runner-tick.sh` does
not exist** — it is unfireable. Every wired trigger (keepalive, fleet-pulse) is
health-only. Bots boot into `STARTUP_PROMPT` *"Idle and await Telegram messages"*
(`composer.py:1261`). The prose triggers ("fleet idle + backlog non-empty → auto-fire
a sprint", `library/expertise/orchestration.md:59`) need an already-running LLM turn
and so can never fire from idle. The goal-aware plan's own re-audit says it plainly:
*"P6 ignition… Not shipped… neither sprint-trigger nor runner-tick is a composed
job"* (`documentation/plans/2026-07-06-goal-aware-fleet-portfolio.md:25,51`).

**Two constraints the design must honour:**

- *Fatigue is already at the ceiling.* Protocols say *"Idle silence is a bug"*
  (`proactivity-discipline.md:7`); workers post a Telegram milestone every 2–3 min
  of work (`worker-lifecycle.md:119,197`); sprints *"emit everything to Telegram"*;
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
   never copies them (consume by contract). **Every occurrence — a check-in, a
   decision, a proposal, a sprint, a Telegram line — is recorded and recreatable
   from the plane alone.** Carriers deliver; the plane remembers.
4. **Team loud, human edge thin.** Bot↔bot communication stays constant — that is
   the work happening. The **manager** is the channel to the operator (Telegram),
   in clean updates and requests for direction. Workers may post to Telegram,
   *really* thin.
5. **Ships as a manager default** (the primary carrier), after the rollout ladder.
6. **Equipment linking.** A library item may `requires:` another; the compositor
   brings it. Improve the system as we go.
7. **A project's ideation autonomy is its own construct** — `planning.initiative`
   (`autonomous | propose | none`, default `none`), beside and independent of closure
   rigor `validation.tier`. Review rules and planning rules never blend. `initiative`
   gates **origination only**: a project's open work is managed by the same instinct
   regardless.
8. **The check-in is a portfolio decision.** With 5–10 projects carrying different
   grants, the manager allocates idle capacity *across* projects — project × action —
   and the record carries which project and why.
9. **Focus memory, not static priority.** Where the operator's attention is —
   declared ("focus on A, B, C this week") and empirical (derived from the plane) —
   is a first-class, time-scoped, *soft* input. No `priority:` field: one `high`
   would starve every other project.
10. **No gravedigging.** Dormancy is a negative signal for new effort; a dormant
    project is revived only by explicit direction (an issue, an ask, a focus
    declaration) — the operator can always steer back to dormant work, the manager
    never digs it up on its own.
11. **The sprint is the legacy skill, scalpeled** — its scoring kept as the batch's
    ordering; its trigger, GitHub-only input, generator and emit-everything cut. The
    check-in is its only caller, on conditions, with no time gap: capacity, focus
    and initiative are the throttles (§6b).
12. **Worker updates go to Telegram — and should — where the worker is configured
    for it.** One thin line on start / done / blocked. The plane records every line
    regardless of carrier, so nothing is lost to a channel and volume is always
    measurable.
13. **The canary is the engineering fleet — the one that develops the framework
    repos — and the check-in belongs to leaf managers.** Ruled 2026-09-14 over the
    bounded alternative (the business-data fleet): a richer backlog makes each
    burn-in day more informative, and the loop into the framework repos is contained
    by the burn-in's initiative grants (§12.4), not by avoiding the fleet. A manager
    whose every in-fleet report is itself a manager (a coordinator) is not equipped by
    default — its idle question is a portfolio of portfolios, which is Phase C (§15).

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
composed fleet job (`claudlobby/system.yaml:483-486`, `interval`) whose script groups by
`assigned_by` and sends per manager via `dispatch.sh` (`commands/task.py:230,790-826`),
**rate-limiting by a plane read** — `source_ref=task-recheck:<asg>` stamped and read
back (`:458-499`), *"no timer state file to lose or to lie."* Reuse that shape.

`lib/manager-checkin.sh <fleet>`, run by the fleet job `<prefix>.manager-checkin`
(`interval: 900`, **`enroll: false`**), per bot dir:

1. **Equipped and a manager** — `bot_is_manager` (lib-common; reads the composed
   `MANAGER_TMUX == BOT_ID`) *and* the composed `checkin` skill symlink resolves
   under the bot's `.claude/skills/` — else skip silently. The symlink is what
   `requires:` composes (§10), so this one test is how a worker, a coordinator and
   an opted-out manager are all excluded, and how an operator un-equips: drop the
   protocol, regenerate, the injection stops. `bot_is_manager` alone is not enough
   — it is true for a coordinator too (`composer.py:1238` composes the self-pointer
   for every member of `manager_bots()`); the leaf-manager rule lives at compose
   time (§10), and the trigger reads its result.
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
`Switch(key="manager-checkin", scope=FLEET_JOB, polarity=OPT_IN,
carrier=ENROLL_FLEET, job="manager-checkin", plane=True, what="every 15 min,
inject /checkin into an idle, equipped leaf manager — spends money, a manager
turn per idle interval")` (the field names are `switches.py:224-237`'s). The composer stamps
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
3. `PROJECT_MISSION.md` + per-project `PROJECT_TIER_*` / `PROJECT_REPOS_*` /
   `PROJECT_INITIATIVE_*` from `bot.conf` (goal, rigor, and ideation grant).
4. `gh issue list --state open` over the fleet's repos, mission-aligned filter (the
   external backlog), grouped by project.
5. **Focus** — the current declared focus (`focus_declared`, unexpired) and the
   empirical focus derivation (§8c): where the operator's attention has been.
6. **The delta** — now vs. the previous check-in, per project: tasks opened /
   completed / stalled / cleared, issues appeared, messages arrived, held items still
   pending. **The delta is the primary signal** for both the action and the surfacing
   judgment: an unchanged world argues for `nothing` and silence; what changed is
   what may be worth acting on or saying.

**Everything above is read per project.** The check-in is a portfolio decision: it
allocates idle capacity across the fleet's projects, each carrying its own
`initiative` grant and closure `tier`.

**DECIDE — one project, then exactly one action:**

| action | when | through |
|---|---|---|
| **dispatch** | an open/backlog task fits an idle worker; routed by judgment, rationale recorded | `dispatch-task.sh` |
| **propose** | no well-defined work exists on a project whose `initiative` is `autonomous` or `propose`; generate task(s) from mission + knowledge + recent work into the intake store (§8); **must carry repo · project · rigor tier** | `emit-batch` (work_item) + `task_proposed` |
| **ask** | the **surfacing judgment** (below) concludes the operator should hear something — a fork only they can resolve, or nothing worthwhile can be found ("ask for tasks") | one Telegram post, shaped by §9 |
| **sprint** | a single project has **≥ 5 well-defined, unstarted work items** (proposals in the intake store *or* mission-aligned issues triaged in) **and** grants `initiative: autonomous` **and** is in focus **and** ≥ 2 workers are idle (batch ≤ idle workers) **and** has no unresolved sprint work in flight. No time gap — capacity, focus and initiative are the throttles. | the **scalpeled** `/autonomous-sprint` (§6b), every dispatch tagged `sprint_id` |
| **nothing** | all work in flight, nothing worthwhile — **recorded**, so "checked and chose nothing" is a fact, not silence | — |

**The allocation rule — how the project is chosen.** Attention is a weighting, never
a ranking:

- **Floor:** every project with open work or a blocker is checked on every check-in,
  regardless of its `initiative` or focus — open work is managed everywhere.
- **Tilt:** *new* effort (proposals, picking backlog issues) goes preferentially and
  proportionally to projects in declared focus and with recent empirical attention —
  never exclusively; a single focused project cannot starve the rest.
- **Grant:** proposals only where `initiative` is `autonomous` or `propose`; a `none`
  project originates nothing.
- **Never stalest-first:** no attention + no focus + no open work = left alone. A
  dormant project is revived only by explicit direction. (The rolling audit's
  "stalest repo" heuristic is the anti-pattern this rule exists to avoid.)
- Mission alignment and backlog depth weigh in; the `rationale` records the weighing.

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

## 6b. The sprint, scalpeled

The legacy `/autonomous-sprint` skill is the right batch-execution *engine* and the
wrong autonomous *agent*. It is kept and cut precisely — never bridged to as it
stands:

| keep | cut |
|---|---|
| the scoring (mission 40 / impact 25 / effort 20 / deps 15) — now used to *order items within a batch* | its standalone trigger (`sprint-trigger.sh`, hand-cron, stale default) — **the check-in is its only caller** |
| dispatch through the existing task doors | its GitHub-only input — the backlog is **the plane**: the project's unstarted `work_item`s (proposals) plus mission-aligned issues triaged in at sprint time (each becomes a `work_item` carrying the issue ref on dispatch — the existing `--ref` pattern) |
| | its empty-backlog generator (the `product-vision` pass) — **generation is the check-in's `propose`**, capped and initiative-gated; the sprint never invents work |
| | "emit everything to Telegram" — it inherits the check-in protocol (§9): one plan post, one summary |

The batch is one fact: every dispatch in a sprint carries a shared `sprint_id` in
`detail`, so "what did that sprint do and how did it turn out" is one plane query —
and "this project still has unresolved sprint work" (the no-pile-on condition) is a
read, not a timer. The loop this closes is the point of the whole design:
**ideate → accumulate → batch.** Proposals accumulate across check-ins; once enough
sit on a focused, `autonomous` project, one check-in executes them together instead
of dripping them out one dispatch at a time.

## 7. The decision record — `checkin_decision`

One `system` event per check-in, **registry-governed under F19 — no migration**
(`plane/registries.py`; add `SYSTEM_EVENT_SEVERITY["checkin_decision"] = "notice"`).
Emitted by a small door the skill invokes, `lib/checkin-record.sh`, which validates
the record against `lib/checkin-contract.py` (schema 1, stdlib) and builds the
actor-anchored system envelope the way `report-back.sh` does — `subject_kind:
actor`, `subject: bot:<fleet>/<manager>`, the decision as `data`, and
`source_ref: checkin:<checkin_id>` (the task-recheck stamp idiom: the ref is the
address a reader joins on) — through `plane_emit_events`. The plane is always on
since the F18 closure; `PLANE_EMIT_DISABLED=1` (the harness exemption) is the one
thing that silences it, and the door says so at rc 1 — for this door the record
IS the action. Subject = the manager.

`detail` (schema 1 — what one hand-equipped manager can produce in chunk 1; later
chunks ADD fields and enum values as schema 2, never rewrite):

```json
{ "schema": 1, "checkin_id": "ck_<32hex>", "prev_checkin_id": "ck_<32hex> | null",
  "inputs_seen": { "open_tasks": 0, "stalls": 0, "unacked": 0,
                   "issues_considered": 0, "knowledge_hits": 0,
                   "unavailable": ["gh"] },
  "delta": { "tasks_opened": 0, "tasks_completed": 0, "stalls_appeared": 0,
             "stalls_cleared": 0, "issues_new": 0, "messages_new": null,
             "held_pending": 0 },
  "action": "dispatch | ask | nothing",
  "project_key": "<slug the action was taken on, or null>",
  "rationale": "<= 600 chars, the manager's words",
  "raise": { "decided": false, "reason": "<why it surfaced, or why not — required both ways>",
             "held": ["<items deferred to a later post>"] } }
```

Every `delta` count is `int | null`: **null means "could not measure" and is never
collapsed to 0**, because an unchanged delta is the skill's primary argument for
silence. `focus_declared` / `focus_empirical_top` join `inputs_seen` with chunk 1b;
`propose` and `sprint` widen the `action` enum with their chunks.

**Links are reverse links, recorded by the door that acts** — RECORD before ACT
means the decision row exists before any outcome does, so the record never carries a
`targets` block (cycle-1 review, B11: a field that can never be populated makes the
record untrustworthy). `dispatch` → `dispatch-task.sh --checkin <ck_id>` appends a
`checkin_dispatch` system event `{checkin_id, assignment_id, work_item_id, task_id}`
to the SAME batch as the assignment (the `--supersedes` precedent; the `Assignment`
payload is strict and cannot carry the id). `propose` → `checkin-propose.sh
--checkin` stamps `checkin_id` into `task_proposed` (chunk 1d). `ask` → the
manager's Telegram reply, recorded by the outbound hook as a communication from the
manager's alias; the outcome join (chunk 3) correlates asks to decisions by alias +
time window, and a `checkin-ask.sh` door is added only if that join proves
ambiguous. This is the `decision` seam the v2 schema reserved for Phase 5
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

**Approval is gated by the project's `planning.initiative` (§8b), never by its
closure tier.** `autonomous` → the manager dispatches its own proposal; the operator
holds a standing veto. `propose` → the proposal waits in the store and surfaces
through the judgment as one `ask`; the operator approves before anything starts.
`none` → no proposal is made. **Approve** = dispatch it: `dispatch-task.sh` gains
`--work-item <id>` so an approved proposal's work_item is *reused* rather than a
second one minted (otherwise the proposal and the dispatch would be two rows for one
piece of work). **Veto** = `task_rejected`, which *withdraws* the work through the
existing withdraw door — a real action, so the standing veto is honest. A formal
approval door is C.

## 8b. `planning.initiative` — a project's ideation autonomy

Closure rigor and ideation autonomy are different axes and must not blend. On
`projects.yaml`, beside the existing closure block and independent of it:

```yaml
projects:
  my-repo:
    title: My Repo
    repos: [acme/my-repo]
    planning:                  # how work is ORIGINATED
      initiative: autonomous   # autonomous | propose | none   (default: none)
    validation:                # how work is CLOSED
      tier: review
```

| phase block | its dial | values |
|---|---|---|
| `planning:` — how work is originated | `initiative:` | `autonomous · propose · none` |
| `validation:` — how work is closed | `tier:` | `auto · review · preview · human` |

`review` is one closure *tier*, not the block's name — closure is also `auto` (CI
green), `preview` (link posted + ack) and `human`; `validation` is the umbrella. The
blocks mirror each other phase-to-phase, the dials dial-to-dial.

- **Default `none`** — a project grants initiative explicitly; a root pull never
  makes a manager start originating work nobody opted into (no silent switches).
  Deliberately asymmetric with `validation.tier`'s default of `review`.
- **Origination only.** `initiative` never freezes a project: its open work is
  dispatched, chased and re-routed by the same instinct regardless.
- **Composition** mirrors the tier: `planning.initiative` flattens to
  `PROJECT_INITIATIVE_<SLUG>` in every `bot.conf` beside `PROJECT_TIER_<SLUG>`;
  `known_values.py` carries the value set; the validator rejects anything else;
  `projects-yaml-schema.md` documents the block. Room to grow without a rename — a
  per-project `proposal_cap` or `sources:` belong here if evidence ever asks.
- **No `priority:` field.** A static `high` would monopolize the manager and starve
  every other project. Prioritization is focus (§8c): soft and time-scoped.

## 8c. Fleet focus — where the operator's attention is

Prioritizing across a portfolio needs to know what the operator cares about *now*,
without a ranking that can block everything else. Focus has two sources and one
concept:

- **Declared.** The operator says "focus on A, B, C this week" — to the manager on
  Telegram, or `claudlobby focus set a b c --until 7d`. Either lands the same
  `focus_declared` system event (F19, no migration) in the plane:
  `{projects: [slugs], window: {from, until}, source: operator | manager, note}`.
  A time-scoped operational *commitment*, so it is plane, not vault (Claudron may
  later hold the durable pattern — "the operator tends to focus on X in Q3"). The
  latest unexpired declaration wins; the default window is seven days. The manager's
  skill recognizes a focus statement and records it through `lib/focus-declare.sh`.
- **Empirical.** Derived at read time — a Lane-C query, no table — as
  recency-weighted attention per project: the operator's messages in threads tied to
  a project's work items, dispatches to it, reports and reviews on it. Presence's
  sibling: a derivation, never stored.
- **Read door.** `claudlobby focus` shows both (declared with its window; empirical
  top-N with the evidence counts). The check-in reads both in READ step 5 and records
  what it saw (§7).

Focus is a **weighting, never a ranking**: it tilts where new effort goes and cannot
zero out a project with open work (the allocation rule, §6). The operator can always
steer back to dormant work by declaring it; the manager never digs it up on its own.

## 9. The check-in protocol — `library/protocols/checkin.md`

No Jinja branching exists inside library bodies (`defaults.py:190-193`); the precedent
for two audiences in one file is `dispatch.md` (`## The task loop` for managers,
`## Manager: active-plan monitoring`). So: **one file, two sections.**

**`## Manager`** — **whether to post at all is the surfacing judgment (§6): silence is
the default, and a post is the exception the judgment must justify and record.** A
check-in that dispatches, proposes or chooses `nothing` posts **nothing** — it is in
the plane. When the judgment does say post, the shape is fixed: **one status line,
one ask with named options, one pointer** (plane URL or PR). At most one post per
check-in; several qualifying items coalesce into one message **across all projects
— one post covers the portfolio, never one per project**; held items wait for the
next justified post. Never restate the rigor in the message; point to it.

**`## Worker`** — one thin line on start / done / blocked, **to Telegram where the
worker is configured for it** (and it should be, where it is); plane-only where it
is not. No milestone cadence. Detail goes through `report-back.sh` to the manager
and the plane. **Every line is a recorded communication regardless of carrier** —
the outbound Telegram hook already lands each reply as a `communication` +
`carrier_accepted` transmission — so the plane recreates every occurrence, and
"worker Telegram lines per day" is one query if volume ever needs tuning.

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
role overlay, `composer.py:1737-1766`), collect `requires.skills` across them and
**union into the bot's skill set — the one list that feeds `compose_settings_local`
(`composer.py:2780`), `link_skills` (`:2788`), the validator's grant loop
(`validator.py:764`) and freshbox (`freshbox.py:74`)**. A required skill that is only
symlinked is linked but never GRANTED (`_resolve_skill_permissions` / `_resolve_skill_grants`
iterate `bot.skills`, `composer.py:2233-2261`, and settings compose BEFORE the
symlink step) — cycle-1 review B8. The unresolvable-requirement error is reported
once per protocol, library-wide, never once per equipping bot (`validator.py:383-396`). v1 scope is
protocols → skills; the schema is generic (`requires.<entity_type>: [names]`) so
protocols → guardrails or skills → mcp can follow without a format change.
**Validator:** an unresolvable requirement (a skill absent from the library) is an
error; `list-library` shows requirements. **Opt-out semantics:** opting a bot out of a
protocol drops that protocol's requirements unless the skill is declared directly.

**Why this matters here.** The protocol role-default is *already wired*
(`defaults.resolve("protocols", roles)` at `composer.py:1763`; no entry uses it yet),
while skills are `_UNARGUED` and `link_skills` iterates `bot.skills` only — a skill in
the registry does not symlink (measured, `naked-bot-observation-gate.md:234-251`).
With linking, `REGISTRY["protocols"].roles = {"leaf-manager": ("checkin",)}` brings
the skill — **the manager default is one line**, and three of the four skill-default
additions (role-scoped `link_skills`, a `SystemDefaultsConfig.skills` opt-out key,
the INSTRUCT-evidence test line) are not needed. The fourth — **a manager arm in
`naked-bot-observe.py`** — *is* needed, and not as skill machinery: the gate
composes no manager today (`naked-bot-observation-gate.md:294-296`), so a
role-scoped default is invisible to `--baseline` until an arm exists. It lands in
chunk 5 before the overlay is populated (§12).

**Leaf managers, not every manager.** `ROLE_MANAGER` is true for every member of
`manager_bots()` — including a coordinator whose reports are themselves managers
(`config.py:735-757` widened it for exactly that bot), and the composed
`MANAGER_TMUX` self-pointer (`composer.py:1238`) follows the same set, so
`bot_is_manager` cannot tell the two apart at runtime either. Two managers reasoning
over one portfolio is the pile-on the allocation rule forbids (§6), and a
coordinator's idle question is a different one — "are my managers progressing?" —
which is Phase C (§15). So the check-in registers under a **second detectable role,
`leaf-manager`**: a manager at least one of whose in-fleet reports (`teams.*.workers`
where it is the manager, plus `manages:`) is not itself in `manager_bots()`. **A
cross-fleet `manages:` target does NOT make a manager leaf** — `config.py:736-748`
says `manages:` exists precisely for "a top-level coordinator whose reports are
themselves managers of other fleets", so an unresolvable cross-fleet target is
evidence of a coordinator, not of a worker; and for a money-spending default the
conservative direction is not to equip. (Cycle-1 review R2 / fork F5 inverted the
first draft of this sentence.) `defaults.py:360-384` names this exact seam:
add the predicate first, then the key to `DETECTABLE_ROLES`, and the composer passes
both roles at `composer.py:1763`. Where a fleet has one team and no `manages:` chain
— the shape every manifest inspected so far has — the two roles name the same bot;
the role exists for the coordinator it must skip.

**Validator surface (no silent switches).** The `checkin` protocol declared on a bot
the trigger will never inject into (a worker, a coordinator) is a `generate` warning
that says why. The skill still links — `/checkin` runs by hand — only the injection
is withheld (§5 step 1).

**Other QoL noticed, to fold in as we go:** `SystemDefaultsConfig` silently drops
unknown opt-out keys (gate doc `:151-181`) — surface a validator warning;
`sprint-trigger.sh`'s stale default (§5); `dispatch-task.sh` never fills
`repo`/`project_key` (§8 makes the check-in fill them, and the dispatch door should
accept them).

## 11. Instrumentation and the read door

`claudlobby checkins [--fleet F] [--bot B] [--since 7d] [--json]` — registered beside
`workstreams` (`commands/_parsers.py:189-197`) over `collect_plane_events`
(`commands/events.py:57`). Lists each check-in: when, `inputs_seen`, action,
rationale, its join rows (`checkin_dispatch`, `task_proposed`), and **outcome so far** (dispatched task's status; proposal
open/dispatched/rejected; whether an `ask` was answered). `--proposals` lists the
intake queue (§8). `--summary` gives the action distribution, the ask-rate (the
fatigue number), proposal acceptance, mean gap between check-ins, and skip reasons.
Refuses an unreachable plane at rc 3 (`source_state.py`), never a clean empty.

This is the operator's "log how it works" instrument: every check-in is a row.
The same query feeds a "decisions" panel on the operator plane and the analytics page
(#1541). **The outcome measure for the whole feature is fleet-active %** (#1541)
before and after — the same discipline as the routing spike: numbers, not vibes.

## 12. Rollout — the ladder the repo mandates (re-sequenced 2026-09-15)

Cycle 1 of `/ironclad` on the first chunk-1 plan (PR #1550) returned 12 blockers,
six of them the same finding: the draft shipped an estate-wide behaviour change (the
cadence retirement) and a skill whose main actions its own rules made unreachable,
while pulling forward mechanisms (equipment linking, the leaf-manager role, the
intake store) whose only consumers are four chunks away. The ladder below is the
adversarial, YAGNI re-cut: **each piece lands in the chunk that consumes it, and
nothing composes differently on the estate until the canary has earned it.**

1. **The record and the run** (plan: `2026-09-14-manager-checkin-chunk1-contract.md`,
   cycle 2) — the schema-1 contract + `checkin-record.sh`; `dispatch-task.sh
   --project` (the well-defined bar; fixes the measured 0/374) and `--checkin` (the
   join row); the two severity lines; `claudlobby checkins` (rows, `--last`); the
   `checkin.md` protocol (additive, no `requires:`) and the `/checkin` skill with
   actions `dispatch | ask | nothing`. The canary leaf manager is equipped **by
   hand** (`protocols: [checkin]`, `skills: [checkin]`). Gate: the doors on a real
   plane through the harness, plus **one real `/checkin --dry-run` on the canary
   manager** before merge — the skill and protocol are runtime behaviour and the
   runtime gate covers them. Pre-change baselines (fleet-active %, Telegram post
   volume) recorded in the PR.
1b. **Focus** (§8c) — `focus_declared`, `lib/focus-declare.sh`, the empirical
   derivation, `claudlobby focus`; the two focus fields join `inputs_seen` as
   schema 2.
1c. **Scalpel the sprint** (§6b) — the `sprint` action joins the enum.
1d. **Intake** (§8, §8b) — **gated on canary evidence** (open question 7): the
   `planning.initiative` construct with its doctor surface (no silent switches —
   `switches.py:30-36`), `lib/checkin-propose.sh` (propose / reject, the
   well-defined bar as a refusal, the proposal cap enforced by the door — a count
   of `task_proposed` rows per `checkin_id`, never prose), `dispatch-task.sh
   --work-item` (looking the id up, as `--supersedes` does), the proposals
   projection and `checkins --proposals`, the `propose` action.
2. **The trigger** — `manager-checkin.sh`, the fleet job, the `Switch` row, and
   the `validate-bot-change.sh` extension (throwaway manager → idle → `/checkin`
   injected → `checkin_decision` lands). The trigger gates on the composed skill
   symlink, which by-hand declaration already scopes to the canary manager.
3. **The read door, whole** — `checkins --summary`, the outcome join (dispatch via
   `checkin_dispatch`; asks by alias + time window, or `checkin-ask.sh` if that
   proves ambiguous), the panel seam.
4. **Canary — the engineering fleet** (ruling 13). Arm the job on that fleet; burn
   in ≥ 3 days against the chunk-1 baselines; judge on `checkins --summary` and the
   fleet-active delta. The canary fleet declares `checkin` in its `defaults.protocols`, so every
   bot in it composes the new protocol, whose Worker section governs where it
   composes beside an older cadence rule — the thin edge on one fleet with no
   library edit (fork F3, locked 2026-09-15: "retire those and make check-in the
   beat", in chunk 5).
5. **Default** — `requires:` linking **with the grant union** (§10), the
   `leaf-manager` role (with the cross-fleet direction of §10), the naked-bot gate's
   leaf-manager arm, then the registry line; the cadence-retirement edits land
   estate-wide with a **grep-derived sweep** (`milestone|beacon|2.3 min|10.15
   min|Idle silence` over `library/` — the in-repo precedent is `a2a2210` followed
   by `8386263`'s "six more residue sites") and a test that asserts over the grep,
   not a hand list. The job's polarity stays `OPT_IN` unless the burn-in argues
   otherwise.

**Sequencing that matters for the canary:** a skill symlink is live the instant it
lands (no restart, no canary window — `fleet-update-lifecycle.md:32,45`); the protocol
lands at the next session start; the job at `setup-fleet`. Arm the job last.

Each chunk goes through the standing gauntlet: review lenses, committed-code mutants,
the two-leg full-suite gate (0 introduced by names *and* counts), CI, admin-merge,
deploy, live verification, record.

## 13. Testing

**Unit.** Trigger: the manager + equipment gate (a worker, a coordinator and an
un-equipped manager all skip; an equipped leaf manager fires), session gate, busy
gate, rate-limit read, unreachable → no fire, `switch_is_on` gate. `leaf-manager`
detection: a team manager is leaf; a `manages:`-only coordinator whose reports are
all managers is not; a cross-fleet target makes it leaf; the `generate` warning on a
`checkin` protocol the trigger will never inject into. `checkin-record.sh`: payload → contract, schema 1.
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
- **The coordinator's cycle** — a manager-of-managers checking in over its
  managers' portfolios (progress, rebalancing, loans); v1 equips leaf managers only
  (§10), so the shape is named here and built nowhere yet.
- **Cross-fleet coordination** — managers negotiating loaned workers (#1132).

## 16. Open questions for the operator

1. ~~Poll and gap~~ **Settled:** 15-min poll, 60-min gap, backoff-on-`nothing` as
   cost hygiene; none of it touches the operator.
2. ~~Sprint conditions~~ **Settled:** per project — ≥ 5 well-defined unstarted items
   (proposals or triaged issues), `initiative: autonomous`, in focus, ≥ 2 idle
   workers (batch ≤ idle), no unresolved sprint work on that project; **no time
   gap**. The sprint is the scalpeled legacy skill (§6b), routed to only by the
   check-in.
3. ~~Proposals~~ **Settled:** cap 3 per check-in across the portfolio; approval gated
   by `planning.initiative` (§8b); focus, never a static priority, steers allocation
   (§8c); no gravedigging.
4. ~~Worker thin updates~~ **Settled:** to Telegram where the worker is configured
   for it; recorded in the plane regardless of carrier; measurable.
5. ~~The surfacing judgment~~ **Settled:** inputs and urgency floor as drawn in §6;
   the delta vs. the previous check-in is the primary signal.
7. ~~Does `propose` wait for the canary~~ **Settled (2026-09-15):** yes. The canary
   fleet's repos carry thousands of open issues, so the canary measures `dispatch`;
   `propose` (the intake store, `planning.initiative`, chunk 1d) follows, and its
   real customers are the operator's thinner-backlog personal-project fleets — "some
   fleets on my personal projects would have less issues on their given domains."
   The canary's `ask`-for-tasks rows on an empty project are the evidence that
   gates 1d.
6. ~~Which fleet is the canary~~ **Settled (2026-09-14):** the engineering fleet —
   the one that develops the framework repos. The operator ruled for the richer
   backlog over the bounded blast radius; the loop into the framework is contained by
   the burn-in's initiative grants (§12.4), and the coordinator question the choice
   raised is settled by the `leaf-manager` role (§10).
