---
title: "Manager Check-in — Chunk 1: The Record and the Run — Implementation Plan"
type: plan
status: draft
owner: fleet owner
created: 2026-09-14
---

# Manager Check-in — Chunk 1: The Record and the Run — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Build to the end — the operator's ruling (2026-09-16).** "Do we believe in what we're building? Then let's get there." The five-chunk canary ladder and its evaluation apparatus are removed: no baselines, no control fleet, no pre-registered bar, no burn-in verdict, no hand-inject interval (forks F6 and F7 are superseded). The loop is built as ONE series of four PRs with mechanical gates only — this PR (the record, the doors, the skill, deployed inert), PR 2 (the trigger and the protocol), PR 2 ending in the one deploy (the canary manager equipped behind one restart, the skill's dry run first, the job armed, then a day of reading its automated check-ins — a look, not a measurement), PR 3 (the read door whole and the outcome join, pulled inert), PR 4 (the default for every leaf manager and the cadence retirement). This plan's code and gauntlet are unchanged; Task 0 keeps the worktree, the venv and the before leg, and Task 7 ends at the merge and the inert deploy. Spec §12 is re-cut to the same shape.

> **Final fold — cycle 9's review, loop closed (2026-09-15).** Cycle 9 of `/ironclad` (PR #1550, ninth review comment) returned 2 blockers, both measured: Task 2 still edited the composed `dispatch.md` protocol while the plan claimed nothing composes differently (moved to chunk 2 with the other protocol edit); and a `checkin/chunk1-record` branch left by an earlier attempt makes the plan's first command fail (`-B`). The majors are folded in a targeted way: the identifier gate now runs in the same block as each publishing command; the dry run proves it recorded nothing (`dryrun_recorded=`); the skill's last-resort row names every required key; the false "private mint" premise is replaced by the real trade (the bash emit ladder and `emit_fleet_event`'s fixed `source_ref`); the hand-inject interval is no longer called dose; the dead `LIMIT 1` is gone; one more contract rule (`checkins` unavailable ⇒ `prev_checkin_id` null). Two findings that need a decision are recorded as open forks F6 and F7 for the operator's gate, not re-paneled. **By the operator's ruling no further review cycles run**: remaining findings are addressed in a targeted fashion and decisions become forks; the standing gate is mechanical (apply-run-restore, 36 mutants, the stubbed rehearsal). The review logs cited below live in `scratch/` (local, untracked); the durable record is PR #1550's review comments. The cycle-9 body is at `c0ff8d8`.

> **Reforge cycle 9 (2026-09-15).** Cycle 8 of `/ironclad` (PR #1550, eighth review comment; log `scratch/ironclad-2026-09-15_cycle8/verified.md`) returned 3 blockers, all in Task 7's procedure: the env file carried shell variables between tool calls but not the working directory (so the two-leg gate could run the wrong checkout on both legs and `gh pr create` could publish for the wrong branch); the hand-equip was never verified before the step that depended on it; and the deploy comment pasted model-authored free text into a public PR. Four lenses converged on the spec's bar: one untreated week already sat at 1.99× the 28-day rate, the bar had no cost term, closure counted the unresolvable against, and no consequence was registered per verdict. The deliverable majors were small and substantive: `considered`/`unavailable` defaulted when omitted, `raise.decided` was unconstrained on non-ask rows, the mission was neither gated nor recorded, READ 3 sent the manager to env vars no grant lets it read, nothing pinned the RECORD template's keys to the contract. Found while verifying: the cycle-8 `&& echo "recorded $ck"` was itself an ungranted subcommand. All are folded here. **One scope change:** the protocol file moves to chunk 2 with the trigger it governs — every clause a chunk-1 run depends on is in the skill, which is read per use, so chunk 1 ships no fenced live restart and no narrator regression; T0 is the equip instant. Declined with the reason recorded: `project_key` optional on dispatch (the north star's rigor clause makes the project the unit). The cycle-8 body is at `91a23ea` and `scratch/ironclad-2026-09-15_cycle8/snapshot-cycle8/plan.md`.

> **Reforge cycle 8 (2026-09-15).** Cycle 7 of `/ironclad` (PR #1550, seventh review comment; log `scratch/ironclad-2026-09-15_cycle7/verified.md`) returned 6 blockers, every one in Task 7 text the cycle-7 fold added or in the spec's bar: the dry-run pane was pasted into a public comment; `CK_CONTROL=none` made the identifier gate grep the word "none"; an unfilled `<…>` template was posted; every block assumed shell state survives between the executor's tool calls (it does not — measured); the negative-control mutant broke the door's success path; the closure numerator counted blocked and failed dispatches. Plus eight risks and the gaps — the two-call id hand-off, the bar's one-week denominator (this fleet's last three weeks read 280, 112 and 0 worker-busy minutes), the `T0` capture guarded on a file a refusal could leave behind, the driver's missing green precondition, the `report-back.sh` alias claim. All are folded here: `$OUT/env.sh` sourced first by every block, the gate rebuilt from set values with `grep -F`, the skill's `dispatch` as ONE call (`ck=$(record …) && dispatch --checkin "$ck"`), `verdict.txt` for the beat and the clause, `T0` as the 28-day rate, `completed`-only closure and an absolute floor in the spec. The Task 7 blocks were run from pane-shaped stubs, with and without a control, before this body was written. The cycle-7 body is at `182eb98` and `scratch/ironclad-2026-09-15_cycle7/snapshot-cycle7/plan.md`.

> **Reforge cycle 7 (2026-09-15).** Cycle 6 of `/ironclad` (PR #1550, sixth review comment; log `scratch/ironclad-2026-09-15_cycle6/verified.md`, 25 entries) returned 4 blockers, all in text the cycle-6 fold added: the baseline file printed the fleet, the manager and the project keys into a body pasted to a public PR; a fold regex left a fabricated `ssh rc=0` under the fail-closed restart gate; the pre-registered bar subtracted a control fleet's raw minutes on a four-times scale; and the alias-change count was a wrong grep pattern reporting every bot (the corrected loop reads 0 of 18 on the host). Plus thirteen risks and the gaps — the protocol's rate-limit read without `--raised`, the lookup placed before the door's root default, the docs not on the branch the run merges into, the squash body without its trailer, the deploy comment with no assembler. All are folded here; the identifier-free baseline file, the PR-body gate, the moved lookup and the protocol test were run before this body was written. The cycle-6 body is at `119fb99` and `scratch/ironclad-2026-09-15_cycle6/snapshot-cycle6/plan.md`.

**Goal:** One leaf manager, equipped by hand (`skills: [checkin]` in its `fleet.yaml`; the protocol lands with the trigger in chunk 2), runs `/checkin` for real after this chunk merges and deploys: it reads the SSOT through named doors, decides `dispatch | ask | nothing`, records the decision as one plane row BEFORE acting, and any dispatch it makes is joined to that row. `claudlobby checkins --last` shows that row. That real run — not a dry run — is the deploy's positive control and is pasted on the PR, which states **which clause of this Goal the run proved** (the record alone, or the record and the join).

**Architecture:** One bash door and one CLI read door over the plane, plus a skill. The decision is one `checkin_decision` system event (no migration; one severity line). The dispatch join is a second system event, `checkin_dispatch`, that `dispatch-task.sh --checkin` appends to the dispatch batch, atomic with the assignment — the `--supersedes` precedent. Nothing composes differently on the estate: no template change, no `bot.conf` change, no protocol edit at all (the `checkin.md` file and the `dispatch.md` field-table row both land in chunk 2 behind its restart — cycle 9: a composed-protocol edit changes every declaring manager's `CLAUDE.md` at its next session start, which is a rollout, not a doc fix), no defaults, no trigger. `lib/` changes are additive and byte-identical without the flags — **except the `tg-post.sh` sender alias**, which changes on pull for every bot whose display `name` differs from its id (`config.py:520-521`; Task 0 counts them read-only before merge and the PR names the count — on this host it is **0 of 18**, so the fix is inert there today; cycle 6's "18 of 18" was a wrong grep pattern, corrected). Where a display name IS declared it is a unification with the two writers that already anchor on `BOT_ID` (`plane-telegram-out.sh:106`; `dispatch-task.sh:585` through `MANAGER="${BOT_ID:-…}"`, `:449`) — `report-back.sh:224` writes `bot:$FLEET_NAME/$BOT` from the positional the bot passes (`:31`), so on a display-named fleet the report ledger would carry the same split, chunk 3's join concern, not this chunk's — and no reader keys on the display form; after the pull only `tg-post.sh`'s pre-pull rows stay under the display alias. It ships in THIS chunk, not chunk 3, because a pre-fix `ask` row would be permanently unjoinable (F4), and the join half (`--checkin`, the lookup, `checkin_dispatch`) ships now for the same one-pull reason: `dispatch-task.sh` reaches every fleet on pull, so landing the join with the record costs one estate pull instead of two. The intended fix, disclosed, never called byte-identical. The canary manager is declared by the operator, by hand, after merge. Two choices are deliberate and cheap to misread: (1) the `checkin_id` never crosses a tool boundary: for a `dispatch` the skill issues the record and the act as **one Bash call** — `ck=$(bash …/checkin-record.sh <<'EOF' … EOF) && bash …/dispatch-task.sh --checkin "$ck" …` — so RECORD-before-ACT is mechanical (a refused or unrecorded decision, rc 2/3, skips the act by `&&`), there is no transcription and no per-session file (a file the way `plane-session-start.sh` hands `session_uid` to `report-back.sh` would join a *stale* decision silently whenever a check-in records and then does not dispatch — the wrong-attribution class `who-reviewed.py` and `dispatch-overdue.py --open-task` document). The shape exists in a shipped skill (`library/skills/fleet-digest/SKILL.md:57`, `out="$(claudlobby …)"`) and compound commands are matched per subcommand (`documentation/decisions/permissions-model.md:56`), so both halves are granted — and NO other subcommand rides the call: a builtin such as `echo` is ungranted like anything else (cycle-8). Whether the `$( )` prefix itself prompts is measured by the dry run's composed rehearsal (Task 7 step 8) and settled by the live `dispatch` (`composed_call=` in `verdict.txt`). The precedent this replaces is measured, not only cited: `--supersedes` was built the two-call way and retired ZERO rows in a week because nobody passed it (`lib/dispatch-task.sh:541`; `lib/dispatch-supersede-hint.py:4-10`, #1032) — the single call is the stronger control; the two-call form with the lookup is the fallback ONLY if the composed call prompts, and a check-in-era dispatch that omitted `--checkin` is detectable after the fact as an assignment batch with no `checkin_dispatch` row (chunk 3 counts them); the lookup at the dispatch door stays for hand callers and discloses an id the plane cannot see; (2) the write door is bash and the read door is Python because every acting door the manager drives from its Bash tool is bash (`report-back.sh`, `dispatch-task.sh`, `task-act.sh` — lib-common's mint, emit and trap) while every read a human or skill consumes is a package subcommand (`report-back`, `workstreams`, `brief`) — the plan pays the bash constraints once, where the estate already pays them. That choice has a cost the plan names once: the form-D contract call and the ERR-trap discipline (cycle-3 B2, cycle-5 B2, cycle-7 B5) are what a Python door would not have paid. A Python door was not foreclosed by the mint — `claudlobby/plane/ids.py:45` ships the shared `mint()` that bash's `plane_mint_id` anchors to (`lib-common.sh:526-530`), and `commands/task.py:52` already writes plane rows from Python (cycle-9 correction of an earlier sentence) — but by the emit path: every acting door the manager drives rides the bash shim ladder (`plane-emit.sh`: socket → cold CLI → spool) and a Python door would be the one acting door outside it. The door also bypasses `emit_fleet_event` (`lib-common.sh:1392`) on purpose: that helper hard-codes `source_ref: fleet-events:sha:<key>` and takes no `source_ref` parameter, and this record needs `checkin:<id>`. Chunk 1d's `checkin-propose.sh` chooses on that trade, not on a mint.

**Tech Stack:** Python 3.11 (stdlib + the package's existing pydantic), bash 3.2-compatible `lib/` scripts sourcing `lib-common.sh`, SQLite plane (schema 11), pytest.

**Spec:** `documentation/plans/2026-09-13-manager-checkin-design.md` — this plan implements its §12 chunk 1 as re-sequenced in cycle 3 and re-measured in cycle 4 (§7 schema rules, §11 the read door, §12 the build series). Executors read both.

## Scope

**In PR 1 (this plan):** the schema-1 decision contract; `lib/checkin-record.sh`; `dispatch-task.sh --project` and `--checkin` (with the id lookup); `lib/plane-lookup.py --checkin-id`; the `tg-post.sh` sender-alias fix; the two severity lines; `claudlobby checkins` (rows, `--last`, `--bot`, `--since`, `--raised`, `--json`); `library/skills/checkin/SKILL.md` (the protocol is chunk 2's — cycle-8: every clause a chunk-1 run depends on is in the skill, which is read per use and needs no restart); the harness block; the gauntlet; the post-merge real run on the canary manager.

**Deferred, each to the PR of the series that consumes it, or to a later feature build** (§Decision Forks; re-cut 2026-09-16):

| Deferred | To | Why not now |
|---|---|---|
| `planning.initiative`; `lib/checkin-propose.sh`; `dispatch-task.sh --work-item` (looking its id up, as `--supersedes` does); the proposals projection; the `propose` action; the proposal cap enforced by the door | **later — a feature build** (F1) | the empty-backlog branch of a fleet whose framework repos carry thousands of open issues |
| `requires:` frontmatter with the grant union; the `leaf-manager` role with the cross-fleet direction of spec §10 | **PR 4 — the default** (F2) | its only consumer is the registry line; the canary declares by hand. Whether `project_key` may be null on a plane-known dispatch for a fleet with no `projects.yaml` is chunk 5's question too (cycle-8: declined for the canary — the north star's rigor clause makes the project the unit a dispatch closes against, so a fleet that declares no tier has no well-defined bar) |
| `library/protocols/checkin.md` — the silence-default protocol with its precedence sentence, both audiences — and its two tests; the `dispatch.md` field-table row for `project:` and its recipe flag (cycle 9: composed text, so it rides chunk 2's restart); prior art chunk 2 names beside `sprint-trigger.sh`: the shipped `autonomous-sprint` skill, which occupies the same read-mission → pick → dispatch surface | **PR 2 — trigger + protocol** | the protocol governs the beat and the trigger makes it; a skill is read per use and needs no restart, so chunk 1 ships no fenced live restart and no narrator regression (cycle-8 first-principles: every clause a chunk-1 run could depend on — the post shape, the two-ask rate limit, the silence default — is restated in the skill) |
| `lib/sprint-selection-record.py` (Phase 0 of #974: the batch selection record; its record/verify half has no caller today and no CLAUDE.md row) | **later — a feature build** | it stays the sprint's instrument and the check-in record is the manager's — the two are not merged (cycle-8 precedent) |
| the cadence-retirement edits, with a **grep-derived** sweep (`grep -i -E 'milestone\|beacon\|2.3 min\|10.15 min\|idle silence\|never go silent\|never licenses silence\|cadence and frequency'` over `library/` under a UTF-8 locale — case-insensitive because the live text reads `Never go silent` (`orchestration.md:108`, `lifecycle/SKILL.md:37`) and `comms-topology.md:74` carries neither old token (cycle-6 R12: the first pattern missed three of the four files it named); the en-dash in `every 2–3 minutes` is matched by `.` only as a character, never as a byte, incl. `expertise/orchestration.md:108`, `protocols/comms-topology.md:74`, `skills/lifecycle/SKILL.md:37`, `protocols/telegram-routing.md:29`) | **PR 4 — the default** (F3) | an estate-wide change whose replacement must be trusted on one fleet first |
| an `ask` door and `targets.msg_id` | **PR 3 — read door + outcome join** (F4) | asks join by alias + time window; `tg-post.sh`'s alias is fixed here so that join can work |
| `sprint` action; `focus_*` fields | **later — feature builds** (schema 2) | not read by this chunk's skill |
| `checkins --summary`, `--limit`, SQL-bound `--since`, and `--last`'s SQL bound (READ 0 passes `--bot`, which filters in Python, so a bare `LIMIT` never applied — cycle 9 removed one; the bound lands with the `subject_alias` bind) | **PR 3** | no consumer before the outcome join (`--raised`, one filter line, lands NOW: READ 0's ask count is its consumer, and without it the count is an unbounded read of every record in the window — cycle-5 R5) |
| the `BOT_NAME` residue: `keepalive.sh:102`'s heartbeat subject and `plane_armed --require-bot` (`lib-common.sh:510-516`) key on `BOT_NAME` where every other alias uses `BOT_ID` (cycle-3 R3, gap) | **PR 2 — trigger + protocol** | the trigger runs under the timer env, and the heartbeat subject is the presence join's key — moving it needs the trigger's harness to prove the join still holds; today `start-bot.sh` exports both names, so it is latent. Chunk 2's prior art, to consume or retire by name: `lib/sprint-trigger.sh` (the shipped schedule-driven idle-manager nudge, with its busy-skip gate) and `lib/briefing-trigger.sh` (a composed per-(bot,slot) timer firing a slash command) |
| a check-in surface on the operator plane (no `view.py` route serves `system` events today; `/api/search` is comms-FTS only) | **later — a feature build** | the outcome join gives it something to render; until then the CLI is the inspect door |

## Decision Forks

- **F1 — Defer `propose` and the intake store past the canary.** *Context:* the spec's `propose` action needs an intake store, a proposal cap and a projection; its consumer is the thin-backlog fleet. *Options:* defer to 1d gated on canary evidence / build now. *Lean:* defer. *Ratifier:* operator. **Status: locked** (2026-09-15) — evidence: operator: "okay sure. some fleets on my personal projects would have less issues on their given domains. but yes these work systems we're testing on have thousands." The canary measures `dispatch` on a deep backlog; 1d ships `propose` for the thin-backlog fleets, gated on the canary's `ask`-for-tasks rows on an empty project — which schema 1's `issues_seen` (the raw count) makes distinguishable from a broken filter (cycle-3 R4) — a count the model types from `gh`'s output, disclosed as self-reported; chunk 1d's door produces it.
- **F2 — Defer `requires:` linking and the `leaf-manager` role to chunk 5.** *Context:* both exist to make the default registry line correct; the canary is equipped by hand. *Options:* defer / build now. *Lean:* defer. *Ratifier:* operator. **Status: locked** — evidence: operator instruction ("NOT overbuilding, YAGNI"); no consumer before chunk 5; cycle-1 B8 (linked but never granted) and R2 (the cross-fleet direction) are written into spec §10 so chunk 5 builds them right.
- **F3 — Where the cadence-retirement edits land.** *Context:* `orchestration.md:108`, `comms-topology.md:74`, `inbound-acknowledgment.md:42` and their siblings mandate a beat the check-in replaces; retiring them is estate-wide. *Options:* (a) PR 4 (the default) estate-wide with the default; during chunk 4 the canary fleet declares `checkin` in `defaults.protocols`, whose preamble states that it governs where it composes beside an older cadence rule; (b) chunk 2; (c) chunk 4. *Lean:* (a). *Ratifier:* operator. **Status: locked** (2026-09-15) — evidence: operator: "Yes I agree with this approach. Lets retire those and make check in the beat."
- **F4 — `ask` has no door in chunk 1.** *Context:* an `ask` is a Telegram post the outbound hook (or `tg-post.sh`) already records as a communication from the manager's alias. *Options:* (a) asks join to decisions by alias + time window in chunk 3; a door only if that proves ambiguous; (b) build now. *Lean:* (a). *Ratifier:* operator. **Status: locked** — evidence: YAGNI instruction; `targets.msg_id` was an unfillable field (cycle-1 B11). Consequence folded (cycle-2 R2, cycle-3 R3): `tg-post.sh:81` writes the sender alias from `BOT_NAME`; Task 2 makes it `${BOT_ID:-$BOT_NAME}` so the chunk-3 join can work for a session while hand callers that set only `BOT_NAME` keep working.
- **F5 — A cross-fleet `manages:` target does NOT make a manager leaf.** *Context:* `bots.<id>.manages:` CAN name a cross-fleet report — `manager_bots()`'s docstring says it is "the only one of the two that can express a CROSS-FLEET report" (`config.py:736-748`) and `_validate_teams` warns rather than errors on a target outside the fleet (`validator.py:1205-1216`) — so the question is whether such a target makes the declaring manager a LEAF. *Options:* count it / do not. *Lean:* do not (a manager whose reports are other fleets' managers is a coordinator, not a leaf). *Ratifier:* operator. **Status: locked** — evidence: the docstring and validator lines above (cycle-5 correction: the earlier evidence line read the citation backwards); spec §10 and §13 corrected. Consumed by chunk 5.

- **F6 — A one-way early look at chunk 2's arming instant.** *Context:* the hand-inject interval between chunks 1 and 2 produces real rows and the `T0` instrument already exists (`baseline_read`), but §12.4's window opens only at chunk 2's arming, so the interval is measured by nothing (cycle-9 ceo-review). *Options:* (a) at chunk 2's arming, take the interval's reading through the same instrument and register it as a ONE-WAY look — `inert` or `net-negative` there can stop the ladder, nothing there can pass it; (b) discard the interval. *Lean:* (a) — the same instrument, no build, and it pulls the stop consequence one chunk earlier. *Ratifier:* operator. **Status: superseded (2026-09-16)** — the build-to-the-end ruling removes the interval: the trigger ships in PR 2 of one series and the loop is deployed once, so there is nothing to look at early.
- **F7 — Does chunk 2 wait for a live `dispatch`?** *Context:* step 9's real run proves the record clause on any action, but the join clause live only on a `dispatch`; the canary's workers read 280, 112 and 0 busy minutes over three weeks, so a live dispatch may take days (cycle-9 adversarial). *Options:* (a) chunk 2 proceeds on Task 5's real-plane join test, and the first live dispatch in the hand-inject interval is pasted as a second PR comment when it lands; (b) chunk 2 waits for that dispatch. *Lean:* (a) — the join is proved mechanically on a real plane, a wait could stall the ladder on the fleet's backlog rather than on the loop, and F6's early look catches an inert interval. *Ratifier:* operator. **Status: superseded (2026-09-16)** — one deploy after PR 3: the join clause is proved by the beat's own dispatches, and there is no chunk 2 to wait.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| A check-in `dispatch` opens the envelope gate through `--project`, so its row carries a deadline and the existing `fleet-pulse` watchdog may page the operator on overdue work the surfacing judgment never chose to raise | operator attention spent outside the check-in's own rate limit | this is the shipped dispatch behaviour, not new machinery; the shipped watchdog's own dedupe and escalation rules bound the paging; the read door's summary (PR 3) shows how many check-in dispatches went overdue (cycle-9 engineering) |
| The skill text is wrong on first contact with a real manager (a refused decision, a door the model cannot drive) | the first real run fails | the dry run precedes the real run (Task 7 steps 8–9) and prints the exact ACT command line it would run; skill text is library content, live on the next `generate`, so a fix is a follow-up PR against `library/` with no restart |
| `lib/dispatch-task.sh` reaches every fleet on pull with no per-bot canary | a regression in the door hits the estate | the flags are additive; behaviour without them is pinned byte-identical by `tests/test_task_id_dispatch.py` + `tests/test_dispatch_task.sh`; the harness runs the real door; the real run on the canary is the positive control |
| The manager reads the record's `considered` list as licence to write essays | the 16 KiB DIAGNOSTIC cap truncates the record | every list is capped in the contract (≤ 10 × ≤ 200 chars); `rationale` and `raise.reason` ≤ 600 — a maximal record measures 7,887 bytes (cycle 3), so the cap is defensive; a truncated row is still LISTED by the read door |
| GitHub or Claudron is unreachable at check-in time | the manager cannot see the backlog | the degraded rule is per input: plane-known open work is still dispatchable; only backlog-sourced new work is withheld |
| The record lands and the ACT then fails (a partial RECORD-before-ACT) | a decision row that reads as acted on | the skill records a follow-up check-in naming the failure (`prev_checkin_id` set, `action: nothing`); `task-act.sh withdraw` is the undo for a dispatch that must not stand; chunk 3's outcome join makes the pair visible |

## Global Constraints

Every task's requirements include these. Exact values are copied from the spec and from `CLAUDE.md`.

- **The repo is PUBLIC.** No PII, real chat ids, user ids, handles, tokens, tailnet names or fleet-specific paths in any committed asset, test, fixture or commit message. Fixtures are shape-verbatim with faked identifiers. Never `@`-mention a bot name in GitHub-bound text. The canary fleet, its manager and the host's install root are named by the operator (ruling 13) into shell variables at Task 0 — never written into this plan, a commit or a PR comment.
- **bash 3.2 target.** `set -euo pipefail`; source `lib-common.sh`; quote every variable; `printf '%s'` for values; **no apostrophes in comments inside a command substitution** (`tests/test_bash_parse.py` gates `lib/` and every `library/**/*.sh`). **Never guard a flag value with `${2:?…}`** — an expansion fault exits rc 0 through lib-common's EXIT trap (`lib-common.sh:337`; `dispatch-task.sh:94-95` documents it and uses `_flag_val`).
- **The ERR trap fires INSIDE a command substitution.** `install_error_trap` arms `set -E` and an ERR trap that calls `emit_script_error` (`script_error` is `critical`, `registries.py:80`); bash fires that trap for a failing command inside `$( )` whatever surrounds the substitution — `if !`, `||`, `set +e` — measured (cycle 3), and re-measured for this cycle: a door that EXPECTS a nonzero child runs it as a **top-level `if` pipeline into a file** (form D: `if printf … | python3 … > "$tmp"; then`), which fires nothing on either path; a substitution form lands a `critical` row while the door says "nothing recorded".
- **New `system` event kinds need NO migration and NO contract change** (`contracts.py:389-418`). Register severity with one line per kind in `claudlobby/plane/registries.py:54`.
- **The `Assignment` payload is `_Strict` (`contracts.py:344`)** — the join is its own system event in the same batch.
- **The plane is always on.** `plane_armed` (`lib-common.sh:495-524`) is opt-OUT: `PLANE_EMIT_DISABLED=1` is the only silencer.
- **Emit from bash through `plane_emit_events <door> <<<"$batch"`** (a here-string, never a pipeline). Every `system` event is actor-anchored `"subject_kind":"actor","subject":"bot:<fleet>/<bot_id>"` — **`BOT_ID`, never `BOT_NAME`** (`lib-common.sh:1420`; `config.py:521`). Ids are minted by `plane_mint_id <prefix>` (`lib-common.sh:526-533`), never a private copy.
- **Doors' rc ladder follows `task-act.sh:55-56`:** 0 acted · 1 usage · 2 refused · 3 the plane could not record. A `--project` key is a `projects.yaml` slug, the recorded rule at `documentation/projects-yaml-schema.md:25`. A read door's usage error is rc 2 (`--since` unparseable) — `dispatch-overdue.py`'s convention (rc 2 = a malformed call, rc 3 = the question cannot be answered), not `task-act.sh`'s, whose rc 1 is usage; `report-back` uses rc 1 for the same case. The docstring says which.
- **Skill grants** follow the shipped shapes: `Bash(<cmd> *)` for a command (`status/SKILL.md:6-8`), `Bash(*<script>*)` star-bounded with **no space** for a lib script (`restart/SKILL.md:4`), `mcp__plugin_telegram_telegram__reply` for posting. A pipeline is matched per subcommand (`documentation/decisions/permissions-model.md:56`), so a door is invoked directly with a here-doc, never through `cat |`. **Never `Bash(claudron *)`** — the boundary allows verbs only (`documentation/integrations/claudron-integration.md:29`; Invariant 5 `tests/test_boundary_invariants.py:275-292`): `Bash(claudron lookup *)`. `gh` gets the one verb it needs: `Bash(gh issue list *)`. **A builtin is a subcommand like any other** (`echo`, `printf`, `env`) and no grant covers it: a skill command carries granted commands ONLY, and facts a bot already has in its composed context (the `## Projects` table, the `## Fleet Mission` section) are read there, never through a shell call (cycle-8).
- **`brief --json` degrades by MODE, not by presence.** Each `degraded[]` entry is `{field, mode, reason, issue, count}` (`brief.py:157-185`); `mode` is `labeled` (present, bounded) or `omitted` (absent by design). Captured live this cycle on a composed fleet: with no plane db every one of `dispatches.open/overdue/orphaned`, `workstreams`, `reports`, `alerts` arrives `omitted` (#1467); with a plane present the list is **never empty** — `utilization: omitted` (#891, `brief.py:927-929`, the one unconditional append), and on this tree also `alerts: labeled` (#903, `brief.py:818-826`, guarded by `has_ssot`, which is False while `known_values` has no `FLEET_EVENT_TYPES`) and `dispatches.orphaned: labeled` (#1014, `:584-587`, when no bots dir resolves). A rule that reads *any* entry naming a field as "unavailable" can never dispatch on a real fleet (cycle-1 B9 → cycle-2 B6 → cycle-3 B1); the rule keys on `mode == "omitted"`.
- **Read doors:** unreachable ≠ empty — `refuse_unreachable("checkins", note)` (`commands/_helpers.py:160`, prints `UNREACHABLE` upper-case, rc 3). The shared plane session's connection **yields tuples** (`plane-readers.py:53-58`; `status.py:218`) — `plane_session` for the reachability/roster refusal, then `plane.db.open_ro(root)` (`commands/task.py:51`) for named rows. A fleet the plane has never seen refuses at rc 3 by `plane_session`'s roster rule (#1014's class) — inherited deliberately, pinned by test.
- **Line numbers** are as of `main` @ `a96b47f`. Re-anchor by the symbol named beside a line, never by the number.
- **Tests run unsandboxed; the baseline is red** (~46 failed / 2 errors on macOS). Under the sandbox, macOS `mktemp` ignores `TMPDIR` and `lib-common.sh`'s source-time `mktemp -d` fails — every bash-door test fails spuriously (the ~250-phantom mode CLAUDE.md documents). The gate is *names + counts* (Task 7), never `pytest | grep`; never pipe `claudlobby validate` or `diff` into `head`/`grep` — redirect to a file, read rc, then read the file.
- **Test command form:** `./.venv/bin/pytest tests/<file>.py -q` from `$WT`, whose `.venv` Task 0 creates. Pre-existing test files this plan names, all present on main: `tests/test_skill_ref_resolution.py`, `tests/test_task_id_dispatch.py`, `tests/test_dispatch_type.py`, `tests/test_dispatch_task.sh`, `tests/test_bash_parse.py`, `tests/test_plane_system_events.py`, `tests/test_main.py`, `tests/test_readme_library_counts.py`, `tests/test_boundary_invariants.py`, `tests/test_no_dead_session_command.py`, and the twelve `tg-post.sh` suites Task 2 runs (`tests/test_tg_post.sh`, `test_creds_check_telegram.py`, `test_fleet_pulse_escalated.py`, `test_fleet_pulse_events_plane.py`, `test_maintenance_jobs.py`, `test_notify_behind.py`, `test_plane_gauntlet_doors.py`, `test_plane_events_door.py`, `test_alert_recipient.sh`, `test_system_defaults.py`, `test_orphan_browser_reaper.sh`, `test_host_health_check.sh`).
- **Operator config (`local/<fleet>/fleet.yaml`) is REPORTED, never edited by the executor**, and **no unmerged code ever runs on the live host**: the canary manager is equipped after merge and pull (Task 7 step 7). Read-only queries against the live plane (Task 0) are the one thing that touches the host before merge.
- **Commits:** message via `git commit -F <file>`; end every message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## File structure

**Create**
| Path | Responsibility |
|---|---|
| `lib/checkin-contract.py` | Stdlib contract for the schema-1 decision record: `normalize(obj, checkin_id)` lists every defect; CLI filter stdin → normalized JSON, rc 2 with reasons. |
| `lib/checkin-record.sh` | The decision door: mint `ck_` id → validate (form D) → ONE `checkin_decision` system event, `source_ref checkin:<id>`. No flags but `--dry-run`/`--help`; `--dry-run` prints `DRY-RUN <id>`. |
| `claudlobby/commands/checkins.py` | `claudlobby checkins` — rows newest first, `--last`, `--bot`, `--since`, `--raised` (the asks only), `--json`; text capped at 10 rows with a disclosure line; rc 3 on an unreachable plane. |
| `library/skills/checkin/SKILL.md` | The `/checkin` reasoning contract: READ 0–5 through named doors (incl. `claudlobby status --json` for who is idle), DECIDE one project then one of `dispatch \| ask \| nothing`, RECORD before ACT, the mode-keyed per-input degraded rule, the surfacing judgment, the follow-up rule for a failed ACT. |
| `tests/test_checkin_contract.py` | The contract + the two severity registrations. |
| `tests/test_checkin_doors.py` | `checkin-record.sh` (Task 1), `dispatch-task.sh --project/--checkin` and the `tg-post.sh` alias (Task 2) — stub transport, real plane; a refusal on the real rig lands NO row. |
| `tests/test_checkins_cli.py` | The read door over a seeded plane. |
| `tests/test_checkin_library.py` | The skill file is whole (three bash blocks); its RECORD template carries every contract key; the skill is coupled to its doors by name; its grants match its own command lines (whitespace-collapsed) and contain no forbidden wildcard. |

**Modify**
| Path | Change |
|---|---|
| `lib/dispatch-task.sh:5-24, 78-82, 104-117, 350-354, 395-405, 660-667, 681-689` | `--project KEY`, `--checkin ck_<32hex>`; `DISPATCH_PROJECT` opens the envelope gate; `project_key` on the work item; a `checkin_dispatch` system event appended to the batch AFTER the `emit_triple` gate, `task_id` null on an id-less dispatch; disclosure when `--checkin` rides an untracked dispatch. |
| `lib/plane-lookup.py:176-235` (`main`) | a `--checkin-id ck_<32hex>` mode: prints the id when a `checkin_decision` row carries `source_ref checkin:<id>`, else nothing + a stderr note (the `--task-id` contract); rc 3 unreachable. |
| `lib/tg-post.sh:81` (+ the comment at `:64`) | sender alias `bot:$FLEET_NAME/${BOT_ID:-$BOT_NAME}` (was `BOT_NAME`; `set -u` at `:13`, hand callers set only `BOT_NAME`). |
| `claudlobby/plane/registries.py:54-123` | two severity lines. |
| `claudlobby/plane/queries.py` (append) | `CHECKIN_ROWS_SQL` — the light form: `subject_alias, occurred_at, detail, detail_truncated, ingest_seq`. |
| `claudlobby/commands/_parsers.py:189-197` + imports | registers `checkins` beside `workstreams` (a standalone import line after the `from .core import (…)` block). |
| `lib/validate-bot-change.sh` (append a block) | the empirical gate for the record door and the read door. |
| `README.md:145-146` | library counts 54 skills / 39 protocols (unchanged — the protocol is chunk 2's) / **92** lib scripts — measured with `tests/test_readme_library_counts.py`'s own rule (it excludes `.py`, so `checkin-contract.py` does not count). |
| `documentation/guides/observability.md` (question→door table, three cells), `CLAUDE.md` (lib table; `commands/` line → 15 files; Key Commands), `CHANGELOG.md` | rows for the two lib scripts and `checkins`. |

**Sizing:** Task 0 S · Task 1 L · Task 2 M · Task 3 M · Task 4 M · Task 5 S · Task 6 S · Task 7 L in wall time (the gauntlet costs roughly Tasks 1–6 combined).

---

### Task 0: Worktree, venv, the before leg

**Files:** none changed. Produces the branch, the venv and the *before* leg every later gate diffs against.

**Interfaces:** produces `$WT` (every later step runs from it), `$OUT` (a sibling directory for the run's evidence files — `before.txt`, `run_before.txt`, `vbc.txt`, `mutants.md`, `pr-body.md` — outside any session-scoped `$TMPDIR`, since Task 7 may run in another session) and `$OUT/env.sh` (the three host facts `$CK_FLEET` / `$CK_MGR` / `$MINI_ROOT` — the canary names from ruling 13 and the host's install root, set by the executor from the operator's words, never `ls | head -1`, never written into a committed file — plus the `no_names` gate, sourced first by every later block; nothing survives between tool calls).

- [ ] **Step 1: Worktree on a fresh branch off main — after this plan and its spec are ON main**

This plan and its spec must be in the tree the PR merges into (cycle-6 R10): PR #1550 (the docs branch) merges first, and the first command below refuses otherwise.

```bash
git -C /Users/chris/Projects/Claudlobby fetch -q origin main
for d in documentation/plans/2026-09-14-manager-checkin-chunk1-contract.md documentation/plans/2026-09-13-manager-checkin-design.md; do
  git -C /Users/chris/Projects/Claudlobby show "origin/main:$d" > /dev/null || { echo "STOP: $d is not on origin/main -- merge PR #1550 first"; exit 1; }
done
WT="$HOME/Projects/claudlobby-worktrees/ck1"      # the one path every later step uses
OUT="$WT-out"                                       # evidence files, beside the worktree, never in a session TMPDIR, never committed
mkdir -p "$(dirname "$WT")" "$OUT"
# The three host facts are SET IN THE SHELL before this block (`export CK_FLEET=…` etc.); the guards refuse an
# unset one, which a placeholder assignment would defeat. They are then written ONCE to $OUT/env.sh -- outside
# the repo -- and EVERY later block begins by sourcing that file, because shell state does not survive between
# the executor's tool calls, let alone sessions (cycle-7 B4, measured). The identifier gate lives in the same file.
: "${CK_FLEET:?the engineering fleet name -- ruling 13, as the operator spells it in local/*/<fleet>/fleet.yaml}"
: "${CK_MGR:?its leaf manager BOT_NAME as keepalive stamps the heartbeat subject (keepalive.sh:102) -- the id unless the fleet declares a display name; the baseline refuses on a mismatch}"
: "${MINI_ROOT:?the claudlobby install root on the host (where state/plane/plane.db and .venv live)}"
{
  printf "export WT='%s' OUT='%s'\n" "$WT" "$OUT"
  printf "export CK_FLEET='%s' CK_MGR='%s' MINI_ROOT='%s'\n" "$CK_FLEET" "$CK_MGR" "$MINI_ROOT"
  cat <<'FN'
no_names() {   # <file>: refuse when any host identifier reached a body bound for the public PR -- names on word boundaries, paths literal; the matching lines print first (cycle-8)
  local f="$1" hits=0 v pat out
  for v in "$CK_FLEET" "$CK_MGR"; do
    [ -n "$v" ] || continue; pat=$(printf '%s' "$v" | sed 's/[][\.*^$/]/\\&/g')
    out=$(grep -n -E "(^|[^A-Za-z0-9_-])${pat}([^A-Za-z0-9_-]|$)" "$f" | head -3); [ -z "$out" ] || { printf '%s\n' "$out"; hits=$((hits + 1)); }
  done
  for v in "$MINI_ROOT" "$(dirname "$MINI_ROOT")"; do
    out=$(grep -n -F -- "$v" "$f" | head -3); [ -z "$out" ] || { printf '%s\n' "$out"; hits=$((hits + 1)); }
  done
  if [ "$hits" = 0 ]; then echo "identifiers in $(basename "$f"): clean"; else echo "STOP: $hits identifier(s) reached $f -- the matching lines are above; fix the PRODUCING step, never the body (a name is matched as a whole word, so a hit is a hit)"; exit 1; fi
}
FN
  printf 'cd "%s" || { echo "STOP: the worktree %s is missing -- Task 0 step 1 creates it"; exit 1; }\n' "$WT" "$WT"     # the OTHER half of shell state: every sourcing block lands in the worktree (cycle-8 B1)
} > "$OUT/env.sh"
cd /Users/chris/Projects/Claudlobby
git fetch -q origin main
git worktree add -B checkin/chunk1-record "$WT" origin/main     # -B: a branch of that name is left over from an earlier attempt at an older tip (measured, cycle 9) -- reset it to the tip just fetched, never reuse it
. "$OUT/env.sh"; echo "env: $(wc -l < "$OUT/env.sh" | tr -d ' ') lines; gate: $(type no_names | head -1); cwd: $(pwd)"; git log --oneline -1
```
Expected: one line, the tip of `origin/main`.

- [ ] **Step 2: A venv IN the worktree**

```bash
. "$HOME/Projects/claudlobby-worktrees/ck1-out/env.sh"
python3 -m venv .venv && ./.venv/bin/python -m pip install -q -e '.[dev]'
./.venv/bin/python -c "import claudlobby, pathlib; print(pathlib.Path(claudlobby.__file__).resolve())"
ls .venv/bin/claudlobby
```
Expected: a path under `$WT/claudlobby/` and the console script present (the door tests reach the plane through it via `PLANE_EMIT_CLI`). Any other path: stop — the editable finder is shadowing the worktree.

- [ ] **Step 3: The before leg, names + counts (unsandboxed)**

```bash
. "$HOME/Projects/claudlobby-worktrees/ck1-out/env.sh"
./.venv/bin/pytest --tb=no -ra > "$OUT/run_before.txt" 2>&1; echo "rc=$?"
awk "/short test summary info/,0" "$OUT/run_before.txt" | grep -E "^(FAILED|ERROR)" | sed 's/ - .*//' | sort -u > "$OUT/before.txt"
wc -l < "$OUT/before.txt"; tail -1 "$OUT/run_before.txt"
```
Expected: `rc=1` (the baseline is red) and a `N failed, M passed` line. rc 2/4/5/127 means the run did not complete.

---

### Task 1: The decision record — the contract, the record door, the severities

**Files:**
- Create: `lib/checkin-contract.py`, `lib/checkin-record.sh`
- Modify: `claudlobby/plane/registries.py:54-123`
- Test: `tests/test_checkin_contract.py`, `tests/test_checkin_doors.py` (the record half)

**Interfaces:**
- Produces: `checkin-contract.py`: `normalize(obj, *, checkin_id=None) -> dict` (raises `ContractError(reasons)`; the door supplies `checkin_id`, minted by `plane_mint_id ck`; the contract validates its shape and never mints), CLI `python3 checkin-contract.py [--checkin-id ck_<32hex>] < decision.json` rc 0/2. `checkin-record.sh [--dry-run] < decision.json` → stdout `ck_<32hex>` (`DRY-RUN ck_<32hex>` under `--dry-run`, so a dry id can never be mistaken for a recorded one); rc 0 recorded · 1 usage (unknown flag, no identity) · 2 contract refused · 3 plane could not record (or silenced). Identity is `BOT_ID` + `FLEET_NAME` from the environment only (no flags — nothing consumed them; cycle-2 gap). Plane row: `kind='system'`, `event='checkin_decision'`, `source_ref='checkin:<ck_id>'`, `subject_alias='bot:<fleet>/<bot_id>'`, `severity='notice'`, `detail` = the normalized record. **A refusal lands no row of any kind** — the contract runs as a form-D pipeline, so the ERR trap never fires (cycle-3 B2).
- Schema 1 (the whole of it; `propose`, `sprint`, `focus_*` arrive as schema 2):

```json
{ "schema": 1, "checkin_id": "ck_<32hex>", "prev_checkin_id": "ck_<32hex> | null  (REQUIRED: null = READ 0 answered none)",
  "inputs_seen": { "open_tasks": "int | null", "stalls": "int | null", "unacked": "int | null",
                   "issues_seen": "int | null  (the RAW backlog count, before the mission filter; capped at READ 4's per-repo limit)",
                   "issues_considered": "int | null  (after it; null when mission is unavailable)", "knowledge_hits": "int | null",
                   "considered": ["<candidate> — <why not chosen>", "... non-empty when action is dispatch, or nothing over a visible backlog"],
                   "unavailable": ["gh", "... ALWAYS present (empty allowed): the reads that failed, by name"] },
  "delta": { "tasks_opened": "int | null", "tasks_completed": "int | null", "stalls_appeared": "int | null",
             "stalls_cleared": "int | null", "issues_new": "int | null", "messages_new": "int | null",
             "held_pending": "int | null" },
  "action": "dispatch | ask | nothing",
  "project_key": "<slug> | null  (a slug is REQUIRED for dispatch)",
  "rationale": "<= 600 chars",
  "raise": { "decided": "true | false  (true is REQUIRED for ask)", "reason": "<non-empty, <= 600>", "held": ["<= 10 items"] } }
```
`considered` is the losers list — what was weighed and passed over, one line each — because "a selector can only be judged against what it did NOT pick" (`lib/sprint-selection-record.py:11-24`, Phase 0 of #974; cycle-2 B8), and on `dispatch` it must be non-empty (cycle-3 R4) — and on `nothing` whenever `issues_seen` is a positive count (cycle-7 gap: the `nothing` rows are the population an *inert* verdict is diagnosed from, so a backlog passed over without a word is a defect; `null` and `0` require nothing). `issues_seen` beside `issues_considered` is the second half of that module's rule (`:26-42`): the raw count beside the filtered one tells a broken FILTER (seen > 0, considered 0) from an empty backlog; it is capped at READ 4's per-repo limit, so 0 versus non-zero is what it discriminates (cycle-8). The module's third half is provenance (`:37-38`: an empty set needs the rc of every query that produced it), and the contract's analogue is `inputs_seen.unavailable` — so BOTH list keys must be PRESENT (empty allowed; a missing key is a named defect, exactly as a missing count is), because an omitted `unavailable` would normalize into the claim that nothing was unavailable; a typo'd `PROJECT_REPOS_*` is a broken QUERY and is separated by `unavailable`, not by the count — F1's 1d gate (cycle-8 precedent). Two more one-line rules (cycle-8): `raise.decided` true REQUIRES `action: ask` (an ask IS the surfacing; `--raised` counts on it, so a `dispatch` row that set it would silence the manager under the rate limit), when `mission` is in `unavailable`, `issues_considered` must be `null` (a count filtered by a mission nobody read is fabricated), and when `checkins` is in `unavailable`, `prev_checkin_id` must be `null` (cycle 9: the same shape — a previous id nobody read is fabricated, and READ 0 is the only source of one). Because the contract refuses a `dispatch`, and a `nothing` over a visible backlog, without `considered`, this record cannot exist unwritten — which Phase 0's selection record could, and did. **Every count in `inputs_seen` and `delta` is `int | null`, null = could not measure, never 0, and a MISSING count is a defect, not a 0** (cycle-1 R17, cycle-3 B3) — enforced for BOTH blocks (cycle-4 B3: the first version enforced `inputs_seen` only, and the harness fixture's `"delta":{}` normalized to seven fabricated nulls at rc 0). `prev_checkin_id` is required so a skipped READ 0 cannot record as a first check-in (cycle-3 gap). `raise.reason` is required non-empty and capped like `rationale` (cycle-2 R8). Every list ≤ 10 strings of ≤ 200 chars, so the record stays far under the 16 KiB cap (7,887 bytes maximal, measured).

- [ ] **Step 1: Write the failing contract tests**

```python
# tests/test_checkin_contract.py
"""The schema-1 decision record (manager check-in spec §7): lib/checkin-contract.py
(stdlib, the dispatch-overdue.py precedent) and the two severity registrations."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit_batch

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT = REPO_ROOT / "lib" / "checkin-contract.py"
CK = "ck_" + "a" * 32
PREV = "ck_" + "b" * 32

_spec = importlib.util.spec_from_file_location("checkin_contract", CONTRACT)
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)

INPUTS = {"open_tasks": 2, "stalls": 0, "unacked": 1, "issues_seen": 9, "issues_considered": 4,
          "knowledge_hits": 1, "considered": ["#12 flaky test — not mission work"], "unavailable": []}


def _decision(**over) -> dict:
    d = {
        "prev_checkin_id": None,
        "inputs_seen": dict(INPUTS),
        "delta": {"tasks_opened": 0, "tasks_completed": 1, "stalls_appeared": 0,
                  "stalls_cleared": 0, "issues_new": 1, "messages_new": None, "held_pending": 0},
        "action": "nothing",
        "project_key": None,
        "rationale": "All work in flight; nothing new worth starting.",
        "raise": {"decided": False, "reason": "no delta the operator would want", "held": []},
    }
    d.update(over)
    return d


def test_a_decision_normalizes_with_the_door_supplied_id():
    out = cc.normalize(_decision(), checkin_id=CK)
    assert out["schema"] == 1 and out["checkin_id"] == CK
    assert out["prev_checkin_id"] is None
    assert out["delta"]["messages_new"] is None          # null survives: could not measure
    assert out["delta"]["tasks_completed"] == 1
    assert out["inputs_seen"]["issues_seen"] == 9 and out["inputs_seen"]["issues_considered"] == 4
    assert out["inputs_seen"]["considered"] == ["#12 flaky test — not mission work"]
    assert "targets" not in out and "focus_declared" not in out["inputs_seen"]


def test_the_contract_never_mints():
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(_decision())                         # no id from the door, none in the body
    assert any("checkin_id" in r for r in exc.value.reasons)


def test_prev_is_kept():
    out = cc.normalize(_decision(prev_checkin_id=PREV), checkin_id=CK)
    assert out["prev_checkin_id"] == PREV


def test_prev_checkin_id_is_required_so_a_skipped_read_0_cannot_pose_as_a_first_checkin():
    d = _decision()
    del d["prev_checkin_id"]
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(d, checkin_id=CK)
    assert any("prev_checkin_id required" in r for r in exc.value.reasons)


def test_an_input_count_the_manager_could_not_measure_is_null_never_zero():
    seen = {**INPUTS, "issues_seen": None, "issues_considered": None}
    out = cc.normalize(_decision(inputs_seen=seen), checkin_id=CK)
    assert out["inputs_seen"]["issues_seen"] is None and out["inputs_seen"]["issues_considered"] is None


def test_a_missing_input_count_is_a_defect_not_a_zero():
    seen = {k: v for k, v in INPUTS.items() if k != "stalls"}
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(_decision(inputs_seen=seen), checkin_id=CK)
    assert any("inputs_seen.stalls required" in r for r in exc.value.reasons)


def test_a_missing_delta_count_is_a_defect_not_a_null():
    d = _decision()
    del d["delta"]["held_pending"]
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(d, checkin_id=CK)
    assert any("delta.held_pending required" in r for r in exc.value.reasons)


def test_nothing_with_a_visible_backlog_must_record_its_losers():
    # the nothing rows are the population an inert verdict is diagnosed from (cycle-7 gap)
    d = _decision()                                   # issues_seen 9 and a non-empty considered: fine
    d["inputs_seen"]["considered"] = []
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(d, checkin_id=CK)
    assert any("action nothing with issues_seen > 0" in r for r in exc.value.reasons)
    d["inputs_seen"]["issues_seen"] = 0               # nothing was there to pass over: no losers required
    assert cc.normalize(d, checkin_id=CK)["action"] == "nothing"
    d["inputs_seen"]["issues_seen"] = None            # could not measure: not a positive count
    assert cc.normalize(d, checkin_id=CK)["action"] == "nothing"


def test_a_missing_list_key_is_a_defect_not_an_empty_list():
    # the provenance half of the cited rule (cycle-8): an omitted `unavailable` would
    # normalize into the claim that nothing was unavailable
    for key in ("considered", "unavailable"):
        d = _decision()
        del d["inputs_seen"][key]
        with pytest.raises(cc.ContractError) as exc:
            cc.normalize(d, checkin_id=CK)
        assert any(f"inputs_seen.{key} required" in r for r in exc.value.reasons)
    d = _decision(); d["inputs_seen"]["unavailable"] = []       # present and empty is fine
    assert cc.normalize(d, checkin_id=CK)["inputs_seen"]["unavailable"] == []


def test_raise_decided_true_requires_an_ask():
    # --raised filters on raise.decided; a dispatch row that set it would count as an ask
    d = _decision(action="dispatch", project_key="shop")
    d["raise"] = {"decided": True, "reason": "surfaced", "held": []}
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(d, checkin_id=CK)
    assert any("raise.decided true requires action ask" in r for r in exc.value.reasons)


def test_a_previous_id_with_no_read_door_is_fabricated():
    d = _decision(prev_checkin_id=PREV)
    d["inputs_seen"]["unavailable"] = ["checkins"]
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(d, checkin_id=CK)
    assert any("prev_checkin_id must be null when checkins is unavailable" in r for r in exc.value.reasons)
    d["prev_checkin_id"] = None
    assert cc.normalize(d, checkin_id=CK)["prev_checkin_id"] is None


def test_a_filtered_count_with_no_mission_is_fabricated():
    d = _decision()
    d["inputs_seen"]["unavailable"] = ["mission"]
    with pytest.raises(cc.ContractError) as exc:                # issues_considered is 4 in the fixture
        cc.normalize(d, checkin_id=CK)
    assert any("issues_considered must be null when mission is unavailable" in r for r in exc.value.reasons)
    d["inputs_seen"]["issues_considered"] = None
    assert cc.normalize(d, checkin_id=CK)["inputs_seen"]["issues_considered"] is None


def test_dispatch_must_record_its_losers():
    d = _decision(action="dispatch", project_key="shop")
    d["inputs_seen"]["considered"] = []
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(d, checkin_id=CK)
    assert any("inputs_seen.considered non-empty" in r for r in exc.value.reasons)
    d["inputs_seen"]["considered"] = ["#12 flaky test — not mission work"]
    assert cc.normalize(d, checkin_id=CK)["action"] == "dispatch"


@pytest.mark.parametrize("over, needle", [
    ({"action": "dispatch"}, "must name project_key"),
    ({"action": "ask"}, "raise.decided"),
    ({"action": "propose"}, "action must be one of"),        # schema 2, not this chunk
    ({"action": "coffee"}, "action must be one of"),
    ({"rationale": "x" * 601}, "rationale must be <= 600"),
    ({"rationale": ""}, "rationale"),
    ({"project_key": "Not-A-Slug"}, "project_key"),
    ({"prev_checkin_id": "nope"}, "prev_checkin_id"),
    ({"inputs_seen": {**INPUTS, "open_tasks": -1}}, "inputs_seen.open_tasks"),
    ({"inputs_seen": {**INPUTS, "considered": ["x"] * 11}}, "inputs_seen.considered"),
    ({"inputs_seen": {**INPUTS, "considered": ["x" * 201]}}, "inputs_seen.considered"),
    ({"delta": {"tasks_opened": -1}}, "delta.tasks_opened"),
    ({"raise": {"decided": False, "reason": ""}}, "raise.reason"),
    ({"raise": {"decided": False, "reason": "r" * 601}}, "raise.reason must be <= 600"),
    ({"raise": {"decided": "yes", "reason": "r"}}, "raise.decided"),
    ({"raise": {"decided": False, "reason": "r", "held": ["h"] * 11}}, "raise.held"),
])
def test_defects_are_listed_by_name(over, needle):
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(_decision(**over), checkin_id=CK)
    assert any(needle in r for r in exc.value.reasons), exc.value.reasons


def test_every_defect_is_reported_not_just_the_first():
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(_decision(action="coffee", rationale=""), checkin_id=CK)
    assert len(exc.value.reasons) >= 2


def test_the_cli_is_a_filter():
    ok = subprocess.run([sys.executable, str(CONTRACT), "--checkin-id", CK],
                        input=json.dumps(_decision()), capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["checkin_id"] == CK
    bad = subprocess.run([sys.executable, str(CONTRACT), "--checkin-id", CK], input="not json",
                         capture_output=True, text=True)
    assert bad.returncode == 2 and "not JSON" in bad.stderr
    bad = subprocess.run([sys.executable, str(CONTRACT), "--checkin-id", CK],
                         input=json.dumps(_decision(action="coffee")), capture_output=True, text=True)
    assert bad.returncode == 2 and "checkin-contract: action must be one of" in bad.stderr and bad.stdout == ""
    bad = subprocess.run([sys.executable, str(CONTRACT), "--checkin-id", "nope"],
                         input=json.dumps(_decision()), capture_output=True, text=True)
    assert bad.returncode == 2 and "checkin_id" in bad.stderr


@pytest.mark.parametrize("kind", ["checkin_decision", "checkin_dispatch"])
def test_the_two_kinds_carry_notice_severity(tmp_path, kind):
    emit_batch(tmp_path, [{
        "event_type": "system", "emitter": "t", "fleet": "f",
        "payload": {"event": kind, "subject_kind": "actor", "subject": "bot:f/mgr", "data": {"schema": 1}}}])
    conn = connect(db_path(tmp_path))
    row = conn.execute("SELECT severity FROM events WHERE kind='system' AND event=?", (kind,)).fetchone()
    conn.close()
    assert row["severity"] == "notice"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/pytest tests/test_checkin_contract.py -q`
Expected: collection fails on the missing `lib/checkin-contract.py`; the severity test alone would fail with `None`.

- [ ] **Step 3: The severities**

In `claudlobby/plane/registries.py`, inside `SYSTEM_EVENT_SEVERITY` before its closing `}`:

```python
    # manager check-in (spec §7): the decision record, and the join row a
    # `dispatch-task.sh --checkin` appends to its batch. notice — the record
    # IS the point; nothing here pages.
    "checkin_decision": "notice",
    "checkin_dispatch": "notice",
```

- [ ] **Step 4: The contract module**

```python
#!/usr/bin/env python3
# lib/checkin-contract.py
"""The check-in decision record contract (manager check-in spec §7), schema 1.

Stdlib only (the dispatch-overdue.py precedent): checkin-record.sh pipes the
manager's decision JSON through `normalize` before anything reaches the plane,
so a malformed decision is refused AT THE DOOR with every reason named, never
landed as a row no reader can join.

    python3 checkin-contract.py --checkin-id ck_<32hex> < decision.json
    exit 0 ok (normalized JSON on stdout) / 2 contract violation (reasons on stderr)

The DOOR mints the id (lib-common's plane_mint_id, the one mint every door
uses); this module validates its shape and never mints. Schema 1 is the record
ONE hand-equipped manager can produce this chunk: actions dispatch | ask |
nothing. Later chunks ADD (propose, sprint, focus fields) as schema 2. Every
count in `inputs_seen` and `delta` is int | null -- null means "could not
measure" and is never collapsed to 0, because an unchanged delta is the skill's
primary argument for silence -- and a MISSING count is a defect, not a 0.
`inputs_seen.considered` is the losers list: a selector can only be judged
against what it did NOT pick (lib/sprint-selection-record.py, #974), so on
`dispatch` it must be non-empty, and on `nothing` whenever `issues_seen` is a
positive count (the nothing rows are the population an inert verdict is
diagnosed from); `issues_seen` beside `issues_considered` is
that module's second rule -- the raw count (capped at READ 4's per-repo limit,
so 0 vs non-zero is what it discriminates) beside the filtered one, so a broken
FILTER and an empty backlog do not produce the same row; a broken QUERY is
separated by `inputs_seen.unavailable`, the module's third rule (provenance), so
both list keys must be PRESENT, empty allowed, never defaulted. `raise.decided`
true requires action ask (--raised counts on it); with `mission` unavailable,
`issues_considered` must be null. `prev_checkin_id` is required
(null = READ 0 answered "none") so a skipped read cannot pose as a first check-in.
"""
from __future__ import annotations

import json
import re
import sys

SCHEMA = 1
ACTIONS = ("dispatch", "ask", "nothing")
TEXT_MAX = 600
LIST_MAX = 10
ITEM_MAX = 200
INPUTS_COUNTS = ("open_tasks", "stalls", "unacked", "issues_seen", "issues_considered", "knowledge_hits")
INPUTS_LISTS = ("considered", "unavailable")
DELTA_COUNTS = ("tasks_opened", "tasks_completed", "stalls_appeared", "stalls_cleared",
                "issues_new", "messages_new", "held_pending")
ID_RE = re.compile(r"^ck_[0-9a-f]{32}$")     # the one spelling: plane_mint_id ck
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")   # a projects.yaml key


class ContractError(ValueError):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _count(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _str_list(v) -> bool:
    return (isinstance(v, list) and len(v) <= LIST_MAX
            and all(isinstance(s, str) and len(s) <= ITEM_MAX for s in v))


def _text(v) -> bool:
    return isinstance(v, str) and v.strip() != "" and len(v) <= TEXT_MAX


def normalize(obj, *, checkin_id: str | None = None) -> dict:
    """Return the schema-1 record, or raise ContractError listing EVERY defect."""
    if not isinstance(obj, dict):
        raise ContractError(["decision must be a JSON object"])
    bad: list[str] = []
    out: dict = {"schema": SCHEMA}

    cid = checkin_id or obj.get("checkin_id")
    if not cid:
        bad.append("checkin_id required (the door mints it: plane_mint_id ck)")
    elif not ID_RE.match(str(cid)):
        bad.append("checkin_id must be ck_<32 hex>")
    out["checkin_id"] = cid
    if "prev_checkin_id" not in obj:
        bad.append("prev_checkin_id required (null = READ 0 answered: no previous check-in)")
    prev = obj.get("prev_checkin_id")
    if prev is not None and not ID_RE.match(str(prev)):
        bad.append("prev_checkin_id must be ck_<32 hex> or null")
    out["prev_checkin_id"] = prev

    seen = obj.get("inputs_seen")
    if not isinstance(seen, dict):
        bad.append("inputs_seen must be an object")
        seen = {}
    out["inputs_seen"] = {}
    for k in INPUTS_COUNTS:
        if k not in seen:
            bad.append(f"inputs_seen.{k} required (a non-negative integer, or null = could not measure)")
            out["inputs_seen"][k] = None
            continue
        v = seen[k]
        if v is not None and not _count(v):
            bad.append(f"inputs_seen.{k} must be a non-negative integer or null (null = could not measure)")
        out["inputs_seen"][k] = v
    for k in INPUTS_LISTS:
        if k not in seen:
            bad.append(f"inputs_seen.{k} required (a list, empty allowed; a missing list is a defect, not an empty one)")
            out["inputs_seen"][k] = []
            continue
        v = seen[k]
        if not _str_list(v):
            bad.append(f"inputs_seen.{k} must be a list of <= {LIST_MAX} strings of <= {ITEM_MAX} chars")
        out["inputs_seen"][k] = v

    delta = obj.get("delta")
    if not isinstance(delta, dict):
        bad.append("delta must be an object")
        delta = {}
    out["delta"] = {}
    for k in DELTA_COUNTS:
        if k not in delta:
            bad.append(f"delta.{k} required (a non-negative integer, or null = could not measure)")
            out["delta"][k] = None
            continue
        v = delta[k]
        if v is not None and not _count(v):
            bad.append(f"delta.{k} must be a non-negative integer or null (null = could not measure)")
        out["delta"][k] = v

    action = obj.get("action")
    if action not in ACTIONS:
        bad.append(f"action must be one of {', '.join(ACTIONS)}")
    out["action"] = action

    pk = obj.get("project_key")
    if pk is not None and not (isinstance(pk, str) and SLUG_RE.match(pk)):
        bad.append("project_key must be a projects.yaml slug or null")
    out["project_key"] = pk
    if action == "dispatch" and pk is None:
        bad.append("action dispatch must name project_key")
    if action == "dispatch" and not out["inputs_seen"]["considered"]:
        bad.append("action dispatch needs inputs_seen.considered non-empty (a selector is judged by what it did NOT pick)")
    seen_n = out["inputs_seen"].get("issues_seen")
    if action == "nothing" and isinstance(seen_n, int) and seen_n > 0 and not out["inputs_seen"]["considered"]:
        bad.append("action nothing with issues_seen > 0 needs inputs_seen.considered non-empty (what was there, and why it was passed over)")

    rationale = obj.get("rationale")
    if not _text(rationale):
        bad.append(f"rationale must be a non-empty string, rationale must be <= {TEXT_MAX} characters")
    out["rationale"] = rationale

    raise_ = obj.get("raise")
    if not isinstance(raise_, dict):
        bad.append("raise must be an object")
        raise_ = {}
    decided = raise_.get("decided", False)
    if not isinstance(decided, bool):
        bad.append("raise.decided must be true or false")
    reason = raise_.get("reason")
    if not _text(reason):
        bad.append(f"raise.reason must be a non-empty string (why it surfaced, or why not), raise.reason must be <= {TEXT_MAX} characters")
    held = raise_.get("held", [])
    if not _str_list(held):
        bad.append(f"raise.held must be a list of <= {LIST_MAX} strings of <= {ITEM_MAX} chars")
    out["raise"] = {"decided": decided, "reason": reason, "held": held}
    if action == "ask" and decided is not True:
        bad.append("action ask requires raise.decided = true (an ask IS a surfacing)")
    if decided is True and action != "ask":
        bad.append("raise.decided true requires action ask (an ask IS the surfacing; --raised counts on it)")
    if "mission" in out["inputs_seen"]["unavailable"] and out["inputs_seen"]["issues_considered"] is not None:
        bad.append("issues_considered must be null when mission is unavailable (a count filtered by a mission nobody read is fabricated)")
    if "checkins" in out["inputs_seen"]["unavailable"] and out.get("prev_checkin_id") is not None:
        bad.append("prev_checkin_id must be null when checkins is unavailable (a previous id nobody read is fabricated)")

    if bad:
        raise ContractError(bad)
    return out


def main(argv: list[str]) -> int:
    checkin_id = None
    if len(argv) == 3 and argv[1] == "--checkin-id":
        checkin_id = argv[2]
    elif len(argv) != 1:
        print("usage: checkin-contract.py [--checkin-id ck_<32hex>] < decision.json", file=sys.stderr)
        return 2
    try:
        obj = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"checkin-contract: not JSON: {exc}", file=sys.stderr)
        return 2
    try:
        out = normalize(obj, checkin_id=checkin_id)
    except ContractError as exc:
        for r in exc.reasons:
            print(f"checkin-contract: {r}", file=sys.stderr)
        return 2
    json.dump(out, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

- [ ] **Step 5: Run the contract tests**

Run: `./.venv/bin/pytest tests/test_checkin_contract.py tests/test_plane_system_events.py -q`
Expected: all pass.

- [ ] **Step 6: Write the failing record-door tests**

```python
# tests/test_checkin_doors.py
"""The check-in's write doors (spec §7): checkin-record.sh (Task 1), and
dispatch-task.sh --project / --checkin plus the tg-post.sh alias (Task 2).
Two rigs: a STUB lib-common that captures the batch (tests/test_briefing_trigger.py's
shape), and the REAL shim landing rows in a scratch plane through the cold CLI
rung (tests/test_task_id_dispatch.py's _fake_lib / plane_env). Only the real rig
carries lib-common's ERR trap, so the refusal case runs there too."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from tests.conftest import constructed_env
from tests.plane_fixtures import plane_root
from tests.plane_fixtures import ro as _ro

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "lib"
CLI = Path(sys.executable).parent / "claudlobby"

STUB_LIB_COMMON = """\
#!/bin/bash
install_error_trap() { :; }
trap 'true' EXIT
plane_armed() { [ "${PLANE_EMIT_DISABLED:-0}" != "1" ]; }
plane_mint_id() { printf '%s_%s' "$1" "${STUB_MINT_HEX:-0123456789abcdef0123456789abcdef}"; }
safe_mktemp() { mktemp "${TMPDIR:-/tmp}/ck-stub.XXXXXXXX"; }
json_escape() { printf '%s' "$1" | python3 -c 'import json,sys; sys.stdout.write(json.dumps(sys.stdin.read())[1:-1])'; }
show_help() { awk 'NR == 1 { next } /^[^#]/ { exit } { sub(/^# ?/, ""); print }' "$1"; }
plane_emit_events() { cat > "$EMIT_CAPTURE"; PLANE_EMIT_LAST_RC="${STUB_EMIT_RC:-0}"; }
PLANE_EMIT_LAST_RC=0
"""
STUB_CK = "ck_0123456789abcdef0123456789abcdef"


def _decision() -> dict:
    return {"prev_checkin_id": None,
            "inputs_seen": {"open_tasks": 1, "stalls": 0, "unacked": 0, "issues_seen": None,
                            "issues_considered": 0, "knowledge_hits": 0, "considered": [], "unavailable": ["gh"]},
            "delta": {"tasks_opened": 0, "tasks_completed": 0, "stalls_appeared": 0, "stalls_cleared": 0,
                      "issues_new": 0, "messages_new": None, "held_pending": 0},
            "action": "nothing", "project_key": None,
            "rationale": "nothing worth starting",
            "raise": {"decided": False, "reason": "no delta", "held": []}}


def _stub_rig(tmp_path: Path) -> tuple[Path, dict]:
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "lib-common.sh").write_text(STUB_LIB_COMMON)
    for name in ("checkin-record.sh", "checkin-contract.py"):
        shutil.copy(LIB / name, lib / name)
        (lib / name).chmod(0o755)
    env = {"EMIT_CAPTURE": str(tmp_path / "emit.json"), "FLEET_NAME": "f", "BOT_ID": "mgr",
           "TMPDIR": str(tmp_path), "PATH": os.environ["PATH"]}
    return lib, env


def _run(lib: Path, script: str, env: dict, stdin: str = "", *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(lib / script), *args], input=stdin, capture_output=True,
                          text=True, env=constructed_env(**env), timeout=60)


def _captured(env: dict) -> dict:
    return json.loads(Path(env["EMIT_CAPTURE"]).read_text())


# --- checkin-record.sh, stub transport ----------------------------------------

def test_record_lands_one_actor_anchored_decision_with_the_shared_mint(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", env, json.dumps(_decision()))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == STUB_CK                   # minted by plane_mint_id ck, never a private mint
    (e,) = _captured(env)["events"]
    assert e["event_type"] == "system" and e["emitter"] == "checkin-record"
    assert e["source_ref"] == f"checkin:{STUB_CK}"
    assert e["payload"]["event"] == "checkin_decision"
    assert e["payload"]["subject_kind"] == "actor" and e["payload"]["subject"] == "bot:f/mgr"
    assert e["payload"]["data"]["checkin_id"] == STUB_CK and e["payload"]["data"]["action"] == "nothing"
    assert e["payload"]["data"]["inputs_seen"]["issues_seen"] is None      # could not measure, kept null


def test_record_prefers_bot_id_over_bot_name(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", {**env, "BOT_NAME": "Display Name"}, json.dumps(_decision()))
    assert r.returncode == 0, r.stderr
    assert _captured(env)["events"][0]["payload"]["subject"] == "bot:f/mgr"


def test_record_refuses_a_malformed_decision_at_rc_2_and_records_nothing(tmp_path):
    lib, env = _stub_rig(tmp_path)
    bad = _decision(); bad["action"] = "coffee"
    r = _run(lib, "checkin-record.sh", env, json.dumps(bad))
    assert r.returncode == 2
    assert "action must be one of" in r.stderr and "nothing recorded" in r.stderr
    assert not Path(env["EMIT_CAPTURE"]).exists()


def test_record_says_rc_3_when_the_plane_did_not_record(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", {**env, "STUB_EMIT_RC": "5"}, json.dumps(_decision()))
    assert r.returncode == 3 and "NOT recorded" in r.stderr and r.stdout == ""


def test_record_silenced_only_by_the_harness_exemption_is_rc_3(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", {**env, "PLANE_EMIT_DISABLED": "1"}, json.dumps(_decision()))
    assert r.returncode == 3 and "PLANE_EMIT_DISABLED" in r.stderr
    assert not Path(env["EMIT_CAPTURE"]).exists()


def test_record_needs_an_identity_rc_1(tmp_path):
    lib, env = _stub_rig(tmp_path)
    env = {k: v for k, v in env.items() if k not in ("FLEET_NAME", "BOT_ID")}
    r = _run(lib, "checkin-record.sh", env, json.dumps(_decision()))
    assert r.returncode == 1 and "identity" in r.stderr


def test_record_unknown_flag_is_rc_1(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", env, json.dumps(_decision()), "--bot", "x")
    assert r.returncode == 1 and "unknown flag" in r.stderr and r.stdout == ""


def test_record_dry_run_validates_prefixes_the_id_and_writes_nothing(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", env, json.dumps(_decision()), "--dry-run")
    assert r.returncode == 0 and r.stdout.strip() == f"DRY-RUN {STUB_CK}"   # never mistakable for a recorded id
    assert not Path(env["EMIT_CAPTURE"]).exists()
    bad = _decision(); bad["action"] = "coffee"
    r = _run(lib, "checkin-record.sh", env, json.dumps(bad), "--dry-run")
    assert r.returncode == 2 and r.stdout == ""                             # validation IS the dry run's point


def test_record_help_prints_the_whole_header(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", env, "", "--help")
    assert r.returncode == 0 and "exit:" in r.stdout and "3 the plane could not record" in r.stdout


# --- the REAL spine: the row lands in a scratch plane -----------------------------

REAL_DOOR_FILES = ("checkin-record.sh", "checkin-contract.py", "lib-common.sh",
                   "plane-emit.sh", "plane-socket-client.py")


def _real_rig(tmp_path: Path) -> tuple[Path, dict]:
    root = plane_root(tmp_path)
    lib = root / "lib"
    lib.mkdir()
    for name in REAL_DOOR_FILES:
        (lib / name).symlink_to(LIB / name)
    env = {"CLAUDLOBBY_ROOT": str(root), "FLEET_NAME": "f", "BOT_ID": "mgr",
           "PLANE_EMIT_CLI": str(CLI), "PLANE_SOCKET": str(root / "no-daemon.sock"),
           "HOME": str(root), "PATH": os.environ["PATH"]}
    return lib, env


def _rows(root: Path) -> list[tuple]:
    if not (root / "state" / "plane" / "plane.db").exists():
        return []
    with _ro(root) as conn:
        return [tuple(r) for r in conn.execute("SELECT kind, event, severity FROM events")]


def test_the_decision_lands_on_a_real_plane(tmp_path):
    lib, env = _real_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", env, json.dumps(_decision()))
    assert r.returncode == 0, r.stderr
    ck = r.stdout.strip()
    assert ck.startswith("ck_") and len(ck) == 35
    with _ro(Path(env["CLAUDLOBBY_ROOT"])) as conn:
        row = conn.execute(
            "SELECT severity, source_ref, subject_alias, detail, detail_truncated FROM events"
            " WHERE kind='system' AND event='checkin_decision'").fetchone()
    assert row is not None
    assert row["severity"] == "notice" and row["source_ref"] == f"checkin:{ck}"
    assert row["subject_alias"] == "bot:f/mgr" and row["detail_truncated"] == 0
    assert json.loads(row["detail"])["action"] == "nothing"
    assert _rows(Path(env["CLAUDLOBBY_ROOT"])) == [("system", "checkin_decision", "notice")]   # and nothing else


def test_a_refused_decision_leaves_no_row_at_all_on_a_real_plane(tmp_path):
    # cycle-3 B2: under the REAL lib-common the ERR trap fires inside a command
    # substitution and lands a critical script_error while the door says "nothing
    # recorded"; the door runs the contract as a form-D pipeline so nothing fires
    lib, env = _real_rig(tmp_path)
    bad = _decision(); bad["action"] = "coffee"
    r = _run(lib, "checkin-record.sh", env, json.dumps(bad))
    assert r.returncode == 2 and "nothing recorded" in r.stderr and r.stdout == ""
    assert _rows(Path(env["CLAUDLOBBY_ROOT"])) == []
```

- [ ] **Step 7: Run them to verify they fail**

Run: `./.venv/bin/pytest tests/test_checkin_doors.py -q` (unsandboxed)
Expected: every test fails on the missing `lib/checkin-record.sh`.

- [ ] **Step 8: The record door**

```bash
#!/bin/bash
# checkin-record.sh -- THE write door for a manager check-in decision
# (manager check-in spec §7). The skill hands it the decision JSON on stdin;
# it mints the checkin_id (plane_mint_id ck -- the one mint every door uses),
# validates the schema-1 contract (checkin-contract.py), and lands ONE
# actor-anchored system event `checkin_decision`, stamped source_ref
# checkin:<checkin_id> -- the task-recheck stamp idiom: the ref is the address
# a reader joins on.
#
# Usage: checkin-record.sh [--dry-run] < decision.json
#   stdout: the checkin_id (one line); DRY-RUN <checkin_id> under --dry-run
#   exit:   0 recorded (committed or spooled), or validated under --dry-run
#           1 usage (unknown flag; no identity)
#           2 contract refused (every reason on stderr; nothing recorded)
#           3 the plane could not record (disclosed; nothing recorded)
#
# Identity is BOT_ID + FLEET_NAME from the environment the manager session
# sources from bot.conf -- never BOT_NAME (a display field), and no flags: no
# caller supplies them. RECORD BEFORE ACT is the skill's rule, so for THIS door
# the record IS the action: an unrecorded decision is a failure it says so
# about (rc 3), never a silent 0. The plane is always on; PLANE_EMIT_DISABLED=1
# (the harness exemption) is the one silencer, also rc 3.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

DRY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1; shift ;;
        -h|--help) show_help "$0"; exit 0 ;;
        *) printf 'checkin-record: unknown flag %s (try --help; the decision rides stdin, never a flag)\n' "$1" >&2; exit 1 ;;
    esac
done
BOT="${BOT_ID:-}"
FLEET="${FLEET_NAME:-${CLAUDLOBBY_FLEET:-}}"
if [ -z "$BOT" ] || [ -z "$FLEET" ]; then
    printf 'checkin-record: no identity -- BOT_ID and FLEET_NAME must be set (a manager session sources them from bot.conf)\n' >&2
    exit 1
fi

checkin_id=$(plane_mint_id ck)
raw=$(cat)
tmp=$(safe_mktemp)
# The contract runs as a top-level pipeline into a file, never inside a
# command substitution: install_error_trap arms an ERR trap that bash fires
# INSIDE a substitution whatever the surrounding if/||/set +e (measured), and
# that trap lands a critical script_error row -- a refused decision must
# record NOTHING. A failing pipeline as an if-condition fires no trap.
if printf '%s' "$raw" | python3 "$LIB_DIR/checkin-contract.py" --checkin-id "$checkin_id" > "$tmp"; then
    normalized=$(cat "$tmp")
    rm -f "$tmp"
else
    rm -f "$tmp"
    printf 'checkin-record: decision refused (nothing recorded)\n' >&2
    exit 2
fi

if [ "$DRY" = "1" ]; then
    printf 'DRY-RUN %s\n' "$checkin_id"
    exit 0
fi
if ! plane_armed checkin-record; then
    printf 'checkin-record: plane silenced (PLANE_EMIT_DISABLED=1) -- decision %s NOT recorded\n' "$checkin_id" >&2
    exit 3
fi
utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
safe_fleet=$(json_escape "$FLEET")
printf -v batch '{"events":[{"event_type":"system","emitter":"checkin-record","source_ref":"checkin:%s","fleet":"%s","occurred_at":"%s","payload":{"event":"checkin_decision","subject_kind":"actor","subject":"bot:%s/%s","data":%s}}]}' \
    "$checkin_id" "$safe_fleet" "$utc" "$safe_fleet" "$(json_escape "$BOT")" "$normalized"
plane_emit_events checkin-record <<<"$batch"
if [ "${PLANE_EMIT_LAST_RC:-0}" -ne 0 ]; then
    printf 'checkin-record: plane record failed rc=%s -- decision %s NOT recorded (for THIS door the record is the action; the door-action-unaffected line above does not apply; claudlobby plane doctor says why, claudlobby plane spool drains what was spooled)\n' "$PLANE_EMIT_LAST_RC" "$checkin_id" >&2
    exit 3
fi
printf '%s\n' "$checkin_id"
```

`chmod 0755 lib/checkin-record.sh lib/checkin-contract.py`. The header is one contiguous `#` block from line 2 (`show_help` prints from line 2 to the first non-`#` line). `safe_mktemp` is lib-common's (`:339`).

- [ ] **Step 9: Run the door tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_checkin_doors.py tests/test_bash_parse.py -q` (unsandboxed)
Expected: all pass. `test_the_decision_lands_on_a_real_plane` proves the shim's cold rung records; its "daemon unavailable — falling back to cold CLI" stderr line is expected. `test_a_refused_decision_leaves_no_row_at_all_on_a_real_plane` is the trap pin: it fails on any command-substitution form of the contract call.

- [ ] **Step 10: Commit**

```bash
git add lib/checkin-contract.py lib/checkin-record.sh claudlobby/plane/registries.py tests/test_checkin_contract.py tests/test_checkin_doors.py
printf '%s\n' 'feat(checkin): the decision record — schema-1 contract and the record door' '' 'lib/checkin-contract.py (stdlib) refuses a malformed decision with every reason;' 'the door mints the id via plane_mint_id, the contract only validates it. The' 'record carries the losers (inputs_seen.considered, non-empty on dispatch — a' 'selector is judged by what it did not pick, #974), the raw backlog count beside' 'the filtered one, null counts (could not measure; a missing count is a defect),' 'a required prev_checkin_id and a capped, required raise.reason.' 'lib/checkin-record.sh lands ONE actor-anchored checkin_decision (BOT_ID alias,' 'source_ref checkin:<id>), task-act rc ladder; the contract runs as a pipeline' 'into a file so a refusal fires no ERR trap and lands no row. Spec §7.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c1.txt"
git commit -q -F "$TMPDIR/c1.txt" && git log --oneline -1
```

---

### Task 2: `dispatch-task.sh --project` and `--checkin`; the `tg-post.sh` alias; the envelope docs

**Files:**
- Modify: `lib/dispatch-task.sh:5-24` (flag docs), `:78-82` (init), `:104-117` (parse, + the lookup after the guards), `:350-354` (the envelope gate), `:395-405` (envelope), `:660-667` (after the `emit_triple` gate — the join block), `:681-689` (the batch); `lib/plane-lookup.py:176-235` (`main`: the `--checkin-id` mode); `lib/tg-post.sh:64, 81`
- Test: `tests/test_checkin_doors.py` (append the Task 2 half)

**Interfaces:**
- Consumes: Task 1 step 3's severity line (the join test asserts `severity == "notice"`), `_flag_val` (`dispatch-task.sh:94-95`), `safe_sender` (`:585-586`), `emit_triple` (`:660-663`), `sup_ev` (`:642`), `json_escape`.
- Produces: `--project KEY` (slug; `| project:KEY` in the envelope; opens the envelope gate; `project_key` on the work item — the fix for the measured 0/374). `--checkin ck_<32hex>`: one `system` event `checkin_dispatch` appended to the dispatch batch AFTER the `emit_triple` gate (`:660-663`) — `source_ref` = the dispatch ref, actor = `safe_sender` (the same alias the batch writes as `assigned_by`), `data = {checkin_id, assignment_id, work_item_id, task_id}` where `task_id` is **null** on an id-less dispatch (a flagless `task` send is tracked — measured this cycle: one assignment, `dispatch-log:sha:` ref — but mints no legacy id, cycle-3 gap) — atomic with the assignment. On an untracked dispatch (a control type, or no ref) it is **disclosed** on stderr, never silently dropped. **The id is looked up, not only shape-checked** (cycle-4 B5): its only producer is the manager transcribing 32 hex characters between two tool calls, so `dispatch-task.sh` asks `plane-lookup.py --checkin-id` whether a `checkin_decision` carries it and DISCLOSES when none does — the `--supersedes` posture (`:630-645`: resolve, then say so), never a refusal, because a record the shim spooled is legitimately absent from the db at rc 0 and a refusal would block the ACT on a transport state. `tg-post.sh` writes `bot:$FLEET_NAME/${BOT_ID:-$BOT_NAME}`: the id where a session exports one, the name where a hand caller sets only that (`bot-sweep-cron.sh:24`) — under `set -u` (`tg-post.sh:13`) a bare `$BOT_ID` would be an unbound-variable fault for those callers (cycle-3 R3). `checkin_dispatch`'s reader is chunk 3's outcome join (`claudlobby events` prefix-filters `fleet-events:`); the real-plane test below proves the row lands. `--project` writes two things for two readers: `project:<key>` in the envelope is for the receiving worker (the documented `[BOTCOMMAND]` field table, `dispatch.md`), `project_key` on the work item is for chunk 3's outcome join and the plane UI.

- [ ] **Step 1: Append the failing tests**

```python
# append to tests/test_checkin_doors.py

# --- dispatch-task.sh --project / --checkin; tg-post.sh alias (Task 2) ----------

from tests.test_task_id_dispatch import _bash, _fake_lib

DISPATCH_STUB = "#!/bin/bash\nprintf '%s\\n' \"$2\" > \"$DISPATCH_CAPTURE\"\nexit 0\n"
CK = "ck_" + "a" * 32


def test_dispatch_project_alone_opens_the_envelope_and_stamps_the_work_item(tmp_path):
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB)
    env["DISPATCH_CAPTURE"] = str(tmp_path / "sent.txt")
    r = _bash(f'"{libdir}/dispatch-task.sh" --project shop w1 "fix the feed"', env=env)
    assert r.returncode == 0, r.stderr
    sent = (tmp_path / "sent.txt").read_text()
    assert sent.startswith("[BOTCOMMAND]") and "| project:shop" in sent     # --project ALONE opens the gate
    with _ro(tmp_path) as conn:
        row = conn.execute("SELECT project_key FROM work_items").fetchone()
    assert row["project_key"] == "shop"


def test_dispatch_refuses_a_non_slug_project(tmp_path):
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB)
    r = _bash(f'"{libdir}/dispatch-task.sh" --project "Not Slug" w1 "x"', env=env)
    assert r.returncode == 1 and "project" in r.stderr


def test_dispatch_checkin_appends_the_join_row_to_the_same_batch(tmp_path):
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB)
    env["DISPATCH_CAPTURE"] = str(tmp_path / "sent.txt")
    r = _bash(f'"{libdir}/dispatch-task.sh" --project shop --checkin {CK} w1 "fix the feed"', env=env)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "sent.txt").read_text().startswith("[BOTCOMMAND]")   # the send happened
    with _ro(tmp_path) as conn:
        asg = conn.execute("SELECT assignment_id, work_item_id, source_ref, assigned_by_uid FROM assignments").fetchone()
        link = conn.execute("SELECT detail, subject_uid, source_ref, severity FROM events"
                            " WHERE kind='system' AND event='checkin_dispatch'").fetchone()
    assert asg is not None and link is not None
    d = json.loads(link["detail"])
    assert d["checkin_id"] == CK and d["assignment_id"] == asg["assignment_id"]
    assert d["work_item_id"] == asg["work_item_id"] and d["task_id"].startswith("t-")
    assert link["source_ref"] == asg["source_ref"] and link["severity"] == "notice"
    assert link["subject_uid"] == asg["assigned_by_uid"]      # the dispatcher, by the plane's own alias rule


def test_dispatch_checkin_on_an_id_less_task_dispatch_records_task_id_null(tmp_path):
    # a flagless task send is TRACKED (#1491: the gate is the type) but mints no
    # legacy id, so the join carries task_id null -- never "" (cycle-3 gap)
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB)
    env["DISPATCH_CAPTURE"] = str(tmp_path / "sent.txt")
    r = _bash(f'"{libdir}/dispatch-task.sh" --checkin {CK} w1 "fix the feed"', env=env)
    assert r.returncode == 0, r.stderr
    with _ro(tmp_path) as conn:
        asg = conn.execute("SELECT source_ref FROM assignments").fetchone()
        link = conn.execute("SELECT detail FROM events WHERE kind='system' AND event='checkin_dispatch'").fetchone()
    assert asg is not None and asg["source_ref"].startswith("dispatch-log:sha:")
    assert link is not None and json.loads(link["detail"])["task_id"] is None


def test_dispatch_checkin_on_an_untracked_dispatch_is_disclosed_not_dropped(tmp_path):
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB)
    env["DISPATCH_CAPTURE"] = str(tmp_path / "sent.txt")
    r = _bash(f'"{libdir}/dispatch-task.sh" --type query --checkin {CK} w1 "where are you?"', env=env)
    assert r.returncode == 0, r.stderr
    assert "--checkin ignored" in r.stderr and "query" in r.stderr
    with _ro(tmp_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE event='checkin_dispatch'").fetchone()[0] == 0


def test_dispatch_refuses_a_malformed_checkin_id(tmp_path):
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB)
    r = _bash(f'"{libdir}/dispatch-task.sh" --checkin nope w1 "x"', env=env)
    assert r.returncode == 1 and "--checkin" in r.stderr


def test_dispatch_checkin_without_a_value_is_rc_1_never_0(tmp_path):
    # flag FIRST and alone: the parse loop stops at the first positional
    # (dispatch-task.sh:115), so a trailing flag would be task text. Through
    # _fake_lib the REAL lib-common (and its EXIT trap) is in play.
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB)
    r = _bash(f'"{libdir}/dispatch-task.sh" --checkin', env=env)
    assert r.returncode == 1 and "--checkin needs a value" in r.stderr and r.stdout == ""


def test_plane_lookup_answers_a_checkin_id(tmp_path):
    from claudlobby.plane.emit_api import emit_batch
    from tests.test_task_id_dispatch import plane_env
    plane_env(tmp_path)
    emit_batch(tmp_path, [{"event_type": "system", "emitter": "t", "fleet": "f", "source_ref": f"checkin:{CK}",
                           "payload": {"event": "checkin_decision", "subject_kind": "actor", "subject": "bot:f/mgr", "data": {"schema": 1}}}])
    look = [sys.executable, "-S", "-E", str(LIB / "plane-lookup.py"), "--root", str(tmp_path), "--checkin-id"]
    hit = subprocess.run([*look, CK], capture_output=True, text=True)
    assert hit.returncode == 0 and hit.stdout.strip() == CK, hit.stderr
    miss = subprocess.run([*look, "ck_" + "f" * 32], capture_output=True, text=True)
    assert miss.returncode == 0 and miss.stdout == "" and "no checkin_decision" in miss.stderr


def test_dispatch_checkin_to_a_decision_the_plane_cannot_see_is_disclosed_not_refused(tmp_path):
    # the skill hands the id over in the same call (ck=$(record) && dispatch), a hand
    # caller pastes it; a well-formed id can still name nothing (mis-copied by hand, or
    # a record the shim spooled): say so, record the join as given.
    # The plane must EXIST for this to be "cannot see" rather than "cannot answer"
    # (a fresh rig has no db until the first emit), so one unrelated row seeds it.
    from claudlobby.plane.emit_api import emit_batch
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB)
    env["DISPATCH_CAPTURE"] = str(tmp_path / "sent.txt")
    emit_batch(tmp_path, [{"event_type": "system", "emitter": "t", "fleet": "f",
                           "payload": {"event": "report_status", "subject_kind": "actor", "subject": "bot:f/w1", "data": {"status": "progress"}}}])
    r = _bash(f'"{libdir}/dispatch-task.sh" --project shop --checkin {CK} w1 "fix the feed"', env=env)
    assert r.returncode == 0, r.stderr
    assert "names no checkin_decision" in r.stderr
    with _ro(tmp_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE event='checkin_dispatch'").fetchone()[0] == 1


def test_dispatch_checkin_when_the_plane_cannot_answer_says_so_not_absent(tmp_path):
    # unreachable is not empty (source_state): a root whose plane db cannot be
    # opened must not print the "names no checkin_decision" line. The lookup runs
    # in the join block, which sits INSIDE the door's first emit, so on a root with
    # no plane.db it answers "cannot answer" (rc 3), not "cannot see" -- which is
    # why the cannot-see test seeds a row first. The unreachable shape tested here
    # is an UNOPENABLE db (a directory where the db file belongs: sqlite refuses
    # it, rc 3). The lookup is one python3 spawn on the pre-send path (cycle 9);
    # chunk 3 may move it below the send if the measured cost warrants.
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB)
    env["DISPATCH_CAPTURE"] = str(tmp_path / "sent.txt")
    broken = tmp_path / "broken"
    (broken / "state" / "plane" / "plane.db").mkdir(parents=True)
    env["CLAUDLOBBY_ROOT"] = str(broken)
    r = _bash(f'"{libdir}/dispatch-task.sh" --project shop --checkin {CK} w1 "fix the feed"', env=env)
    assert r.returncode == 0, r.stderr                    # the door never blocks the send on a plane fault
    assert "the plane could not answer" in r.stderr and "names no checkin_decision" not in r.stderr


def test_dispatch_checkin_to_a_recorded_decision_is_quiet(tmp_path):
    from claudlobby.plane.emit_api import emit_batch
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB)
    env["DISPATCH_CAPTURE"] = str(tmp_path / "sent.txt")
    emit_batch(tmp_path, [{"event_type": "system", "emitter": "t", "fleet": "f", "source_ref": f"checkin:{CK}",
                           "payload": {"event": "checkin_decision", "subject_kind": "actor", "subject": "bot:f/lead", "data": {"schema": 1}}}])
    r = _bash(f'"{libdir}/dispatch-task.sh" --project shop --checkin {CK} w1 "fix the feed"', env=env)
    assert r.returncode == 0, r.stderr
    assert "names no checkin_decision" not in r.stderr


def test_tg_post_anchors_the_sender_on_bot_id_with_the_hand_caller_fallback():
    text = (LIB / "tg-post.sh").read_text()
    assert 'bot:$FLEET_NAME/${BOT_ID:-$BOT_NAME}' in text        # a session has BOT_ID; bot-sweep-cron.sh sets only BOT_NAME
    assert 'bot:$FLEET_NAME/$BOT_NAME"' not in text
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/pytest tests/test_checkin_doors.py -q -k "dispatch or tg_post"` (unsandboxed)
Expected: 12 failed — `unknown flag '--project'` / `'--checkin'`, `plane-lookup.py: error: unrecognized arguments: --checkin-id`, and the alias assertion.

- [ ] **Step 3: The flags**

(a) Flag docs, after the `--ref URL` line (`:10`):
```bash
#   --project KEY      projects.yaml project (adds project:<KEY> to the envelope and
#                      project_key to the plane work item -- the well-defined bar)
#   --checkin ID       The check-in decision this dispatch acts on (ck_<32hex>):
#                      appends a checkin_dispatch join row to the plane batch, atomic
#                      with the assignment. Disclosed and ignored on an untracked
#                      dispatch (a control type mints no assignment to join).
```
(b) Init beside `DISPATCH_REPO=""` (`:78`): `DISPATCH_PROJECT=""` and `DISPATCH_CHECKIN=""`.
(c) Parse loop (`:104-117`), beside `--repo`:
```bash
        --project)      DISPATCH_PROJECT=$(_flag_val "$1" "${2:-}"); shift 2 ;;
        --checkin)      DISPATCH_CHECKIN=$(_flag_val "$1" "${2:-}"); shift 2 ;;
```
and directly after the loop:
```bash
if [ -n "$DISPATCH_PROJECT" ] && ! printf '%s' "$DISPATCH_PROJECT" | grep -Eq '^[a-z][a-z0-9-]*$'; then
    echo "dispatch-task: --project must be a projects.yaml slug ([a-z][a-z0-9-]*), got '$DISPATCH_PROJECT'" >&2; exit 1
fi
if [ -n "$DISPATCH_CHECKIN" ] && ! printf '%s' "$DISPATCH_CHECKIN" | grep -Eq '^ck_[0-9a-f]{32}$'; then
    echo "dispatch-task: --checkin must be a check-in id (ck_<32hex>), got '$DISPATCH_CHECKIN'" >&2; exit 1
fi
```
(The id LOOKUP does not live here: it runs in the join block (f), beside the `--supersedes` lookup, because the door sets its own `CLAUDLOBBY_ROOT` default at `:450` — a lookup placed at the parse loop ran before that default and reported "could not answer" for every hand caller; cycle-6 R5.)
(c2) `lib/plane-lookup.py`: the mode, beside `_by_assignment` (a `fn(pr, conn)` under `_with_plane`, the file's own ladder), and its flag in `main()`:
```python
def _checkin_id(a) -> int:
    """`--checkin-id ck_<32hex>`: print the id when a `checkin_decision` system
    event carries `source_ref = checkin:<id>` (manager check-in spec §7), else
    nothing plus a stderr note -- the `--task-id` contract: a stamped id is not
    proof the row exists, the caller says so and carries on. Unreachable = rc 3."""
    def fn(pr, conn):
        row = conn.execute(
            "SELECT 1 FROM events WHERE kind = 'system' AND event = 'checkin_decision'"
            " AND source_ref = ? LIMIT 1", (f"checkin:{a.checkin_id}",)).fetchone()
        if row is None:
            print(f"plane-lookup: no checkin_decision with id {a.checkin_id}", file=sys.stderr)
            return 0
        print(a.checkin_id)
        return 0
    return _with_plane(a.root, fn)
```
```python
    ap.add_argument("--checkin-id", default=None,
                    help="print the id when a checkin_decision carries source_ref checkin:<id>, else"
                    " nothing + a note (dispatch-task.sh --checkin asks before it joins)")
```
and, after the `--root` empty check in `main()`: `if a.checkin_id: return _checkin_id(a)`.
(d) **The envelope gate** (`:352-353`) — the project opens it, or a `--project`-only dispatch sends freeform:
```bash
if [ -n "$FORCE_ENVELOPE" ] || [ -n "$DISPATCH_REPO" ] || [ -n "$DISPATCH_PRIORITY" ] \
   || [ -n "$DISPATCH_REF" ] || [ -n "$DISPATCH_WORKSTREAM" ] || [ -n "$DISPATCH_PROJECT" ]; then
```
(e) Envelope (`:395-400`), after the `repo:` line:
```bash
    [ -n "$DISPATCH_PROJECT" ]    && DISPATCH_MSG="$DISPATCH_MSG | project:$DISPATCH_PROJECT"
```
(f) **The join block — AFTER the `emit_triple` gate, beside `link_frag`** (`:660-667`; cycle-2 B1: placed before `:660`, `emit_triple` is unbound under `set -u` and the whole dispatch is lost). Replace the two lines `local link_frag="" ws_frag="" repo_frag="" deadline_frag="" iso_deadline=""` / `if [ -n "$emit_triple" ]; then … fi` with:
```bash
    local link_frag="" ws_frag="" repo_frag="" proj_frag="" deadline_frag="" iso_deadline="" ck_ev="" ck_tid=""
    if [ -n "$emit_triple" ]; then
        link_frag="\"work_item_id\":\"$PLANE_WI_ID\",\"assignment_id\":\"$PLANE_ASG_ID\","
    fi
    # The check-in join (spec §7): the decision row was recorded BEFORE this
    # dispatch (RECORD before ACT), and the Assignment payload is strict, so
    # the link is its own system event in the SAME batch -- the supersede
    # precedent below. Only a tracked dispatch has an assignment to join; an
    # untracked one says so and drops the flag -- unlike --supersedes, which
    # STILL rides the untracked path (the retire is its point): there is no
    # assignment event here to point at. A tracked but id-less send (no
    # envelope flag) carries task_id null, never "".
    if [ -n "$DISPATCH_CHECKIN" ]; then
        if [ -n "$emit_triple" ]; then
            # The skill captures the id into a variable and dispatches in the SAME
            # call (record && dispatch), but a hand caller pastes it, so a well-formed
            # id can still name no decision. Looked up like --supersedes and DISCLOSED,
            # never refused: a record the shim spooled is legitimately absent from
            # the db at rc 0, and a refusal would block the ACT on a transport
            # state. Form D (a top-level if over the command, output to a file):
            # rc 3 means the plane could not answer at all, and UNREACHABLE must
            # not be reported as ABSENT -- the source_state rule -- two notes.
            local _ck_tmp
            _ck_tmp=$(safe_mktemp)
            if python3 -S -E "$LIB_DIR/plane-lookup.py" --root "${CLAUDLOBBY_ROOT:-}" --checkin-id "$DISPATCH_CHECKIN" > "$_ck_tmp" 2>/dev/null; then
                if [ ! -s "$_ck_tmp" ]; then
                    echo "dispatch-task: --checkin $DISPATCH_CHECKIN names no checkin_decision the plane can see (spooled, or mis-copied from the record door?) -- the join is recorded as given; verify with claudlobby checkins --last" >&2
                fi
            else
                echo "dispatch-task: --checkin $DISPATCH_CHECKIN not verified -- the plane could not answer (no CLAUDLOBBY_ROOT, or the db is unreachable); the join is recorded as given" >&2
            fi
            rm -f "$_ck_tmp"
            ck_tid="null"
            [ -n "$TASK_ID" ] && ck_tid="\"$(json_escape "$TASK_ID")\""
            ck_ev="{\"event_type\":\"system\",\"emitter\":\"dispatch-task\",\"source_ref\":\"$dispatch_ref\",\"fleet\":\"$safe_fleet\",\"payload\":{\"event\":\"checkin_dispatch\",\"subject_kind\":\"actor\",\"subject\":\"$safe_sender\",\"data\":{\"checkin_id\":\"$DISPATCH_CHECKIN\",\"assignment_id\":\"$PLANE_ASG_ID\",\"work_item_id\":\"$PLANE_WI_ID\",\"task_id\":$ck_tid}}}"
        else
            echo "dispatch-task: --checkin ignored: a $DISPATCH_TYPE dispatch mints no assignment to join" >&2
        fi
    fi
```
(`safe_sender` is the json-escaped sender alias `bot:<fleet>/<id>` built at `:585-586`, the same value the batch writes as `assigned_by`. The `[ -n "$TASK_ID" ] && …` list is the file's own idiom at `:629` — a false test in an AND-list neither exits nor fires the ERR trap.)
(g) The batch (`:681-689`): the project fragment beside `repo_frag`, the join beside `sup_ev`:
```bash
        [ -n "$DISPATCH_PROJECT" ] && proj_frag=",\"project_key\":\"$(json_escape "$DISPATCH_PROJECT")\""
        wi_ev="{\"event_type\":\"work_item\",\"emitter\":\"dispatch-task\",\"source_ref\":\"$dispatch_ref\",\"fleet\":\"$safe_fleet\",\"payload\":{\"work_item_id\":\"$PLANE_WI_ID\",\"title\":\"$safe_task\",\"created_by\":\"$safe_sender\"${ws_frag}${repo_frag}${proj_frag}}}"
```
and the `printf -v _batch` line becomes:
```bash
        printf -v _batch '{"events":[%s,%s,%s%s%s]}' "$wi_ev" "$asg_ev" "$comm" "${sup_ev:+,$sup_ev}" "${ck_ev:+,$ck_ev}"
```
(`asg_ev`, `comm` and the `plane_emit_events` line stay as they are.)

- [ ] **Step 4: The alias fix and the envelope docs**

`lib/tg-post.sh:81`: `--arg sender "bot:$FLEET_NAME/$BOT_NAME"` → `--arg sender "bot:$FLEET_NAME/${BOT_ID:-$BOT_NAME}"`; the comment at `:64` says why (`BOT_ID` is the alias every plane door anchors on; `BOT_NAME` is the fallback for hand callers such as `bot-sweep-cron.sh`, which set only that). The `plane_armed --require-bot` gate at `:69` still keys on `BOT_NAME`, which every session exports beside `BOT_ID` — unchanged here, filed for chunk 2 (§Scope).

The `dispatch.md` protocol is NOT edited in this chunk (cycle 9): it is composed into every declaring manager's `CLAUDE.md` (`templates/claude.md.j2:228`; `diff.py:44` compares the recomposition against the runtime file), so its `project:` field-table row and recipe flag land in chunk 2 with the other protocol edit, behind that chunk's restart; until then `--project` is documented in the door's own header and CLAUDE.md's lib table (Task 6).

- [ ] **Step 5: Run the tests to verify they pass — including every suite that drives `tg-post.sh`**

```bash
./.venv/bin/pytest tests/test_checkin_doors.py tests/test_task_id_dispatch.py tests/test_dispatch_type.py tests/test_bash_parse.py \
  tests/test_creds_check_telegram.py tests/test_fleet_pulse_escalated.py tests/test_fleet_pulse_events_plane.py \
  tests/test_maintenance_jobs.py tests/test_notify_behind.py tests/test_plane_gauntlet_doors.py \
  tests/test_plane_events_door.py tests/test_system_defaults.py -q
for t in tests/test_dispatch_task.sh tests/test_tg_post.sh tests/test_alert_recipient.sh tests/test_orphan_browser_reaper.sh tests/test_host_health_check.sh; do
  bash "$t" > "$TMPDIR/$(basename "$t").out" 2>&1; echo "$t rc=$?"
done
```
(unsandboxed) Expected: all pass; every `rc=0`. `test_dispatch_type.py` parses the protocol docs' `[BOTCOMMAND]` TYPE list — untouched by a field-table row. A failure in a `tg-post.sh` suite is a hand caller the fallback missed: read the suite before touching the alias again.

- [ ] **Step 6: Commit**

```bash
git add lib/dispatch-task.sh lib/plane-lookup.py lib/tg-post.sh tests/test_checkin_doors.py
printf '%s\n' 'feat(dispatch-task): --project stamps project_key and opens the envelope; --checkin appends the join row' '' 'project_key on the work item fixes the measured 0/374. The checkin_dispatch' 'system event rides the SAME batch as the assignment, after the emit_triple gate' '(the supersede precedent), task_id null on an id-less tracked send; the id is' 'looked up through plane-lookup.py --checkin-id and disclosed when the plane' 'cannot see it; an untracked dispatch discloses the ignored flag. tg-post.sh anchors its sender on' 'BOT_ID with the BOT_NAME fallback hand callers need under set -u. Spec §7.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c2.txt"
git commit -q -F "$TMPDIR/c2.txt" && git log --oneline -1
```

---

### Task 3: `claudlobby checkins` — the read door

**Files:**
- Modify: `claudlobby/plane/queries.py` (append `CHECKIN_ROWS_SQL`), `claudlobby/commands/_parsers.py:189-197` + its imports
- Create: `claudlobby/commands/checkins.py`
- Test: `tests/test_checkins_cli.py`

**Interfaces:**
- Consumes: `brief.plane_session(paths, fleet) -> (plane, note)` (`brief.py:252`; a context manager whose `conn` yields TUPLES — probed for reachability and closed on purpose), `brief.resolve_fleet_name(paths)` (`:233`), `brief.TEXT_ROW_LIMIT` (`:122`, the shared text cap), `plane.db.open_ro(root) -> (conn | None, reason)` (`db.py:28`, named rows), `_helpers.refuse_unreachable(command, note) -> int` (`:160`, prints `UNREACHABLE`), `queries.fleet_alias_range` / `fleet_range_params` / `_epoch`.
- Produces: `CHECKIN_ROWS_SQL` (binds: fleet, fleet) — the LIGHT form (`subject_alias, occurred_at, detail, detail_truncated, ingest_seq`; the record is parsed once in Python, the `plane-readers.py:886-907` precedent — cycle-3 gap), ordered `occurred_at DESC, ingest_seq DESC`; `cmd_checkins(args) -> int`: `--fleet` (dest `checkins_fleet`), `--bot`, `--since 7d` (the report-back grammar `24h, 7d, 30m, ISO`, parsed inline — not a new helper; cycle-2 gap), `--last` (ignores `--since`), `--raised` (only rows whose `raise.decided` is true — READ 0's ask count, so that count is a bounded read rather than every record in the window; cycle-5 R5), `--json`; rc 0 · 2 no fleet / bad `--since` (the door ladder's usage code; `report-back` says 1 for the same case — deliberate, in the docstring) · 3 plane unreachable, including a fleet the plane has never seen (`plane_session`'s roster rule). Text output shows the losers and the unavailable inputs when present, and caps at `TEXT_ROW_LIMIT` rows with a disclosure line (`--json` is never capped). A row with no `data` (the contract accepts one, `contracts.py:418`) is listed, never a traceback (cycle-3 R8). No `--limit` (chunk 3). `--since` filters in Python over the fleet's rows — intentional through chunk 3, which binds it in SQL with `--summary`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_checkins_cli.py
"""`claudlobby checkins` — the read door (spec §11), minimal form. Seeded through
the real emit spine. The plane session's connection yields tuples (status.py:218);
the door reads named rows through open_ro."""

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claudlobby.commands import checkins as cmd
from claudlobby.plane.emit_api import emit_batch
from tests.plane_fixtures import plane_root

REPO_ROOT = Path(__file__).resolve().parent.parent
F = "ck-fleet"
CK1, CK2, CK3 = ("ck_" + c * 32 for c in "abc")


@pytest.fixture(autouse=True)
def root(tmp_path: Path) -> Path:
    r = plane_root(tmp_path)
    (r / "lib").mkdir()
    for name in ("dispatch-overdue.py", "plane-readers.py", "plane-lookup.py"):
        shutil.copy(REPO_ROOT / "lib" / name, r / "lib" / name)
    return r


class _Args:
    def __init__(self, root, **kw):
        self.root, self.fleet, self.seed = str(root), None, False
        self.checkins_fleet = kw.get("fleet", F)
        self.bot = kw.get("bot")
        self.since = kw.get("since", "7d")
        self.last = kw.get("last", False)
        self.raised = kw.get("raised", False)
        self.json = kw.get("json", False)


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def _record(ck: str, **over) -> dict:
    d = {"schema": 1, "checkin_id": ck, "prev_checkin_id": None,
         "inputs_seen": {"open_tasks": 0, "considered": [], "unavailable": []}, "delta": {},
         "action": "nothing", "project_key": None, "rationale": f"r-{ck[-4:]}",
         "raise": {"decided": False, "reason": "quiet", "held": []}}
    if "raise_" in over:                      # `raise` is a keyword: the tests pass it as raise_
        d["raise"] = over.pop("raise_")
    d.update(over)
    return d


def _decision(root, bot: str, ck: str, *, age_h: float, **over):
    emit_batch(root, [{
        "event_type": "system", "emitter": "checkin-record", "fleet": F,
        "source_ref": f"checkin:{ck}", "occurred_at": _ago(hours=age_h),
        "payload": {"event": "checkin_decision", "subject_kind": "actor",
                    "subject": f"bot:{F}/{bot}", "data": _record(ck, **over)}}])


def _out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_rows_are_newest_first_by_occurred_at_scoped_to_fleet_and_bot(root, capsys):
    _decision(root, "mgr", CK1, age_h=5)          # arrival order CK1, CK2, CK3;
    _decision(root, "mgr", CK2, age_h=1, prev_checkin_id=CK1, action="dispatch", project_key="shop")
    _decision(root, "other", CK3, age_h=2)        # occurred order CK2 (1h), CK3 (2h), CK1 (5h)
    assert cmd.cmd_checkins(_Args(root, json=True)) == 0
    rows = _out(capsys)["checkins"]
    assert [r["checkin_id"] for r in rows] == [CK2, CK3, CK1]
    assert rows[0]["prev_checkin_id"] == CK1 and rows[0]["action"] == "dispatch" and rows[0]["bot"] == "mgr"
    assert rows[0]["raise"] == {"decided": False, "reason": "quiet"}
    assert cmd.cmd_checkins(_Args(root, bot="mgr", json=True)) == 0
    assert [r["checkin_id"] for r in _out(capsys)["checkins"]] == [CK2, CK1]


def test_last_ignores_the_window(root, capsys):
    _decision(root, "mgr", CK1, age_h=24 * 30)    # a manager idle for a month
    assert cmd.cmd_checkins(_Args(root, bot="mgr", last=True, json=True)) == 0
    assert [r["checkin_id"] for r in _out(capsys)["checkins"]] == [CK1]   # the chain is not broken by --since


def test_since_window(root, capsys):
    _decision(root, "mgr", CK1, age_h=30)
    _decision(root, "mgr", CK2, age_h=1)
    _decision(root, "mgr", CK3, age_h=2)
    assert cmd.cmd_checkins(_Args(root, since="24h", json=True)) == 0
    assert [r["checkin_id"] for r in _out(capsys)["checkins"]] == [CK2, CK3]
    assert cmd.cmd_checkins(_Args(root, since="yesterday")) == 2          # usage: the door ladder's code


def test_raised_filters_to_the_asks(root, capsys):
    _decision(root, "mgr", CK1, age_h=1)
    _decision(root, "mgr", CK2, age_h=2, action="ask", raise_={"decided": True, "reason": "a fork", "held": []})
    _decision(root, "mgr", CK3, age_h=3, action="ask", raise_={"decided": True, "reason": "another", "held": []})
    assert cmd.cmd_checkins(_Args(root, raised=True, json=True)) == 0
    assert [r["checkin_id"] for r in _out(capsys)["checkins"]] == [CK2, CK3]      # the ask count READ 0 takes


def test_a_truncated_record_is_listed_and_marked_never_dropped(root, capsys):
    _decision(root, "mgr", CK1, age_h=1, rationale="x" * 20000)       # over the 16 KiB DIAGNOSTIC cap
    assert cmd.cmd_checkins(_Args(root, json=True)) == 0
    rows = _out(capsys)["checkins"]
    assert len(rows) == 1 and rows[0]["truncated"] is True and rows[0]["checkin_id"] is None


def test_a_row_without_a_record_is_listed_not_a_traceback(root, capsys):
    # contracts.py:418 accepts a system event with no data; the reader must not raise
    emit_batch(root, [{"event_type": "system", "emitter": "t", "fleet": F, "source_ref": f"checkin:{CK1}",
                       "payload": {"event": "checkin_decision", "subject_kind": "actor", "subject": f"bot:{F}/mgr"}}])
    assert cmd.cmd_checkins(_Args(root, json=True)) == 0
    rows = _out(capsys)["checkins"]
    assert len(rows) == 1 and rows[0]["record"] is None and rows[0]["checkin_id"] is None and rows[0]["truncated"] is False
    assert cmd.cmd_checkins(_Args(root)) == 0
    assert "carries no record" in capsys.readouterr().out


def test_the_text_listing_shows_the_losers_and_the_unavailable_inputs(root, capsys):
    _decision(root, "mgr", CK1, age_h=1,
              inputs_seen={"open_tasks": 1, "considered": ["#7 docs — not mission work"], "unavailable": ["gh"]})
    assert cmd.cmd_checkins(_Args(root)) == 0
    out = capsys.readouterr().out
    assert "passed over: #7 docs — not mission work" in out and "unavailable: gh" in out


def test_the_text_listing_caps_at_ten_rows_and_says_so(root, capsys):
    for i in range(12):
        _decision(root, "mgr", "ck_" + f"{i:032x}", age_h=i + 1)
    assert cmd.cmd_checkins(_Args(root)) == 0
    out = capsys.readouterr().out
    assert "showing the newest 10 of 12" in out and out.count("surfacing:") == 10
    assert cmd.cmd_checkins(_Args(root, json=True)) == 0
    assert len(_out(capsys)["checkins"]) == 12                            # --json is never capped


def test_an_empty_fleet_plane_answers_no_checkins_at_rc_0(root, capsys):
    # a plane that has SEEN the fleet (one identity row — the roster the session
    # opens on) but holds no decision is EMPTY, not unreachable
    emit_batch(root, [{"event_type": "system", "emitter": "t", "fleet": F,
                       "payload": {"event": "report_status", "subject_kind": "actor",
                                   "subject": f"bot:{F}/w1", "data": {"status": "progress"}}}])
    assert cmd.cmd_checkins(_Args(root)) == 0
    out = capsys.readouterr().out
    assert F in out and "no check-ins" in out


def test_a_fleet_the_plane_has_never_seen_refuses_at_rc_3(root, capsys):
    # plane_session's roster rule (#1014's class), inherited on purpose: a typo'd
    # --fleet must not read as "no rows" (cycle-3 question 2)
    _decision(root, "mgr", CK1, age_h=1)
    assert cmd.cmd_checkins(_Args(root, fleet="never-seen")) == 3
    assert "UNREACHABLE" in capsys.readouterr().err


def test_unreachable_plane_refuses_at_rc_3(tmp_path, capsys):
    bare = tmp_path / "bare"
    bare.mkdir()
    assert cmd.cmd_checkins(_Args(bare)) == 3
    assert "UNREACHABLE" in capsys.readouterr().err       # refuse_unreachable's own token, upper-case
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/pytest tests/test_checkins_cli.py -q`
Expected: `ImportError: cannot import name 'checkins'`.

- [ ] **Step 3: The query**

Append to `claudlobby/plane/queries.py`:

```python

# --- the manager check-in (manager check-in spec §7, §11) ---------------------
# A check-in is ONE system event, `checkin_decision`, actor-anchored on the
# manager, stamped source_ref checkin:<checkin_id>; the record is its detail
# (schema 1). Ordered by WHEN IT HAPPENED (`occurred_at`, the clock every other
# read of this door uses), ingest_seq as the tiebreak -- under the shim's spool
# rung the two clocks diverge, and "the previous check-in" means the one that
# happened last, not the one that landed last. The record is parsed ONCE by the
# reader (plane-readers.py:886-907), never extracted column by column here: a
# truncated detail is not JSON (detail_truncated=1 IS the parse guard) and the
# row is still returned -- the door exists to show every decision, and dropping
# the over-cap ones would hide exactly the records that most need looking at.
# Binds: fleet, fleet.
CHECKIN_ROWS_SQL = (
    "SELECT e.subject_alias AS subject_alias, e.occurred_at AS occurred_at,"
    " e.detail AS detail, e.detail_truncated AS detail_truncated, e.ingest_seq AS ingest_seq"
    " FROM events e"
    " WHERE e.kind = 'system' AND e.event = 'checkin_decision'"
    f" AND {fleet_alias_range('e.subject_alias')}"
    f" ORDER BY {_epoch('e.occurred_at')} DESC, e.ingest_seq DESC"
)
```

- [ ] **Step 4: The command**

```python
# claudlobby/commands/checkins.py
"""`claudlobby checkins` — the check-in's read door (manager check-in spec §11),
minimal form: the decision rows, newest first. `--summary`, `--limit` and the
outcome join land in chunk 3.

Two connections, on purpose: `brief.plane_session` is THE reachability door for
the package (no db / no fleet / a plane that has never seen the fleet all refuse
with a note -- unreachable is not empty), but its connection yields TUPLES
(plane-readers.py:53-58; status.py:218). The rows are read through
`plane.db.open_ro`, which sets sqlite3.Row -- the `commands/task.py` pattern. The
session is a context manager; it is probed and closed here on purpose (nothing is
read through it), not entered.
Usage errors (no fleet, an unparseable --since) are rc 2 -- dispatch-overdue.py's
convention for a read door (2 = malformed call, 3 = cannot answer); report-back
says 1 for the same case and task-act.sh's write ladder puts usage at 1 too.
One convention for the plane's READ doors wins over matching either sibling."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

from ..plane.db import open_ro
from ..plane.queries import CHECKIN_ROWS_SQL, fleet_range_params
from ._helpers import _resolve_paths, refuse_unreachable


def _since(text: str) -> datetime:
    """The `--since` grammar the read doors share: 24h, 7d, 30m, or an ISO
    instant (`cmd_report_back` applies the same rule inline, core.py)."""
    raw = (text or "").strip()
    now = datetime.now(timezone.utc)
    try:
        if raw.endswith("h"):
            return now - timedelta(hours=int(raw[:-1]))
        if raw.endswith("d"):
            return now - timedelta(days=int(raw[:-1]))
        if raw.endswith("m"):
            return now - timedelta(minutes=int(raw[:-1]))
        got = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return got if got.tzinfo else got.replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(f"cannot parse --since '{raw}' (use e.g. 24h, 7d, 30m, or ISO date)") from None


def _bot_of(alias: str | None) -> str:
    return alias.rsplit("/", 1)[-1] if alias else ""


def _row(r) -> dict:
    truncated = bool(r["detail_truncated"])
    detail = r["detail"]
    rec = json.loads(detail) if detail and not truncated else None    # a data-less row parses to nothing, never raises
    if not isinstance(rec, dict):
        rec = None
    g = rec or {}
    raise_ = g.get("raise") if isinstance(g.get("raise"), dict) else {}
    seen = g.get("inputs_seen") if isinstance(g.get("inputs_seen"), dict) else {}
    return {
        "checkin_id": g.get("checkin_id"), "prev_checkin_id": g.get("prev_checkin_id"),
        "bot": _bot_of(r["subject_alias"]), "occurred_at": r["occurred_at"],
        "action": g.get("action"), "project_key": g.get("project_key"),
        "raise": {"decided": bool(raise_.get("decided")), "reason": raise_.get("reason")},
        "rationale": g.get("rationale"),
        "considered": list(seen.get("considered") or []), "unavailable": list(seen.get("unavailable") or []),
        "truncated": truncated, "record": rec,
    }


def collect_checkins(conn, fleet: str, *, since: datetime | None, bot: str | None,
                     last: bool, raised: bool = False) -> list[dict]:
    """Newest first by occurred_at. `since` None means no window (--last);
    `raised` keeps only rows whose raise.decided is true (the ask count)."""
    out: list[dict] = []
    for r in conn.execute(CHECKIN_ROWS_SQL, fleet_range_params(fleet)):
        if bot and _bot_of(r["subject_alias"]) != bot:
            continue
        if since is not None and datetime.fromisoformat(r["occurred_at"]) < since:
            continue
        row = _row(r)
        if raised and not row["raise"]["decided"]:
            continue
        out.append(row)
        if last:
            break
    return out


def cmd_checkins(args) -> int:
    paths = _resolve_paths(args)
    from ..brief import TEXT_ROW_LIMIT, plane_session, resolve_fleet_name

    fleet = getattr(args, "checkins_fleet", None) or resolve_fleet_name(paths)
    if not fleet:
        print("checkins: no fleet is named (--fleet <name>, or a fleet.yaml naming one)"
              " — the plane's rows are per fleet", file=sys.stderr)
        return 2
    try:
        since = None if args.last else _since(args.since)
    except ValueError as exc:
        print(f"checkins: {exc}", file=sys.stderr)
        return 2
    plane, note = plane_session(paths, fleet)
    if plane is None:
        return refuse_unreachable("checkins", note)
    plane.close()
    conn, reason = open_ro(paths.root)
    if conn is None:
        return refuse_unreachable("checkins", reason or "plane db unreadable")
    try:
        rows = collect_checkins(conn, fleet, since=since, bot=args.bot, last=args.last,
                                raised=getattr(args, "raised", False))
    finally:
        conn.close()
    if args.json:
        print(json.dumps({"schema": 1, "fleet": fleet,
                          "since": since.isoformat() if since else None,
                          "checkins": rows}, indent=2))
        return 0
    scope = f"fleet {fleet}" + (f", bot {args.bot}" if args.bot else "") + \
        (" (newest only)" if args.last else f", last {args.since}") + \
        (" (asks only)" if getattr(args, "raised", False) else "")
    if not rows:
        print(f"no check-ins — {scope}" + ("" if getattr(args, "last", False) else " (--last reads the newest row with no window)"))
        return 0
    print(f"check-ins — {scope}: {len(rows)}")
    for r in rows[:TEXT_ROW_LIMIT]:
        if r["truncated"]:
            print(f"  {r['occurred_at']}  {r['bot']}  (record over the size cap — truncated at ingest)")
            continue
        if r["record"] is None:
            print(f"  {r['occurred_at']}  {r['bot']}  (row carries no record)")
            continue
        raised = " · raised" if r["raise"]["decided"] else ""
        proj = f" [{r['project_key']}]" if r["project_key"] else ""
        print(f"  {r['occurred_at']}  {r['bot']}  {r['action']}{proj}{raised}  {r['checkin_id']}")
        print(f"      {r['rationale']}")
        print(f"      surfacing: {r['raise']['reason']}")
        if r["considered"]:
            print("      passed over: " + " · ".join(r["considered"]))
        if r["unavailable"]:
            print("      unavailable: " + ", ".join(r["unavailable"]))
    if len(rows) > TEXT_ROW_LIMIT:
        # silent truncation reads as exhaustive coverage (brief.py's rows() rule)
        print(f"  ... showing the newest {TEXT_ROW_LIMIT} of {len(rows)} — full list in --json")
    return 0
```

Register it in `claudlobby/commands/_parsers.py` directly after the `workstreams` block (ends `:197`); the import is a **standalone** line placed after the `from .core import (…)` block (never inside it — `cmd_checkins` is not in `core`):

```python
from .checkins import cmd_checkins
```

```python
    pck = sub.add_parser("checkins", help="The manager check-in's decisions, newest first (plane read)")
    pck.add_argument("--fleet", dest="checkins_fleet", default=None,
                     help="fleet whose rows to read (default: the fleet.yaml this root names)")
    pck.add_argument("--bot", default=None, help="one manager's rows only")
    pck.add_argument("--since", default="7d", help="window: 24h, 7d, 30m, or an ISO instant (default 7d)")
    pck.add_argument("--last", action="store_true", help="only the newest row, ignoring --since")
    pck.add_argument("--raised", action="store_true", help="only the rows that surfaced to the operator (raise.decided) — the ask count")
    pck.add_argument("--json", action="store_true", help="machine-facing envelope")
    pck.set_defaults(func=cmd_checkins)
```

(`dest="checkins_fleet"`, not `fleet`: a subparser copies its namespace over the parent's — `_parsers.py:223-225`.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_checkins_cli.py tests/test_main.py -q`
Expected: all pass. The truncation test depends on the 16 KiB DIAGNOSTIC cap truncating at ingest (`ingest.py:286-294`); if `emit_batch` rejects the oversize record instead, that is a contract change on main — stop and re-read `FIELD_POLICY[("system","data")]`.

- [ ] **Step 6: Commit**

```bash
git add claudlobby/plane/queries.py claudlobby/commands/checkins.py claudlobby/commands/_parsers.py tests/test_checkins_cli.py
printf '%s\n' 'feat(cli): claudlobby checkins — the decision rows, newest first by occurred_at' '' 'Reachability through brief.plane_session (unreachable is not empty, rc 3 via' 'refuse_unreachable, a never-seen fleet included), rows through plane.db.open_ro' '(the session yields tuples), the record parsed once in Python. --last ignores' 'the window so an idle month never reads as a first check-in; truncated and' 'data-less rows are listed and marked, never dropped; text shows the losers and' 'caps at TEXT_ROW_LIMIT with a disclosure. Spec §11.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c3.txt"
git commit -q -F "$TMPDIR/c3.txt" && git log --oneline -1
```

---

### Task 4: The skill

**Files:**
- Create: `library/skills/checkin/SKILL.md` (the protocol is chunk 2's — cycle-8)
- Test: `tests/test_checkin_library.py`

**Interfaces:**
- No protocol ships in this chunk (cycle-8): the skill restates every clause a chunk-1 run could depend on — the post shape, the two-ask rate limit read through `--raised`, the silence default — and a skill is read per use, so no restart is needed for the run; the protocol (its precedence sentence, both audiences, no `requires:`, no self-fire clause) lands in chunk 2 with the trigger it governs, and its composed-carrier rehearsal happens there.
- The skill consumes, by name: `claudlobby checkins --bot $BOT_ID --last --json` and `claudlobby checkins --bot $BOT_ID --since 7d --raised --json` (READ 0), `claudlobby brief --bot $BOT_ID --json` (READ 1), `claudlobby status --json` (READ 1b — the roster with `bots[].state` / `pane_state` / `tmux_alive` (`status.py:614-640`), the only door that answers "which worker is idle"; cycle-3 R5), `claudron lookup --limit 5 <project>`, the `## Projects` table and `## Fleet Mission` section of the manager's own composed `CLAUDE.md` (READ 3 — in context, no call: `templates/claude.md.j2:193-205` composes the table from `projects.yaml` and `composer.py:1887-1893` composes the charter for a manager; the `PROJECT_TIER_*`/`PROJECT_REPOS_*` env vars carry the same facts but reading them needs an ungranted shell call — cycle-8 devex), `gh issue list …` on ONE line, `bash "$CLAUDLOBBY_ROOT/lib/checkin-record.sh" <<'EOF'` (a here-doc on the door, never `cat |`; its `--dry-run` twin is shown too), `bash "$CLAUDLOBBY_ROOT/lib/dispatch-task.sh" --project … --checkin …`, and for `ask` the Telegram reply tool or `bash "$CLAUDLOBBY_ROOT/lib/tg-post.sh"` (the tmux-injected case has no chat to reply to).
- `inputs_seen.considered` and `inputs_seen.unavailable` are ALWAYS written (empty allowed): the contract refuses a record without them (cycle-8).
- Grants follow the shipped shapes exactly (Global Constraints) and are verb-scoped to what the skill runs (cycle-4 R2: `Bash(claudlobby *)` would hand an unattended loop `generate`/`promote`/`new-bot`/`move-bot`/`plane prune`/`task` — writes its `dispatch | ask | nothing` record cannot express): `Bash(claudlobby checkins *)`, `Bash(claudlobby brief *)`, `Bash(claudlobby status *)`, `Bash(claudron lookup *)`, `Bash(gh issue list *)`, `Bash(*checkin-record.sh*)`, `Bash(*dispatch-task.sh*)`, `Bash(*tg-post.sh*)`, `mcp__plugin_telegram_telegram__reply`, `Read`. The star-bounded script form is the shape `restart/SKILL.md:4` ships — under `allowed-tools`, a key the compositor never reads — so this is the first `Bash(*<script>*)` grant to ride `tool_grants` (`loader.py:361`, `composer.py:2245`; `status/SKILL.md:5-8` is the key precedent), and the library test asserts the composer resolves it.
- The degraded rule keys on `mode` (Global Constraints, the live capture): only an `omitted` entry on a field the skill uses makes it unavailable; `labeled` is present-and-bounded.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_checkin_library.py
"""The /checkin skill (spec §6) is whole, is coupled to its doors by name and to
the contract by key, and its grants MATCH the command lines it writes (permissions-model.md:48-52: a space is a word boundary;
a pipeline is matched per subcommand) and contain no forbidden wildcard
(claudron-integration.md:29; boundary Invariant 5)."""

import fnmatch
import importlib.util
import json
import re
import shutil
from pathlib import Path

from claudlobby.config import load_fleet
from claudlobby.loader import parse_frontmatter
from claudlobby.paths import Paths
from claudlobby.status import BotStatus, format_json
from tests.conftest import install_real_template

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "library"
SKILL = LIB / "skills" / "checkin" / "SKILL.md"


def _flat(text: str) -> str:
    """Whitespace-collapsed: prose and code spans wrap at ~85 columns, and a
    needle that straddles a wrap must still be found (cycle-3 B5, R7)."""
    return " ".join(text.split())


DOORS = ["claudlobby checkins --bot $BOT_ID --last --json", "claudlobby checkins --bot $BOT_ID --since 7d --raised --json",
         "claudlobby brief --bot $BOT_ID --json", "claudlobby status --json", "claudron lookup --limit 5", "gh issue list",
         'bash "$CLAUDLOBBY_ROOT/lib/checkin-record.sh" <<\'EOF\'', 'ck=$(bash "$CLAUDLOBBY_ROOT/lib/checkin-record.sh" <<\'EOF\'', '--checkin "$ck"',
         'ck=$(bash "$CLAUDLOBBY_ROOT/lib/checkin-record.sh" --dry-run <<\'EOF\'', ') && claudlobby checkins --bot $BOT_ID --last --json',
         "lib/dispatch-task.sh", "--checkin", "--project", "lib/tg-post.sh", "## Projects", "## Fleet Mission",
         "pane_state", "issues_seen", "DRY-RUN", "checkins[0]", "unavailable"]
CONTRACT = REPO / "lib" / "checkin-contract.py"
_spec = importlib.util.spec_from_file_location("checkin_contract", CONTRACT)
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)


def test_the_record_template_carries_every_contract_key():
    # the here-doc is the model's only example of the contract; a dropped key would refuse
    # every real check-in at rc 2 and the fallback would keep writing stub rows (cycle-8 devex)
    text = SKILL.read_text()
    record = re.search(r"```bash\nck=\$\(bash \"\$CLAUDLOBBY_ROOT/lib/checkin-record.sh\" <<'EOF'\n(.*?)\nEOF", text, re.S).group(1)
    for key in (*cc.INPUTS_COUNTS, *cc.INPUTS_LISTS, *cc.DELTA_COUNTS, "prev_checkin_id", "project_key", "rationale", "decided", "reason", "held"):
        assert f'"{key}"' in record, f"the RECORD template lost {key}"


def test_the_roster_door_still_types_an_unobserved_worker_as_null():
    # the skill's dispatch gate reads pane_state null as UNOBSERVED; status.py writes
    # `bs.pane_state or None` over a dataclass default of "" -- pin it (cycle-8 engineering)
    bots = json.loads(format_json([BotStatus(name="w1")], "f"))["bots"]
    assert bots[0]["pane_state"] is None and bots[0]["plane_unreachable"] is None


def test_the_skill_file_is_whole_and_its_three_bash_blocks_are_closed():
    # cycle-3 B6: a nested fence in the plan truncated the deliverable at the inner
    # closer and every test still passed on the stump; pin the tail and the fences
    # (three blocks since cycle 9: the plain record, the record-and-dispatch call, the dry run)
    text = SKILL.read_text()
    assert "## Not in this chunk" in text and "## Rules" in text
    assert text.count("```") == 6 and len(re.findall(r"```bash\n.*?```", text, re.S)) == 3


def test_the_skill_is_coupled_to_its_doors():
    text = _flat(SKILL.read_text())
    for d in DOORS:
        assert d in text, d
    for action in ("dispatch", "ask", "nothing"):
        assert f"**{action}**" in text, action
    assert "RECORD before ACT" in text and "considered" in text and "could not measure" in text
    assert "follow-up check-in" in text                               # a failed ACT is recorded, never retried blind
    assert "names the chosen project and its tier" in text            # the rigor bar was weighed, not only what was picked
    assert "minimal valid `nothing` row" in text                       # the re-record is bounded: a turn never ends without a row
    assert "UNOBSERVED" in text and "plane_unreachable" in text          # a roster answer with no observation is not an idle worker
    assert "propose" not in text.split("## Not in this chunk")[0]   # the enum the contract accepts
    assert "cat <<" not in text                                      # a pipeline is matched per subcommand


def test_the_degraded_rule_is_keyed_on_mode_omitted_per_field():
    # brief's degraded[] is NEVER empty on a real fleet (captured live, cycle 4: a
    # fleet with a plane carries alerts:labeled #903 and dispatches.orphaned:labeled
    # #1014 on every call); only mode "omitted" means a field is absent (#1467)
    rule = _flat(SKILL.read_text().split("## DECIDE")[0])
    assert "`mode` is `omitted`" in rule and "unavailable" in rule
    assert "`mode` is `labeled`" in rule and "present" in rule
    assert "utilization" in rule and "not an input" in rule
    assert "plane-known" in _flat(SKILL.read_text())


# --- the grants match the command lines the skill itself writes --------------------

FORBIDDEN = ("Bash", "Bash(*)", "Bash(bash *)", "Bash(cat *)", "Bash(claudron *)", "Bash(sh *)", "Bash(gh *)", "Bash(claudlobby *)")


def _bash_grant_matches(grant: str, command: str) -> bool:
    """permissions-model.md:48-51 — the pattern inside Bash(...) is a glob over
    the command line; a trailing ' *' requires a space (a word boundary)."""
    assert grant.startswith("Bash(") and grant.endswith(")")
    return fnmatch.fnmatchcase(command, grant[5:-1])


def _skill_command_lines() -> list[str]:
    """Every `bash …`, `claudlobby …`, `claudron …`, `gh …` span — inline code,
    which MAY wrap across lines — or fenced-bash line of SKILL.md, whitespace
    collapsed; first pipeline stage only (the skill must not use pipelines)."""
    text = SKILL.read_text()
    cmds = re.findall(r"`((?:bash|claudlobby|claudron|gh) [^`]+)`", text)
    for block in re.findall(r"```bash\n(.*?)```", text, re.S):
        for line in block.splitlines():
            for piece in line.split(" && "):                        # a compound command is matched per subcommand
                piece = re.sub(r"^[a-z_]+=\$\(", "", piece.strip())   # `ck=$(bash …` is the bash subcommand
                piece = re.sub(r"^\)\s*", "", piece)                    # `) && claudlobby …` closes the substitution
                if piece.startswith(("bash ", "claudlobby ", "claudron ", "gh ")):
                    cmds.append(piece)
    return [_flat(c) for c in cmds]


def test_the_skill_grants_cover_its_own_commands_and_nothing_forbidden():
    fm, _ = parse_frontmatter(SKILL.read_text())
    grants = fm["tool_grants"]
    assert not [g for g in grants if g in FORBIDDEN], grants
    assert "mcp__plugin_telegram_telegram__reply" in grants           # ask posts through the reply tool
    bash_grants = [g for g in grants if g.startswith("Bash(")]
    for g in bash_grants:                                              # a CLI grant names ONE literal verb, never a prefix
        if g.startswith("Bash(claudlobby"):
            assert re.fullmatch(r"Bash\(claudlobby [a-z][a-z-]+ \*\)", g), g
    cmds = _skill_command_lines()
    assert cmds, "no command lines found in the skill"
    assert any(c.startswith("gh issue list ") for c in cmds)          # the wrapped span IS collected (cycle-3 R7)
    assert any("--dry-run" in c for c in cmds)                          # the dry run has a runnable invocation
    assert any(c.startswith('bash "$CLAUDLOBBY_ROOT/lib/dispatch-task.sh"') and '"$ck"' in c for c in cmds)   # the act rides the record call (cycle-7 R1)
    for block in re.findall(r"```bash\n(.*?)```", SKILL.read_text(), re.S):                                # …and none hides in a fenced block
        for line in block.splitlines():
            for piece in line.split(" && "):
                piece = re.sub(r"^[a-z_]+=\$\(", "", re.sub(r"^\)\s*", "", piece.strip()))
                assert not re.match(r"(echo|printf|env|cat|export)\b", piece), f"ungranted builtin: {line!r}"
    for c in cmds:
        assert any(_bash_grant_matches(g, c) for g in bash_grants), f"ungranted: {c!r}"
    # necessity: every grant is the ONLY match for some command line, so a grant
    # widened to a wildcard (which still "covers") shows up as a sibling made idle
    for g in bash_grants:
        others = [o for o in bash_grants if o != g]
        assert any(not any(_bash_grant_matches(o, c) for o in others)
                   for c in cmds if _bash_grant_matches(g, c)), f"grant {g} covers nothing on its own"


def test_the_composer_resolves_the_script_grants_through_tool_grants(fleet_dir):
    # restart/SKILL.md declares Bash(*spin-up-bot.sh*) under allowed-tools, a key
    # the compositor never reads: this is the first star-bounded script grant that
    # must ride the tool_grants path (loader.iter_skill_grants -> composer._resolve_skill_grants)
    from claudlobby.composer import _resolve_skill_grants
    install_real_template(fleet_dir)
    dst = fleet_dir / "library" / "skills" / "checkin"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy(SKILL, dst / "SKILL.md")
    text = (fleet_dir / "fleet.yaml").read_text().replace(
        "    lead:\n", "    lead:\n      skills: [checkin]\n", 1)
    (fleet_dir / "fleet.yaml").write_text(text)
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    grants = _resolve_skill_grants(fleet.bots["lead"], Paths(root=fleet_dir, fleet_dir=fleet_dir))
    for g in ("Bash(*checkin-record.sh*)", "Bash(*dispatch-task.sh*)", "Bash(*tg-post.sh*)",
              "Bash(claudlobby checkins *)", "Bash(claudlobby status *)"):
        assert g in grants, grants
    assert "Bash(claudlobby *)" not in grants
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/pytest tests/test_checkin_library.py -q`
Expected: all fail on the missing files except `test_the_cadence_rules_are_untouched_in_this_chunk` (the guard that F3 stays honoured).

- [ ] **Step 3: (no protocol this chunk — cycle-8; the file and its two tests land in chunk 2 with the trigger)**

- [ ] **Step 4: The skill**

The deliverable below is fenced with FOUR backticks because it contains three-backtick blocks of its own (cycle-3 B6: a three-backtick outer fence closes at the first inner closer and the tail of the file is lost — and every test still passes on the stump, which is what `test_the_skill_file_is_whole…` now pins).

````markdown
---
name: checkin
description: "The idle-manager check-in: read the SSOT (the plane through checkins, brief and status, Claudron, the mission with each project's tier and repos, the GitHub backlog), decide ONE project and ONE action, record the decision BEFORE acting, and let the surfacing judgment decide whether the operator hears anything at all. Silence is the default."
argument-hint: "[--dry-run]"
tool_grants:
  - "Bash(claudlobby checkins *)"
  - "Bash(claudlobby brief *)"
  - "Bash(claudlobby status *)"
  - "Bash(claudron lookup *)"
  - "Bash(gh issue list *)"
  - "Bash(*checkin-record.sh*)"
  - "Bash(*dispatch-task.sh*)"
  - "Bash(*tg-post.sh*)"
  - "mcp__plugin_telegram_telegram__reply"
  - "Read"
---

# Check-in

Your own re-engagement cycle. Nobody is watching it; what they may see is only what
the surfacing judgment (DECIDE, below) lets through. **Every read goes through a
named door and every write through a named door** — never a hand-rolled query,
never a hand-built plane envelope, never a pipeline. That coupling is what makes
your reasoning inspectable (the `checkins` read door) and the edges deterministic.

`$BOT_ID`, `$FLEET_NAME` and `$CLAUDLOBBY_ROOT` come from your `bot.conf`; each
project's key, repos and tier come from the `## Projects` table in your own
CLAUDE.md, which is in your context before this skill runs. If no
`## Projects` table is composed into your CLAUDE.md, this fleet has no
`projects.yaml`: `dispatch` is not available to you (it needs `--project`), and the
check-in ends in `ask` or `nothing` — say so in the rationale.

## Arguments

Parse `$ARGUMENTS`:
- `--dry-run`: do every READ and the DECIDE, then validate the decision through the
  door's own dry run (the RECORD here-doc with `--dry-run`, which prints
  `DRY-RUN <ck_id>` and records nothing), and print, without running it, the
  single-call ACT line you would have run. Act on nothing.

## READ — in order, all cheap, all SSOT

A step that fails is **recorded, never guessed around**: add its name to
`inputs_seen.unavailable` and continue. A count you could not measure is `null`
(**could not measure**), never `0` — in `inputs_seen` and in `delta` alike.

0. **The previous check-in, and this week's asks** —
   `claudlobby checkins --bot $BOT_ID --last --json` (the row is `checkins[0]`;
   its `record.inputs_seen` is *the state at the last check-in*; keep its
   `checkin_id` for `prev_checkin_id`; an empty `checkins` → `null`) and
   `claudlobby checkins --bot $BOT_ID --since 7d --raised --json` (the `checkins[]`
   rows are the asks already raised this week; count them — `--raised` keeps the
   read to those rows). rc 3 means
   the plane is unreachable — record `checkins` as unavailable and `prev_checkin_id`
   as `null`; the record shows both, so a skipped read never poses as a first one.
1. **The fleet's present** — `claudlobby brief --bot $BOT_ID --json`: `dispatches`
   (`open` / `overdue` / `orphaned` / `dispatched`, each row with `escalated`,
   `nudged`, `last_progress_at`), `workstreams` (`active`, `stalled`),
   `reports.unacked`, `alerts` (last 24h critical), `mission`. Read `degraded[]`
   **by mode, for the fields you use**: an entry whose `mode` is `omitted` and whose
   `field` is `dispatches`, `workstreams`, `reports` or `alerts` (or a dotted child,
   such as `dispatches.open`) makes that section **unavailable** — never zero. An
   entry whose `mode` is `labeled` means the field is present and bounded — a real
   fleet's brief always carries `alerts` labeled, and usually `dispatches.orphaned`
   — so use the field and note the bound. The standing `utilization` entry (#891)
   is **not an input** of this skill; ignore it.
1b. **The roster, and who is idle** — `claudlobby status --json`: `bots[]`, each with
   `name` (the id the ACT line takes as `<worker>`), `state`, `pane_state` (`BUSY` /
   `IDLE`), `tmux_alive`, `current_task`, and `plane_unreachable` (non-null when the
   plane could not be read for that bot). A `null` `pane_state` — with or without
   `plane_unreachable` set (a fleet with no heartbeats yet) — means the worker is
   UNOBSERVED, not idle. `dispatch` needs an alive, observed, idle worker; the
   rationale names the worker and its observed `pane_state`. (This third door exists
   because `brief` carries no per-bot pane state — it answers what is open, not who
   is idle; add no fourth.)
2. **Knowledge** — `claudron lookup --limit 5 <project>` for each project with open
   work. Count the hits (`knowledge_hits`); read what is relevant.
3. **The goal and each project's rigor** — both are already in your context, no
   call needed: the `## Fleet Mission` section of your own CLAUDE.md (the charter is
   composed in for a manager) and the `## Projects` table (Project · Title · Repos ·
   Tier · Mission, composed from `projects.yaml`): the Tier is how that project's
   work CLOSES, the Repos are what step 4 reads. If there is no `## Projects` table,
   this fleet has no `projects.yaml` and `dispatch` is not available to you (it needs
   `--project`). **If there is no `## Fleet Mission` section and no mission line,
   record it**: put `mission` in `inputs_seen.unavailable`, set `issues_considered`
   to `null` (a count filtered by a mission you could not read is fabricated), and
   say so in the rationale — a decision taken against no charter must not look like
   one taken against a real one.
4. **The external backlog** — per repo in the `## Projects` table, one line:
   `gh issue list --repo <owner/name> --state open --limit 50 --json number,title,labels,updatedAt`.
   Count every open issue returned as `issues_seen` (the raw number, BEFORE any
   filter), then filter to mission-aligned items, group by project, and count those
   as `issues_considered`. The two together tell a broken filter from an empty
   backlog (`issues_seen` is capped at the `--limit`, so it discriminates 0 from
   non-zero, not 50 from 3,000); a read that failed goes into `unavailable`, which
   is what tells a broken query from an empty backlog — write both list keys on
   every record, empty when empty, never omitted.
5. **The delta** — now versus step 0, per project: tasks opened / completed /
   stalled / cleared, new issues, new messages, held items still pending. **The
   delta is the primary signal** for both the action and the surfacing judgment: an
   unchanged world argues for `nothing` and silence.

**Everything above is read per project.** The check-in is a portfolio decision.

## DECIDE — one project, then exactly one action

**The allocation rule.** Attention is a weighting, never a ranking: every project
with open work or a blocker is checked on every check-in (the floor); *new* effort
— picking backlog issues — goes preferentially to projects with recent attention,
never exclusively (the tilt); no attention + no open work = left alone — a dormant
project is revived only by explicit direction (never stalest-first). Mission
alignment and backlog depth weigh in. **Record the losers:** every candidate you
weighed and passed over goes into `inputs_seen.considered` as one line, `<candidate>
— <why not>` (at most ten) — a decision is judged by what it did not pick, and a
`dispatch` with an empty `considered` is refused by the contract.

| action | when | through |
|---|---|---|
| **dispatch** | an open or backlog item fits an idle worker (step 1b); you choose the worker and the rationale says why | ONE Bash call — the RECORD here-doc with the dispatch appended by `&&` (the block under RECORD before ACT): `bash "$CLAUDLOBBY_ROOT/lib/dispatch-task.sh" --project <key> [--repo <owner/name>] [--ref <issue-url>] --checkin "$ck" <worker> "<task>"` — `--project` is the well-defined bar (a projects.yaml key); `--checkin` joins the dispatch to this decision; `$ck` is the id the record door just printed, captured in the same call |
| **ask** | the surfacing judgment (below) concludes the operator should hear something — a fork only they can resolve, or the backlog holds nothing worth starting ("ask for tasks") | one Telegram post in the shape chunk 2's protocol will fix (one line, one ask with named options, one pointer): the reply tool when this check-in arrived on Telegram; `bash "$CLAUDLOBBY_ROOT/lib/tg-post.sh" "<the post>"` when it was injected into your pane (there is no chat to reply to). Either way it is recorded as your communication |
| **nothing** | all work in flight, nothing worthwhile — **recorded**, so "checked and chose nothing" is a fact, not silence | — |

**The surfacing judgment.** Its default answer is **no**. Weigh, at minimum: does
this genuinely need a human (a `requires-approval` boundary in the mission, a tier
that mandates sign-off, conflicting priorities)? · would the operator want to know (a
deliverable ready, a blocker that stalls the fleet, a failure with cost)? · what
changed since they were last told (the delta against step 0 and the last post's
`held`)? · has enough accumulated to be worth one message? · have two asks already
been raised this week (step 0 — then proceed on your best tier-gated judgment or
wait quietly, never a third)? · what Claudron says about how the operator wants to
be engaged · the urgency floor (a `blocked` that stalls everything breaks through
regardless). Record the judgment in `raise`: `decided`, `reason` (always, in both
directions), and what you `held`.

**Degraded inputs, per input.** The plane unreachable (`checkins` rc 3, or
`dispatches`/`workstreams`/`reports`/`alerts` **omitted** in `brief`, or `status`
failing) narrows the actions to `ask | nothing` — never dispatch blind. `gh` or
`claudron` unavailable narrows only the **source** of new work: you may still
dispatch open, **plane-known** tasks; you may not pick fresh backlog issues you
could not read. Record what was unavailable either way.

## RECORD before ACT

Build the decision as JSON (schema 1) and record it FIRST — the decision exists even
if the action then fails. The door is invoked directly with a here-doc (never
through `cat |`). For `ask` and `nothing` the record is the door call alone (the
first form below: the door prints the id; you do not need it — READ 0 finds it next
time); for `dispatch` the SAME call carries the act after `&&` (the second form),
so the id never crosses a tool boundary and a refused or unrecorded decision (rc 2 /
rc 3) skips the act by construction. **Nothing else rides either call** — no `echo`,
no `printf`: a builtin is an ungranted subcommand in this session. Two shapes are
coupled: **`ask` requires `raise.decided: true`**,
and **`dispatch` requires `project_key: "<slug>"` and a non-empty `considered`**.
Every `N|null` below is an integer or `null`, never omitted — in both blocks. The
rationale names the chosen project and its tier (the `## Projects` table): the record
must show the rigor bar was weighed, not only what was picked.

For `ask` or `nothing`:

```bash
bash "$CLAUDLOBBY_ROOT/lib/checkin-record.sh" <<'EOF'
{ ...the decision JSON below... }
EOF
```

For `dispatch`, the record and the act as ONE call:

```bash
ck=$(bash "$CLAUDLOBBY_ROOT/lib/checkin-record.sh" <<'EOF'
{"prev_checkin_id": <"ck_…" from step 0, or null>,
 "inputs_seen": {"open_tasks": N|null, "stalls": N|null, "unacked": N|null,
                 "issues_seen": N|null, "issues_considered": N|null, "knowledge_hits": N|null,
                 "considered": ["<candidate> — <why not>"], "unavailable": []},
 "delta": {"tasks_opened": N|null, "tasks_completed": N|null, "stalls_appeared": N|null,
           "stalls_cleared": N|null, "issues_new": N|null, "messages_new": N|null, "held_pending": N|null},
 "action": "dispatch|ask|nothing",
 "project_key": <"<slug>" for dispatch (or the project an ask is about), else null>,
 "rationale": "<your words, <= 600 chars: the weighing, the project and its tier, the worker and its observed state, the why>",
 "raise": {"decided": <true for ask, else false>, "reason": "<why it surfaced, or why not, <= 600>", "held": []}}
EOF
) && bash "$CLAUDLOBBY_ROOT/lib/dispatch-task.sh" --project <key> --checkin "$ck" <worker> "<task>"
```

The `&&` is RECORD-before-ACT made mechanical: the door prints the `checkin_id` alone
on success, `$ck` carries it into the dispatch in the same call (a shell variable
does not survive between your tool calls, which is why the two are never split), and
rc 2 or rc 3 from the door skips the act. If the dispatch door says the plane cannot
see that id, verify with `claudlobby checkins --bot $BOT_ID --last --json` before
anything else. rc 2: the decision was refused — every reason is on stderr;
fix and re-record **once**; if the second attempt is refused too, record the minimal
valid `nothing` row and stop — minimal means EVERY required key, so it cannot be
refused a third time: `prev_checkin_id` from step 0 (or `null`), every count `null`,
`considered` `[]`, `unavailable` listing the reads that failed, `action: "nothing"`,
`project_key: null`, a `rationale` quoting the refusal reasons, and `raise` with
`decided: false`, a non-empty `reason` ("refused twice; nothing surfaced") and
`held: []` — the record's existence is the point, a
turn that ends with no row is the one failure the loop must not produce. rc 3: the
plane did not record it — do **not** act on an unrecorded `dispatch` (the `&&` has
already skipped it); say so in your next justified post. For `ask`, ACT through the
door in the table once the record has printed its id. **If the ACT door fails** (a nonzero rc from `dispatch-task.sh` or the post),
record a **follow-up check-in** at once — `prev_checkin_id` = the id just printed,
`action: nothing`, the rationale naming the failure — and never retry a dispatch
blind; the pair is what the outcome join will show.

Under `--dry-run` the same here-doc goes to the door's dry run, which validates and
prints `DRY-RUN <ck_id>` (the id is the SECOND word; a real run prints the id
alone), in the composed shape with a harmless granted read standing where the
dispatch would go — so the shape itself passes through the permission layer before
any real run relies on it:

```bash
ck=$(bash "$CLAUDLOBBY_ROOT/lib/checkin-record.sh" --dry-run <<'EOF'
{ ...the same decision JSON... }
EOF
) && claudlobby checkins --bot $BOT_ID --last --json
```

Then print, in your reply and not as a command, the ACT line you would have run.

## Not in this chunk

`propose` (generating new work into an intake store, gated by a project's
`planning.initiative`) and `sprint` (batch execution) arrive with later chunks and
widen the `action` enum then. Until they do, "the backlog holds nothing worth
starting" is an `ask`, not an invention.

## Rules

- One project, one action, one record per check-in. Never two actions.
- Never freelance a read (no ad-hoc SQL, no reading state files) or a write (no
  hand-built plane envelopes): the doors above are the whole contract.
- Never restate a project's tier in a post; point to it.
- `--dry-run` never records and never acts.
````

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_checkin_library.py tests/test_skill_ref_resolution.py tests/test_dispatch_type.py tests/test_boundary_invariants.py -q`
Expected: all pass. (A `claudlobby validate` pass against a fresh worktree is vacuous — no `fleet.yaml`, rc 1, zero skill output, measured in cycle 3 — so the grant shapes are proven by the library test alone, and by the deploy's per-fleet `validate` in Task 7 step 6.)

- [ ] **Step 6: Commit**

```bash
git add library/skills/checkin/SKILL.md tests/test_checkin_library.py
printf '%s\n' 'feat(library): the /checkin skill' '' 'A thin reasoning wrapper coupled to its doors by name and to the contract by key:' 'actions dispatch | ask | nothing, the losers and the raw backlog count recorded,' 'a per-input degraded rule keyed on brief mode=omitted, the roster read for who' 'is idle, a follow-up check-in on a failed ACT, verb-scoped grants in the shipped' 'shapes matched against its own command lines by test and resolved through the' 'composer. Spec §6, §9.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c4.txt"
git commit -q -F "$TMPDIR/c4.txt" && git log --oneline -1
```

---

### Task 5: The empirical gate — the record door and the read door on a real plane through the harness

**Files:**
- Modify: `lib/validate-bot-change.sh` (append a scenario block before the harness summary; helpers `val_plane_ready`, `val_sql` at `:163,171`, `harness_check` at `lib-common.sh:4634`, `$VAL_CLI`, `$VAL_REPO`, `$LIB_DIR`, `$ROOT`; the harness already exports a SHORT `PLANE_SOCKET` at `:135`)

**Interfaces:** consumes Tasks 1 and 3. It proves the two doors under the identity env a manager session carries and through the installed CLI, with the **positive control gated on a non-empty id** (cycle-2 B10: `grep -c ""` counts every line) whose shape is checked with the contract's own regex (cycle-3 B4: a 34-`?` glob failed a correct 32-hex id).

- [ ] **Step 1: Append the block**

```bash
# ===========================================================================
# manager check-in chunk 1 -- the record door and the read door, end to end
# on a real plane. Unit tests pin the contract and the envelopes; what only
# running the real doors proves is that a decision LANDS as a row the read
# door can join, through the real shim, under the identity env a manager
# session carries (BOT_ID, FLEET_NAME), and that "not listed" is a real
# negative -- the positive control runs FIRST and is gated on a non-empty id,
# so a failed record or an unreachable read door can never read as a clean
# answer (an empty grep pattern matches every line).
# ===========================================================================
echo ""
echo "=== validate manager check-in: the decision lands and the read door joins it ==="
CK_FLEET_H="valckf"
val_plane_ready "$ROOT" "$CK_FLEET_H"
ck_decision='{"prev_checkin_id":null,"inputs_seen":{"open_tasks":0,"stalls":0,"unacked":0,"issues_seen":null,"issues_considered":0,"knowledge_hits":0,"considered":[],"unavailable":["gh"]},"delta":{"tasks_opened":0,"tasks_completed":0,"stalls_appeared":0,"stalls_cleared":0,"issues_new":0,"messages_new":null,"held_pending":0},"action":"nothing","project_key":null,"rationale":"harness: nothing worth starting","raise":{"decided":false,"reason":"no delta","held":[]}}'
ck_id=$(printf '%s' "$ck_decision" | env CLAUDLOBBY_ROOT="$ROOT" FLEET_NAME="$CK_FLEET_H" BOT_ID="valckmgr" \
    PLANE_EMIT_CLI="$VAL_CLI" PLANE_SOCKET="$PLANE_SOCKET" \
    bash "$VAL_REPO/lib/checkin-record.sh" 2> "$ROOT/ck-record.err" || true)
printf '%s' "$ck_id" | grep -Eq '^ck_[0-9a-f]{32}$' && r=yes || r=no
harness_check "checkin: the record door returns a ck_<32hex> id" "$r"
ck_row=$(val_sql "$ROOT" "SELECT json_extract(detail,'\$.action') || '|' || severity || '|' || subject_alias FROM events WHERE kind='system' AND event='checkin_decision' AND source_ref='checkin:$ck_id'")
[ -n "$ck_id" ] && [ "$ck_row" = "nothing|notice|bot:$CK_FLEET_H/valckmgr" ] && r=yes || r=no
harness_check "checkin: ...and the decision LANDED as one actor-anchored notice row (source_ref checkin:<id>, BOT_ID alias)" "$r"
# A REFUSAL, on purpose: the ERR-trap class fires only when the contract child
# fails, so a block that feeds the door valid decisions alone can never see it
# (cycle-5 B2). rc 2, nothing on stdout, and -- the line after -- no script_error
# row with this door's name under detail.data.script.
if printf '%s' '{"action":"coffee"}' | env CLAUDLOBBY_ROOT="$ROOT" FLEET_NAME="$CK_FLEET_H" BOT_ID="valckmgr" \
    PLANE_EMIT_CLI="$VAL_CLI" PLANE_SOCKET="$PLANE_SOCKET" \
    bash "$VAL_REPO/lib/checkin-record.sh" > "$ROOT/ck-refuse.out" 2> "$ROOT/ck-refuse.err"; then ck_rc=0; else ck_rc=$?; fi
[ "$ck_rc" = "2" ] && [ ! -s "$ROOT/ck-refuse.out" ] && r=yes || r=no
harness_check "checkin: a malformed decision is refused at rc 2 with nothing printed" "$r"
ck_err=$(val_sql "$ROOT" "SELECT COUNT(*) FROM events WHERE kind='system' AND event='script_error' AND json_extract(detail,'\$.data.script') LIKE 'checkin-record%'")
[ "$ck_err" = "0" ] && r=yes || r=no
harness_check "checkin: ...and neither the record nor the refusal fired a script_error row (the ERR-trap class)" "$r"
ck_other=$(printf '%s' "$ck_decision" | env CLAUDLOBBY_ROOT="$ROOT" FLEET_NAME="$CK_FLEET_H" BOT_ID="valckother" \
    PLANE_EMIT_CLI="$VAL_CLI" PLANE_SOCKET="$PLANE_SOCKET" \
    bash "$VAL_REPO/lib/checkin-record.sh" 2>> "$ROOT/ck-record.err" || true)
# The CLI reaches the plane through <root>/lib/dispatch-overdue.py -- linked for
# THIS scenario and removed after it (the #1481 neighbour rule at :652/:682).
ln -sfn "$LIB_DIR" "$ROOT/lib"
CLAUDLOBBY_ROOT="$ROOT" "$VAL_CLI" --root "$ROOT" checkins --fleet "$CK_FLEET_H" --json \
    > "$ROOT/ck-read.out" 2> "$ROOT/ck-read.err" || true
{ [ -n "$ck_id" ] && grep -q "$ck_id" "$ROOT/ck-read.out"; } && r=yes || r=no
harness_check "checkin: the read door LISTS the decision (positive control, gated on a non-empty id)" "$r"
CLAUDLOBBY_ROOT="$ROOT" "$VAL_CLI" --root "$ROOT" checkins --fleet "$CK_FLEET_H" --bot valckmgr --last --json \
    > "$ROOT/ck-read2.out" 2> "$ROOT/ck-read2.err" || true
rm -f "$ROOT/lib"
{ [ -n "$ck_id" ] && [ -n "$ck_other" ] && grep -q "$ck_id" "$ROOT/ck-read2.out" && ! grep -q "$ck_other" "$ROOT/ck-read2.out"; } && r=yes || r=no
harness_check "checkin: ...--bot --last returns THIS manager's newest row and not the other manager's (a real negative)" "$r"
```

The `script_error` check keys on `json_extract(detail,'$.data.script')`: `install_error_trap` passes `basename "$0"` (`lib-common.sh:4443`), `emit_script_error` puts it in `data.script` (`:4220-4231`), and `emit_fleet_event` wraps that under `detail = {"source", "legacy_ts", "data": {...}}` (`:1440`) while hard-coding `source_ref: fleet-events:sha:<key>` (`:1442`) — a check keyed on the ref could never fail (cycle-4 B1), and neither could one keyed on `$.script`: the row the substitution mutant lands was read back this cycle and its detail is `{"source": "lib", "legacy_ts": …, "data": {"script": "checkin-record.sh", "exit_code": 2, …}}`. A bare-root dispatch elsewhere in the harness emits two `script_error` rows of its own (pre-existing on main, measured), which carry a different `$.script` and so stay out of scope.

- [ ] **Step 2: Run the harness unsandboxed twice — clean, then with the mutant applied — read the six lines each time, and prove the restore**

Every command here sources `$OUT/env.sh` first (Task 0 step 1): this task may run in another session, and nothing survives between tool calls.

Run: `. "$HOME/Projects/claudlobby-worktrees/ck1-out/env.sh"; bash lib/validate-bot-change.sh > "$OUT/vbc.txt" 2>&1; echo "rc=$?"; grep -E 'checkin:' "$OUT/vbc.txt"` (and for the negative control below, the same with `$OUT/vbc-negctl.txt`)
Expected: six `PASS` lines beginning `checkin:`; the harness's overall verdict unchanged from main's (run main's harness once for the baseline if unsure — a pre-existing failure in another block is out of scope and is named, not fixed). If the `script_error` line fails, open `ck-record.err` and the row's `detail` before touching anything: the `$.data.script` value may differ on this root — fix the LIKE, never the assertion.

Negative control (a guard that cannot fail certifies nothing — cycle-4 B1; a control the block cannot trip is no control — cycle-5 B2, which is why the block REFUSES a decision on purpose). Two runs, two files, and the restore proved by `git diff --quiet`, literally:

```bash
. "$HOME/Projects/claudlobby-worktrees/ck1-out/env.sh"
./.venv/bin/python - <<'PY'
import pathlib
p = pathlib.Path("lib/checkin-record.sh"); t = p.read_text()
old = '''if printf '%s' "$raw" | python3 "$LIB_DIR/checkin-contract.py" --checkin-id "$checkin_id" > "$tmp"; then'''
new = '''if normalized=$(printf '%s' "$raw" | python3 "$LIB_DIR/checkin-contract.py" --checkin-id "$checkin_id") && printf '%s' "$normalized" > "$tmp"; then'''
assert t.count(old) == 1; p.write_text(t.replace(old, new))     # the record-refusal-in-substitution mutant (Task 7 step 2): only the REFUSAL path moves into a substitution -- the success path still fills $tmp (cycle-7 B5)
PY
bash lib/validate-bot-change.sh > "$OUT/vbc-negctl.txt" 2>&1; echo "rc=$?"; grep -E 'checkin:' "$OUT/vbc-negctl.txt"
git checkout -- lib/checkin-record.sh; git diff --quiet -- lib/checkin-record.sh && echo "restored"
```
Expected: in `vbc-negctl.txt` the refusal lands a `script_error` row named `checkin-record.sh` and exactly one `checkin:` line reads **FAIL** — the trap line; the other five still PASS, because the mutant leaves the success path intact (measured on a scratch plane: the mutant's refusal writes exactly that row) — then `restored`. **`vbc.txt` and `vbc-negctl.txt` go into the PR body** (Task 7 step 3b).

- [ ] **Step 3: Commit**

```bash
git add lib/validate-bot-change.sh
printf '%s\n' 'test(harness): the check-in record lands and the read door joins it' '' 'validate-bot-change.sh gains the chunk-1 scenario: record -> row under a manager' 'identity env (id checked with the contract regex; a malformed decision refused on' 'purpose; zero script_error rows keyed on detail.data.script, proven able to fail' 'by a negative control); read' 'door lists it (positive control first, gated on a non-empty id); --bot --last' 'excludes the other manager (a real negative). Uses the harness short socket' 'path; $ROOT/lib linked around the CLI calls like #1481.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c5.txt"
git commit -q -F "$TMPDIR/c5.txt" && git log --oneline -1
```

---

### Task 6: `README.md`, `CLAUDE.md`, `CHANGELOG.md`, the observability guide

**Files:** `README.md:145-146`; `CLAUDE.md` (the `lib/` table; the `commands/` line at `:471`; `## Key Commands` → `# Operations`); `CHANGELOG.md` (`[Unreleased]`, `:7`); `documentation/guides/observability.md` (the question→door table, `:10-23`, three columns).

**Interfaces:** consumes the landed files of Tasks 1–4; every number below is measured in the same breath as it is written (`counts-are-pasted-never-recalled`).

- [ ] **Step 1: README counts** — `tests/test_readme_library_counts.py` pins them and its rule for `lib/` **excludes `.py`** (`:17-19`), so `checkin-contract.py` does not count. Measure with the test's own functions, then edit `README.md:145-146`:

```bash
./.venv/bin/python -c "from tests.test_readme_library_counts import _bash_scripts, _md_members; print('lib', len(_bash_scripts()), 'protocols', len(_md_members('protocols')))"
ls -d library/skills/*/ | wc -l
```
Expected: `lib 92 protocols 39` and `54`. Then `53 skills` → `54 skills`; `91 bash lifecycle scripts` → `92 bash lifecycle scripts` (protocols stay 39 — the protocol is chunk 2's). Run: `./.venv/bin/pytest tests/test_readme_library_counts.py -q` → pass. (A second in-flight branch edits the same README line — merge conflicts here are resolved by re-measuring, never by arithmetic.)

- [ ] **Step 2: The lib rows** — append to the `lib/` table in `CLAUDE.md`, in the house style:

```markdown
| `checkin-record.sh` | THE write door for a manager check-in decision (manager check-in spec §7). Mints the `ck_` id through `plane_mint_id` (the one mint), validates the schema-1 record through `checkin-contract.py`, lands ONE actor-anchored `checkin_decision` system event stamped `source_ref checkin:<id>` — the task-recheck stamp idiom, so a reader joins on the ref. **For this door the record IS the action**: rc 3 when the plane did not record or is silenced (never a silent 0), rc 2 when the contract refuses (every reason named), rc 1 for usage including a missing identity — the `task-act.sh` ladder. The contract runs as a top-level pipeline into a file, never in a command substitution: `install_error_trap`'s ERR trap fires inside a substitution whatever surrounds it and would land a `critical` `script_error` row for a decision the door says it did not record. Identity is `BOT_ID` + `FLEET_NAME` from the environment, never `BOT_NAME` (a display field) and never a flag (nothing supplies one); `--dry-run` validates and prints `DRY-RUN <id>` |
| `checkin-contract.py` | The schema-1 decision record, stdlib (the `dispatch-overdue.py` precedent): `normalize()` lists EVERY defect rather than the first; keeps a count `null` when the manager could not measure it (never collapsed to 0 — an unchanged delta is the skill's argument for silence) and refuses a MISSING count rather than defaulting it; requires `prev_checkin_id` (null = no previous) and `raise.reason` in both directions; carries the losers in `inputs_seen.considered` — non-empty on `dispatch` — and the raw backlog count `issues_seen` beside the filtered `issues_considered`, because a selector is judged by what it did NOT pick and a broken filter must not look like an empty backlog (`sprint-selection-record.py`, #974). Actions this chunk: `dispatch \| ask \| nothing`; `propose`/`sprint` widen the enum as schema 2. Also the CLI filter the door pipes through |
```

and one sentence each in the existing `dispatch-task.sh` and `tg-post.sh` rows: ``Since the check-in chunk: `--project <key>` stamps `project_key` on the work item and opens the envelope; `--checkin ck_<32hex>` appends a `checkin_dispatch` join row to the SAME batch as the assignment (a strict `Assignment` payload cannot carry the id; `task_id` null on an id-less tracked send), disclosed and ignored on an untracked dispatch.`` / ``Its sender alias is `bot:$FLEET_NAME/${BOT_ID:-$BOT_NAME}` — the id where a session exports one (the alias every plane door anchors on), the name where a hand caller such as `bot-sweep-cron.sh` sets only that; it was `BOT_NAME` alone, which silently broke any alias join for a renamed bot. `keepalive.sh:102`'s heartbeat subject is the remaining `BOT_NAME` alias, moved with chunk 2.``

- [ ] **Step 3: The command and the guide** — in the package structure's `commands/` line (`CLAUDE.md:471`) add `checkins` to the list and change `(14 files)` → `(15 files)`; under `## Key Commands` → `# Operations`:

```bash
claudlobby checkins [--fleet F] [--bot B] [--since 7d] [--last] [--raised] [--json]   # the manager check-in's decisions, newest first (plane read; --fleet where the root manifest names none)
```

In `documentation/guides/observability.md`'s question→door table (three columns: Question · Where to look · Command) add:

```markdown
| What did a manager decide at its last check-in, and why? | The plane (the check-in's `checkin_decision` rows) | `claudlobby checkins --bot <b> --last` |
```

- [ ] **Step 4: CHANGELOG** — under `[Unreleased]`, one bullet per landed piece (the record door + contract; `dispatch-task --project/--checkin`; the `tg-post.sh` alias; `checkins`; the skill).

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md CHANGELOG.md documentation/guides/observability.md
printf '%s\n' 'docs: README counts, CLAUDE.md rows, CHANGELOG and the observability guide for the check-in record and run (chunk 1)' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c6.txt"
git commit -q -F "$TMPDIR/c6.txt" && git log --oneline -1
```

---

### Task 7: The gauntlet, the merge, the inert deploy

The mechanical gauntlet: nothing merges without all of it, and the PR body cites each observation — claimed evidence is not evidence. **This PR deploys inert**: no fleet declares the skill, and the runtime gate for the skill text (its dry run on the canary manager, then the first automated check-ins) runs ONCE at the end of the series, in PR 2's deploy task, after the trigger and the protocol have landed. No unmerged code ever runs on the live host (cycle-2 R4), and no pre-merge instrument on the estate can run a model against skill text at all: `validate-bot-change.sh` boots a STUBBED `claude`.

**Files:** none beyond step 1's fold commits (`/simplify` → `/review-work` → `/verify-completion`); the merge and the deploy touch no working tree.

**Interfaces:** consumes `$OUT/env.sh` (Task 0's `$WT`, `$OUT`, the three host facts and the `no_names` gate; sourcing it enters the worktree), `$OUT` (`before.txt`, `run_before.txt`, `vbc.txt`, `vbc-negctl.txt`) and every commit of Tasks 1–6; produces `$OUT/mut-ck1-defs.py`, `mut-progress.txt`, `mutants.md`, `run_after.txt` + `after.txt`, `collect-*.txt`, `pr-body.md`, `squash-body.md`, `pr-url.txt`, `deploy.md`, `deploy-comment.md`, the PR, the merge and the inert deploy. Shell state does not survive between the executor's tool calls, let alone sessions (cycle-7 B4, measured), so EVERY block below begins by sourcing `$OUT/env.sh`; the head block also checks what the task consumes:

```bash
. "$HOME/Projects/claudlobby-worktrees/ck1-out/env.sh"; cd "$WT"      # Task 0's facts and the no_names gate; nothing else survives between sessions
ls "$OUT/before.txt" "$OUT/run_before.txt" "$OUT/vbc.txt" "$OUT/vbc-negctl.txt" > /dev/null || { echo "missing evidence files -- Task 0 or Task 5 did not complete"; exit 1; }
git status --porcelain | grep -q . && { echo "dirty tree -- commit first; the mutant driver restores with git checkout"; exit 1; }
git log --oneline -1
```

- [ ] **Step 1: Review lenses** — `/simplify`, then `/review-work`, then `/verify-completion` on the branch (the phase-finalization gate). Fold every finding as its own commit; re-run the touched test files.

- [ ] **Step 2: Committed-code mutants, from `$WT`** — never against uncommitted code: the driver refuses a dirty tree, because it restores with `git checkout --`. For each: apply, run the named tests, expect pytest rc 1 (tests failed — rc 2/4/5/127 are INVALID RUNS, never kills), restore. Every anchor must occur exactly once; a surviving mutant is a missing test — add the test, never a weaker mutant. Bash-door tests unsandboxed.

```python
# $OUT/mut-ck1-defs.py — (name, file, old, new, [killing test files]) + the driver
MUTANTS = [
    ("rationale-cap-off", "lib/checkin-contract.py",
     "return isinstance(v, str) and v.strip() != \"\" and len(v) <= TEXT_MAX", "return isinstance(v, str) and v.strip() != \"\"",
     ["tests/test_checkin_contract.py"]),
    ("null-delta-collapsed", "lib/checkin-contract.py",
     "v = delta[k]\n        if v is not None and not _count(v):", "v = delta[k] or 0\n        if not _count(v):",
     ["tests/test_checkin_contract.py"]),
    ("missing-delta-defaults-to-null", "lib/checkin-contract.py",
     "if k not in delta:\n            bad.append(f\"delta.{k} required", "if k not in delta:\n            delta[k] = None\n        if False:\n            bad.append(f\"delta.{k} required",
     ["tests/test_checkin_contract.py"]),
    ("null-input-collapsed", "lib/checkin-contract.py",
     "v = seen[k]\n        if v is not None and not _count(v):", "v = seen[k] or 0\n        if not _count(v):",
     ["tests/test_checkin_contract.py"]),
    ("missing-input-defaults-to-zero", "lib/checkin-contract.py",
     "if k not in seen:\n            bad.append(f\"inputs_seen.{k} required (a non-negative integer", "if k not in seen:\n            seen[k] = 0\n        if False:\n            bad.append(f\"inputs_seen.{k} required (a non-negative integer",
     ["tests/test_checkin_contract.py"]),
    ("prev-optional", "lib/checkin-contract.py",
     "if \"prev_checkin_id\" not in obj:", "if False:", ["tests/test_checkin_contract.py"]),
    ("losers-optional-on-dispatch", "lib/checkin-contract.py",
     "if action == \"dispatch\" and not out[\"inputs_seen\"][\"considered\"]:", "if False:", ["tests/test_checkin_contract.py"]),
    ("losers-optional-on-nothing", "lib/checkin-contract.py",
     "if action == \"nothing\" and isinstance(seen_n, int) and seen_n > 0 and not out[\"inputs_seen\"][\"considered\"]:", "if False:",
     ["tests/test_checkin_contract.py"]),
    ("lists-default-when-missing", "lib/checkin-contract.py",
     "        if k not in seen:\n            bad.append(f\"inputs_seen.{k} required (a list, empty allowed; a missing list is a defect, not an empty one)\")\n            out[\"inputs_seen\"][k] = []\n            continue\n        v = seen[k]",
     "        v = seen.get(k, [])", ["tests/test_checkin_contract.py"]),
    ("raised-on-non-ask", "lib/checkin-contract.py",
     "if decided is True and action != \"ask\":", "if False:", ["tests/test_checkin_contract.py"]),
    ("mission-unavailable-count-kept", "lib/checkin-contract.py",
     "if \"mission\" in out[\"inputs_seen\"][\"unavailable\"] and out[\"inputs_seen\"][\"issues_considered\"] is not None:", "if False:",
     ["tests/test_checkin_contract.py"]),
    ("checkins-unavailable-prev-kept", "lib/checkin-contract.py",
     "if \"checkins\" in out[\"inputs_seen\"][\"unavailable\"] and out.get(\"prev_checkin_id\") is not None:", "if False:",
     ["tests/test_checkin_contract.py"]),
    ("considered-uncapped", "lib/checkin-contract.py",
     "return (isinstance(v, list) and len(v) <= LIST_MAX", "return (isinstance(v, list)",
     ["tests/test_checkin_contract.py"]),
    ("contract-mints", "lib/checkin-contract.py",
     "if not cid:\n        bad.append(\"checkin_id required (the door mints it: plane_mint_id ck)\")",
     "if not cid:\n        cid = \"ck_\" + \"f\" * 32",
     ["tests/test_checkin_contract.py"]),
    ("record-swallows-failure", "lib/checkin-record.sh",
     "if [ \"${PLANE_EMIT_LAST_RC:-0}\" -ne 0 ]; then", "if false; then", ["tests/test_checkin_doors.py"]),
    ("record-uses-bot-name", "lib/checkin-record.sh",
     "BOT=\"${BOT_ID:-}\"", "BOT=\"${BOT_NAME:-${BOT_ID:-}}\"", ["tests/test_checkin_doors.py"]),
    ("record-refusal-in-substitution", "lib/checkin-record.sh",
     "if printf '%s' \"$raw\" | python3 \"$LIB_DIR/checkin-contract.py\" --checkin-id \"$checkin_id\" > \"$tmp\"; then",
     "if normalized=$(printf '%s' \"$raw\" | python3 \"$LIB_DIR/checkin-contract.py\" --checkin-id \"$checkin_id\") && printf '%s' \"$normalized\" > \"$tmp\"; then",
     ["tests/test_checkin_doors.py"]),
    ("dry-run-unmarked", "lib/checkin-record.sh",
     "printf 'DRY-RUN %s\\n' \"$checkin_id\"", "printf '%s\\n' \"$checkin_id\"", ["tests/test_checkin_doors.py"]),
    ("project-never-opens-gate", "lib/dispatch-task.sh",
     "|| [ -n \"$DISPATCH_WORKSTREAM\" ] || [ -n \"$DISPATCH_PROJECT\" ]; then", "|| [ -n \"$DISPATCH_WORKSTREAM\" ]; then",
     ["tests/test_checkin_doors.py"]),
    ("join-row-dropped", "lib/dispatch-task.sh",
     "\"${sup_ev:+,$sup_ev}\" \"${ck_ev:+,$ck_ev}\"", "\"${sup_ev:+,$sup_ev}\" \"\"", ["tests/test_checkin_doors.py"]),
    ("join-dropped-silently", "lib/dispatch-task.sh",
     "echo \"dispatch-task: --checkin ignored: a $DISPATCH_TYPE dispatch mints no assignment to join\" >&2", ":",
     ["tests/test_checkin_doors.py"]),
    ("join-task-id-empty-string", "lib/dispatch-task.sh",
     "ck_tid=\"null\"", "ck_tid=\"\\\"\\\"\"", ["tests/test_checkin_doors.py"]),
    ("join-unreachable-as-absent", "lib/dispatch-task.sh",
     "echo \"dispatch-task: --checkin $DISPATCH_CHECKIN not verified -- the plane could not answer (no CLAUDLOBBY_ROOT, or the db is unreachable); the join is recorded as given\" >&2", ":",
     ["tests/test_checkin_doors.py"]),
    ("join-lookup-silent", "lib/dispatch-task.sh",
     "echo \"dispatch-task: --checkin $DISPATCH_CHECKIN names no checkin_decision the plane can see (spooled, or mis-copied from the record door?) -- the join is recorded as given; verify with claudlobby checkins --last\" >&2", ":",
     ["tests/test_checkin_doors.py"]),
    ("tg-post-bot-name", "lib/tg-post.sh",
     "--arg sender \"bot:$FLEET_NAME/${BOT_ID:-$BOT_NAME}\"", "--arg sender \"bot:$FLEET_NAME/$BOT_NAME\"", ["tests/test_checkin_doors.py"]),
    ("rows-by-ingest-order", "claudlobby/plane/queries.py",
     "ORDER BY {_epoch('e.occurred_at')} DESC, e.ingest_seq DESC", "ORDER BY e.ingest_seq DESC", ["tests/test_checkins_cli.py"]),
    ("last-bounded-by-since", "claudlobby/commands/checkins.py",
     "since = None if args.last else _since(args.since)", "since = _since(args.since)", ["tests/test_checkins_cli.py"]),
    ("truncated-rows-dropped", "claudlobby/plane/queries.py",
     "\" WHERE e.kind = 'system' AND e.event = 'checkin_decision'\"", "\" WHERE e.kind = 'system' AND e.event = 'checkin_decision' AND e.detail_truncated = 0\"",
     ["tests/test_checkins_cli.py"]),
    ("dataless-row-crashes", "claudlobby/commands/checkins.py",
     "rec = json.loads(detail) if detail and not truncated else None", "rec = json.loads(detail) if not truncated else None",
     ["tests/test_checkins_cli.py"]),
    ("cap-undisclosed", "claudlobby/commands/checkins.py",
     "if len(rows) > TEXT_ROW_LIMIT:", "if False:", ["tests/test_checkins_cli.py"]),
    ("raised-filter-off", "claudlobby/commands/checkins.py",
     "if raised and not row[\"raise\"][\"decided\"]:", "if False:", ["tests/test_checkins_cli.py"]),
    ("claudron-wildcard-sneaks-in", "library/skills/checkin/SKILL.md",
     "  - \"Bash(claudron lookup *)\"\n", "  - \"Bash(claudron *)\"\n", ["tests/test_checkin_library.py"]),
    ("record-grant-needs-a-space", "library/skills/checkin/SKILL.md",
     "  - \"Bash(*checkin-record.sh*)\"\n", "  - \"Bash(*checkin-record.sh *)\"\n", ["tests/test_checkin_library.py"]),
    ("degraded-any-field", "library/skills/checkin/SKILL.md",
     "an entry whose `mode` is `omitted` and whose", "an entry of either mode whose", ["tests/test_checkin_library.py"]),
    ("grant-widened-to-star", "library/skills/checkin/SKILL.md",
     "  - \"Bash(claudlobby checkins *)\"\n", "  - \"Bash(*)\"\n", ["tests/test_checkin_library.py"]),
    ("grant-widened-to-prefix", "library/skills/checkin/SKILL.md",
     "  - \"Bash(claudlobby checkins *)\"\n", "  - \"Bash(claudlobby c*)\"\n", ["tests/test_checkin_library.py"]),
]

if __name__ == "__main__":
    # THE DRIVER (cycle-4 R3, cycle-5 R7): refuse a dirty tree, apply, run the named
    # tests, restore from git, print the PR table on stdout (progress on stderr).
    # killed <=> pytest rc 1 (tests failed); rc 0 = SURVIVED; anything else is an
    # INVALID RUN (2 collection error, 4 bad path, 5 nothing collected, 127 no venv)
    # and is never evidence. Run from $WT on COMMITTED code only.
    import pathlib, subprocess, sys
    dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, check=True).stdout.strip()
    assert not dirty, "the tree must be clean: mutants run against COMMITTED code and restore with git checkout --"
    # the GREEN precondition (cycle-7 R5): a kill by a test that already fails on the committed tip is not a kill
    files = sorted({t for *_, tests in MUTANTS for t in tests})
    green = subprocess.run(["./.venv/bin/python", "-m", "pytest", *files, "-q", "-p", "no:cacheprovider"], capture_output=True, text=True).returncode
    assert green == 0, f"the killing tests must pass on the committed tip first (pytest rc {green}); fix the tree, never the driver"
    print(f"green: the {len(files)} killing files pass on the committed tip", file=sys.stderr, flush=True)
    rows = []
    for name, f, old, new, tests in MUTANTS:
        path = pathlib.Path(f); orig = path.read_text()
        assert orig.count(old) == 1, f"{name}: anchor occurs {orig.count(old)}x in {f}"
        path.write_text(orig.replace(old, new))
        rc = None
        try:
            rc = subprocess.run(["./.venv/bin/python", "-m", "pytest", *tests, "-q", "-x", "-p", "no:cacheprovider"],
                                capture_output=True, text=True).returncode
        finally:
            subprocess.run(["git", "checkout", "--", f], check=True)
        assert path.read_text() == orig, f"{name}: restore failed"
        verdict = "killed" if rc == 1 else ("SURVIVED" if rc == 0 else f"INVALID RUN (pytest rc {rc})")
        rows.append(f"| `{name}` | `{f}` | {', '.join(tests)} | {verdict} |")
        print(rows[-1], file=sys.stderr, flush=True)
    print("\n".join(["| mutant | file | killing tests | result |", "|---|---|---|---|", *rows]))
    bad = [row for row in rows if "| killed |" not in row]
    assert not bad, "a surviving mutant is a missing test (add the test, never a weaker mutant); an INVALID RUN is not evidence"
```
Thirty-six mutants. **Write the block above to `$OUT/mut-ck1-defs.py` byte-for-byte** (a Write of the fenced content — `$OUT` is outside the repo, so nothing is committed), then run it, never through a pipe (the driver's final assertion IS the gate's verdict; it refuses first when the killing files are not green on the committed tip):

```bash
. "$HOME/Projects/claudlobby-worktrees/ck1-out/env.sh"; cd "$WT"
./.venv/bin/python "$OUT/mut-ck1-defs.py" > "$OUT/mutants.md" 2> "$OUT/mut-progress.txt"; echo "rc=$?"; head -1 "$OUT/mut-progress.txt"; cat "$OUT/mutants.md"
```
Expected (unsandboxed): `rc=0`, the progress file's first line `green: the N killing files pass on the committed tip`, and a 36-row table with every result `killed`; `$OUT/mutants.md` IS the PR body's mutant table. `record-refusal-in-substitution` is the cycle-3 B2 pin — it survives on the stub rig (whose `install_error_trap` is a no-op) and must die on the real rig's refusal test, which is why that test exists; `join-lookup-silent` / `join-unreachable-as-absent` pin cycle-4 B5 and cycle-5 R1; `grant-widened-to-star` dies on `FORBIDDEN` and `grant-widened-to-prefix` on the grant-shape assertion (cycle-5 gap); `raised-filter-off` pins cycle-5 R5.

- [ ] **Step 3: The two-leg full-suite gate** — `before.txt` is Task 0's; the after leg runs on the FINAL committed tip, unsandboxed:

```bash
. "$HOME/Projects/claudlobby-worktrees/ck1-out/env.sh"
./.venv/bin/pytest --tb=no -ra > "$OUT/run_after.txt" 2>&1; echo "rc=$?"
awk "/short test summary info/,0" "$OUT/run_after.txt" | grep -E "^(FAILED|ERROR)" | sed 's/ - .*//' | sort -u > "$OUT/after.txt"
comm -13 "$OUT/before.txt" "$OUT/after.txt"     # failures YOU introduced — must be empty
tail -1 "$OUT/run_before.txt"; tail -1 "$OUT/run_after.txt"
./.venv/bin/pytest --collect-only -q tests/test_checkin_contract.py tests/test_checkin_doors.py tests/test_checkins_cli.py tests/test_checkin_library.py > "$OUT/collect-new.txt"; echo "rc=$?"; tail -1 "$OUT/collect-new.txt"     # the four new files' count, measured (rc 0; redirected, never piped)
./.venv/bin/pytest --collect-only -q tests/test_bash_parse.py -k checkin-record > "$OUT/collect-parse.txt"; echo "rc=$?"; tail -1 "$OUT/collect-parse.txt"                    # + the NUMERATOR of "1/N tests collected": the parse case
./.venv/bin/pytest --collect-only -q tests/test_no_dead_session_command.py -k checkin-record > "$OUT/collect-dead.txt"; echo "rc=$?"; tail -1 "$OUT/collect-dead.txt"       # + its numerator: the second suite parametrized over LIB_SCRIPTS
```
Both rc must be 1 (the red baseline). Known load flakes on this host: `test_boot_capture.sh:203 (dur=1)` and the `test_github_app_wrapper` refresh loop — re-run a flake alone before calling it a regression. The after leg's `passed` must equal before's plus the four-file count plus the two numerators (the `-k` lines print `1/N tests collected (N-1 deselected)` — add the 1, never the N). `tests/test_bash_parse.py:24` builds `LIB_SCRIPTS` from `lib/*.sh` and `tests/test_no_dead_session_command.py:23,44` parametrizes over the same list, so the new `.sh` lands one case in each; nothing parametrizes over `library/skills/*` or `library/protocols/*` (measured).

- [ ] **Step 3b: Assemble the PR body and the squash body from the evidence directory, behind the identifier gate** (cycle-5 B1: two `gh` calls read a file nothing wrote; cycle-6 B1: a file with the fleet's name in it was pasted into a public body)

```bash
. "$HOME/Projects/claudlobby-worktrees/ck1-out/env.sh"
{
cat <<'HDR'
## What landed (chunk 1 of the manager check-in — spec `documentation/plans/2026-09-13-manager-checkin-design.md` §12.1)

- Task 1 — `lib/checkin-contract.py` (schema 1: the losers, the raw backlog count, null = could not measure, a missing count is a defect) and `lib/checkin-record.sh` (one actor-anchored `checkin_decision`, form-D contract call, `DRY-RUN <id>`); two severity lines.
- Task 2 — `dispatch-task.sh --project` (opens the envelope, stamps `project_key`) and `--checkin` (the `checkin_dispatch` join in the same batch, the id looked up through `plane-lookup.py --checkin-id` beside the supersede lookup, disclosed when the plane cannot see it or cannot answer); `tg-post.sh` sender `${BOT_ID:-$BOT_NAME}`.
- Task 3 — `claudlobby checkins` (`--last`, `--bot`, `--since`, `--raised`, `--json`; rc 3 when the plane cannot answer).
- Task 4 — `library/skills/checkin/SKILL.md` (READ 0–5 → DECIDE → RECORD before ACT, a dispatch riding the record call by `&&`; verb-scoped grants; the protocol is chunk 2's).
- Task 5 — the `validate-bot-change.sh` scenario (record, refusal, read door, negative control).
- Task 6 — README counts, CLAUDE.md rows, CHANGELOG, the observability guide.

## Empirical observations

Harness (`lib/validate-bot-change.sh`, unsandboxed), then the negative control with the `record-refusal-in-substitution` mutant applied:
HDR
echo '```'; grep -E 'checkin:' "$OUT/vbc.txt"; echo '--- negative control (mutant applied): ---'; grep -E 'checkin:' "$OUT/vbc-negctl.txt"; echo '```'
cat <<'MID'
Real-plane tests: `test_the_decision_lands_on_a_real_plane`, `test_a_refused_decision_leaves_no_row_at_all_on_a_real_plane`, `test_dispatch_checkin_appends_the_join_row_to_the_same_batch`, `test_plane_lookup_answers_a_checkin_id`.

## Two-leg full-suite gate (names + counts)
MID
echo '```'; echo "introduced (comm -13, must be empty):"; comm -13 "$OUT/before.txt" "$OUT/after.txt"; echo "before: $(tail -1 "$OUT/run_before.txt")"; echo "after:  $(tail -1 "$OUT/run_after.txt")"; echo '```'
echo; echo "## Mutants (the driver's table, rc 0)"; echo; cat "$OUT/mutants.md"
cat <<'TAIL'

## Rollout posture

`lib/dispatch-task.sh`, `lib/plane-lookup.py` and `lib/tg-post.sh` reach every fleet on pull. The dispatch flags and the lookup mode are additive and behaviour without them is byte-identical, pinned by `tests/test_task_id_dispatch.py` + `tests/test_dispatch_task.sh`. The `tg-post.sh` sender alias moves on pull only for bots whose display name differs from their id (0 of 18 on this host, measured 2026-09-15); where it is 0 the change is inert. This PR deploys inert; the first automated check-in on the canary manager, at the end of the series (PR 2's deploy), is the positive control. The `canary-rollout` protocol governs the series' one deploy (one production manager for a day before the default in PR 4); no A/B verdict gates any PR — the operator ruled the loop is built to the end on belief, with mechanical gates only (2026-09-16).

Spec: `documentation/plans/2026-09-13-manager-checkin-design.md` (§7, §11, §12); plan: `documentation/plans/2026-09-14-manager-checkin-chunk1-contract.md`. Forks F1–F5 locked (see the plan's §Decision Forks).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
TAIL
} > "$OUT/pr-body.md"
{
cat <<'SQ'
feat(checkin): chunk 1 — the record and the run (contract, record door, dispatch join, read door, skill)

The schema-1 decision record and its door; dispatch-task --project/--checkin
with the join looked up and disclosed; plane-lookup --checkin-id; the tg-post
sender alias on BOT_ID; claudlobby checkins; the /checkin skill (the protocol
lands with the trigger); the harness scenario. Evidence (harness lines, the two-leg gate, the mutant
table, the baselines) is in the PR description.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
SQ
} > "$OUT/squash-body.md"
no_names "$OUT/pr-body.md"; no_names "$OUT/squash-body.md"
[ "$(grep -c 'checkin:' "$OUT/pr-body.md")" = 12 ] && [ "$(grep -c 'FAIL' "$OUT/pr-body.md")" = 1 ] || { echo "STOP: the PR body does not carry six PASS lines twice and the one negative-control FAIL -- re-run Task 5 step 2 (an instruction to run a check is not a check; cycle-8)"; exit 1; }
wc -l "$OUT/pr-body.md"
```
Expected: `identifiers in pr-body.md: clean`, `identifiers in squash-body.md: clean`, then the line count — the twelve `checkin:` lines and the one `FAIL` are gated, not eyeballed. The squash body is a summary with the `Co-Authored-By` trailer (Global Constraints — the evidence dump stays in the PR description, never in permanent history).

- [ ] **Step 4: Push, open the PR, CI on Linux**

```bash
. "$HOME/Projects/claudlobby-worktrees/ck1-out/env.sh"
no_names "$OUT/pr-body.md"      # the gate runs in THIS block, beside the door that publishes (cycle 9)
git push -u origin checkin/chunk1-record
gh pr create --head checkin/chunk1-record --title "feat(checkin): chunk 1 — the record and the run (contract, record door, dispatch join, read door, skill)" --body-file "$OUT/pr-body.md" > "$OUT/pr-url.txt"; echo "rc=$?"; cat "$OUT/pr-url.txt"     # --head: a wrong-cwd run refuses instead of publishing for another branch (cycle-8 B1)
```
The PR body is `$OUT/pr-body.md` from step 3b — the tasks, the harness lines of both runs, the real-plane test names, the two-leg gate, the driver's mutant table, the baselines and the rollout posture, ending with the attribution line. Wait for CI green; a `test_boot_capture.sh` load flake is re-run, not waved through.

- [ ] **Step 5: Admin-merge with the explicit squash body** (the operator's standing authorization for gauntleted work) — `. "$HOME/Projects/claudlobby-worktrees/ck1-out/env.sh"; no_names "$OUT/squash-body.md" && gh pr merge --squash --admin --body-file "$OUT/squash-body.md" "$(cat "$OUT/pr-url.txt")"`; delete the branch (the gate runs beside the door that publishes — cycle 9).

- [ ] **Step 6: Deploy to the Mini: pull, then validate every fleet**

Chunk 1 composes nothing differently for any bot that does not declare `checkin`; `lib/` and the `checkins` subcommand are live on pull. Fleets are enumerated the way `lib/setup-fleets:18` does (flat or nested); nothing is piped; the canary name rides the ssh command string as in Task 0. **The live host already carries composed drift** (measured read-only this cycle: `diff` rc 0 with 90 lines on each fleet before any pull), so the expectation is *unchanged*, not *empty*: the loop runs before and after the pull and the two readings are compared.

```bash
. "$HOME/Projects/claudlobby-worktrees/ck1-out/env.sh"
ssh -o BatchMode=yes mini "CK_FLEET=$CK_FLEET MINI_ROOT=$MINI_ROOT bash -s" <<'EOF' > "$OUT/deploy.md"
export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH
cd "$MINI_ROOT" || exit 1
git status --porcelain | grep -q . && { echo "dirty checkout -- stop"; exit 1; }
sweep() {   # <tag>: validate rc, checkin findings, diff rc + line count, per fleet
  for fy in local/*/fleet.yaml local/*/*/fleet.yaml; do
    [ -f "$fy" ] || continue
    fleet=$(basename "$(dirname "$fy")")
    .venv/bin/claudlobby --fleet "$fleet" validate > "/tmp/ck-validate-$1-$fleet.out" 2>&1; echo "$1 validate $fleet rc=$?"
    grep -ci 'checkin' "/tmp/ck-validate-$1-$fleet.out" | sed "s/^/$1 checkin findings $fleet: /"
    .venv/bin/claudlobby --fleet "$fleet" diff > "/tmp/ck-diff-$1-$fleet.out" 2>&1; echo "$1 diff $fleet rc=$? lines=$(wc -l < "/tmp/ck-diff-$1-$fleet.out" | tr -d ' ')"
  done
}
sweep before
git fetch -q origin main && git pull --ff-only && git log --oneline -1
sweep after
time .venv/bin/claudlobby --root "$MINI_ROOT" checkins --fleet "$CK_FLEET"; echo "checkins rc=$?"
EOF
echo "ssh rc=$?"; cat "$OUT/deploy.md"
```
Expected: for every fleet the `after` lines equal the `before` lines — same `validate` rc, `checkin findings` 0 both times, same `diff` rc and line count (a fleet with existing drift keeps exactly that drift; nothing composes differently yet — a composed-protocol edit would show here as a diff delta on every fleet whose manager declares it, which is why chunk 1 ships none — cycle 9) — and `claudlobby checkins` printing `no check-ins — fleet …` at rc 0 in tens of milliseconds (measured read-only this cycle: 14 ms over 34,916 `system` rows — `SEARCH USING INDEX idx_events_kind_seq` plus a temp B-tree for the ORDER BY; no index needed now). rc 2 with argparse's `invalid choice: 'checkins'` means this install predates the subcommand — the pull did not land, check `git log --oneline -1` there; rc 3 is `refuse_unreachable`: the plane cannot answer for that fleet, and the printed note says why.

- [ ] **Step 7: Record the inert deploy on the PR, behind the identifier gate**

The sweep in step 6 prints fleet names, so its file never leaves `$OUT`; the comment carries counts. The runtime gate for the skill text is PR 2's deploy task.

```bash
. "$HOME/Projects/claudlobby-worktrees/ck1-out/env.sh"
ls "$OUT/deploy.md" "$OUT/pr-url.txt" > /dev/null || { echo "STOP: step 6 or step 4 did not complete"; exit 1; }
{
echo "## Deployed inert (PR 1 of the check-in series)"; echo
echo "Pulled to the host at $(grep -m1 -E '^[0-9a-f]{7,} ' "$OUT/deploy.md" | cut -c1-7); no fleet declares the skill yet."
echo "- fleets validated: $(grep -c '^before validate .* rc=0' "$OUT/deploy.md") before, $(grep -c '^after validate .* rc=0' "$OUT/deploy.md") after (rc 0)"
echo "- checkin findings: $(grep '^before checkin findings' "$OUT/deploy.md" | awk -F': ' '{s+=$2} END {print s+0}') before, $(grep '^after checkin findings' "$OUT/deploy.md" | awk -F': ' '{s+=$2} END {print s+0}') after"
echo "- composed drift unchanged on every fleet: $([ "$(grep '^before diff' "$OUT/deploy.md" | sed 's/^before //')" = "$(grep '^after diff' "$OUT/deploy.md" | sed 's/^after //')" ] && echo yes || echo NO -- read deploy.md)"     # a string compare: process substitution is refused under the executor's sandbox
echo "- \`claudlobby checkins\` on the canary fleet: $(grep -m1 'checkins rc=' "$OUT/deploy.md")"
echo; echo "The runtime gate for the skill (its dry run, then the first automated check-ins) runs at the end of the series, in PR 2's deploy."
} > "$OUT/deploy-comment.md"
no_names "$OUT/deploy-comment.md"
gh pr comment "$(cat "$OUT/pr-url.txt")" --body-file "$OUT/deploy-comment.md"
```
Expected: `identifiers in deploy-comment.md: clean`, the comment URL. That comment closes this PR; PR 2 (the trigger and the protocol) follows.

---

## Self-review (after the build-to-the-end re-cut, 2026-09-16)

**What changed and what did not.** The code and its gates are untouched: Tasks 1–6, the harness scenario, the 36 mutants, the two-leg suite gate, the PR body behind the identifier gate. Removed: Task 0's baselines (the T0 instrument, the control fleet, the noise floor, the observed-worker STOP), Task 7's equip/dry-run/real-run/deploy-comment steps and their `verdict.txt`, the `CK_CONTROL`/`CK_NO_CONTROL`/`CK_BEAT` inputs, the `baseline_read` reader, forks F6/F7 (superseded), the contaminated-comparison Risks row, the interim raw-SQL doc row (PR 3 ships the door), one dead test assertion. `$OUT/env.sh` keeps the three host facts, the `no_names` gate and the `cd` into the worktree. Task 7 ends at the merge, the inert deploy and one counted comment; the runtime gate for the skill text moves to PR 2's deploy, where the trigger and the protocol exist to be exercised.

**Placeholder scan.** No TBD/TODO; every code step carries code; no `<…>` placeholder remains inside a command. Operator-supplied values are `CK_FLEET`, `CK_MGR`, `MINI_ROOT`, guarded with `${:?}` at Task 0 step 1, written once to `$OUT/env.sh` outside the repo, never literals in a committed file, and matched as whole words out of every body bound for the PR, in the same block as the command that publishes it.

**Type consistency.** `normalize(obj, *, checkin_id=None)` matches the door's `--checkin-id` and every contract test. `INPUTS` and the seven-key `delta` in the contract tests, `_decision()` in the doors tests and `ck_decision` in the harness all carry every count and `prev_checkin_id`. `checkin-record.sh` rc ladder 0/1/2/3 is identical in the header, the tests, the harness (rc 2 on the refusal), the CLAUDE.md row and the skill; `DRY-RUN` is in the header, the door, the doors test, `DOORS`, the skill and step 8. `CHECKIN_ROWS_SQL` binds `(fleet, fleet)` — matched in `collect_checkins`; its five columns are the five `_row()` reads; `collect_checkins(..., raised=)` matches `_Args.raised` and the parser's `--raised`; the protocol and the skill name the same `--since 7d --raised` read; the envelope's rows are `checkins[]` in the tests, the skill and step 9. `plane-lookup.py --checkin-id` prints the id or nothing at rc 0 and rc 3 when it cannot answer, which is what the join block's form-D read distinguishes (empty file vs nonzero rc); its two disclosure strings are the two mutant anchors. `_Args.checkins_fleet` matches `dest="checkins_fleet"`; no `limit` anywhere. `STUB_CK` matches the stub's `plane_mint_id`. The skill's door strings match `DOORS` verbatim after whitespace collapse, its three `claudlobby` grants are the three verbs it runs and pass the grant-shape assertion, and `raise_` in the CLI tests maps to `raise` in the record. `ck_tid` and `_ck_tmp` are `local`s of the emit function, read in the join only. `$OUT/pr-url.txt` is written by step 4 and read by steps 5 and 7; `$OUT/squash-body.md` and `$OUT/deploy-comment.md` are written by 3b/7 and read once each; `$OUT/deploy.md` is written by step 6 and read by step 7. `$OUT/env.sh` is written by Task 0 step 1 and sourced first by every later block (it enters the worktree and defines `no_names`); the skill's composed call, its dry-run rehearsal, `DOORS` and the extractor agree on `ck=$(bash …`, `) && claudlobby checkins …` and `--checkin "$ck"`; the contract's `INPUTS_COUNTS`/`INPUTS_LISTS`/`DELTA_COUNTS` are the library test's key pin; the four cycle-8/9 contract lines are four mutant anchors, each occurring once.
