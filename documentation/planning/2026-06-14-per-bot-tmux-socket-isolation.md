---
title: "Per-Bot tmux Socket Isolation"
type: plan
status: draft
owner: clog
tags: [runtime, supervision, tmux, reliability, dispatch]
created: 2026-06-14
updated: 2026-06-14
---

# Per-Bot tmux Socket Isolation

## Goal

Give every fleet bot its own tmux server (its own `-L <socket>`) instead of sharing one per-user server, so the death of one server can no longer drop the entire fleet's sessions at once. This hardens the dispatch substrate (`tmux send-keys`) that the manager-worker pattern depends on, directly serving the north star — **"Trivial to run a fleet of distinct, cooperating bots on cheap hardware"** (`PROJECT_MISSION.md`) — and its principle that bots are *"distinct identities … isolated state, not threads of a single account."* Today a bot's session is the one piece of its identity that is **not** isolated.

## Current State

Verified against the codebase (2026-06-14). **No script uses `-L`/`-S` anywhere** — a repo-wide grep returns zero matches; every tmux call (shell and Python) talks to the implicit default per-user socket at `/tmp/tmux-$(id -u)/default`. There is exactly one tmux server per host user.

**The failure this fixes (observed incident):** the shared tmux server died and dropped every bot's session simultaneously across all fleets on the host. Each per-bot systemd/launchd service only spawns a detached `tmux new-session -d` and then exits, so the service still reads `active` even after its session is gone. The fleet-health sweep showed every bot as `SERVICE=ok` but `SESSION=missing` at the same instant — the unmistakable signature of a single-server death rather than N independent restarts. The watchdog (`lib/keepalive.sh`) then rebuilt the whole fleet bot-by-bot over ~2 minutes. The single shared server is a fleet-wide single point of failure.

**The one-server coupling is already documented** in `lib/start-bot.sh:106-119` and in the lesson `library/lessons/migration/tmux-server-env-inheritance.md:5` ("tmux runs one server per user … every session spawned afterward inherits that frozen env"). The per-session `.tmux-env` file mechanism (`lib/start-bot.sh:121,140-142`) exists specifically to work around env bleed on that shared server.

**Key facts that shape the design:**

- **Session name = bot directory slug** (`basename`), via `tmux_session_name()` at `lib/lib-common.sh:250-252`; created at `lib/start-bot.sh:195`. (The `claude --name "<LABEL>-<date>-<HHMM>"` value seen on the process line is Claude Code's own session label — `lib/start-bot.sh:98,155` — **not** the tmux session name. Three distinct axes already exist: **unit name** = `BOT_SERVICE`, **session name** = slug, and now a proposed **socket** — keep all three separate.)
- **bot.conf is the single source of truth** that auto-propagates into the session. `compose_bot_conf()` (`claudlobby/composer.py:268-457`, written at `:1132`) emits `KEY=value` lines; `lib/start-bot.sh:140-142` re-sources bot.conf into `.tmux-env` via `set -a; . bot.conf; set +a`. **Any new var added to bot.conf reaches both the start-bot parent and the claude session with zero unit-file changes.**
- **Manager identity is already derivable at compose time.** `MANAGER_TMUX` is emitted at `claudlobby/composer.py:441-448` from `fleet.teams` (`TeamConfig{name,manager,workers}`, `config.py:230-234`). A worker gets `MANAGER_TMUX=<team.manager>`; a manager gets `MANAGER_TMUX=<self>`. (`BotConfig.reports_to`/`manages` exist at `config.py:184-185` but feed only CLAUDE.md org rendering, **not** `MANAGER_TMUX`.)
- **Unit files do not need to change.** Both systemd (`composer.py:487-519`) and launchd (`composer.py:526-556`) units pass only the bot dir to `start-bot.sh`; everything else flows through bot.conf. **One exception:** the generated systemd `ExecStop` at `composer.py:507` runs `tmux kill-session -t {bot.bot_id}` against the default socket and will silently no-op once a bot moves to a private socket.

**tmux call-site census (the work surface).** Two parallel access patterns exist; there is **no single chokepoint** today — only `check_tmux_session()` (`lib-common.sh:256`) wraps a subcommand (`has-session`); every other subcommand is shelled inline as `"$_TMUX_BIN" <cmd>`.

| Area | File:line | Subcommand | Targets |
|---|---|---|---|
| Helper | `lib-common.sh:250-252,256` | `tmux_session_name`, `has-session` | own slug |
| Create/own | `start-bot.sh:96,195,204,208,230-236` | `kill-session`,`new-session`,`has-session`,`capture-pane`,`send-keys` | own slug |
| Watchdog | `keepalive.sh:59,62,88` | `has-session`,`capture-pane` | own slug |
| Handoff | `pre-stop-handoff.sh:36,37` | `has-session`,`send-keys` | own slug |
| Bench | `bench-cold-start.sh:185,186,189,218` | `has-session`,`kill-session`,`capture-pane` | own slug |
| Mem | `fleet-memory-check.sh:125,134` | `list-panes`,`has-session` | own slug |
| **Dispatch (mgr→worker)** | `dispatch.sh:18,25,27` | `has-session`,`send-keys` | **peer (worker arg)** |
| **Report (worker→mgr)** | `report-back.sh:23,65,67` | `send-keys` | **peer (`MANAGER_TMUX`)** |
| **Pulse push (→mgr)** | `fleet-pulse.sh:66` + per-bot checks `:108,110,301` | `send-keys`,`has-session`,`capture-pane` | **peer + own slug** |
| Sprint nudge (→mgr) | `sprint-trigger.sh:20…` | `check_tmux_session`,`capture-pane`,`send-keys` | **peer (`MANAGER_TMUX`)** |
| Sweep cron | `bot-sweep-cron.sh:33,39,46,48` | `has-session`,`capture-pane`,`send-keys` (bypasses helper) | peer (arg) |
| **Fleet enumerate** | `reconcile-fleet.sh:46` (bare `tmux ls`), `:191` `kill-session` | `ls`,`kill-session` | **all sessions, one socket** |
| Test harness | `validate-bot-change.sh:37,38,56,57,76` | bare `tmux` new/capture/kill | literal `valbot`/`valmgr` |
| Python | `status.py:96` (`ls`), `doctor.py:204` (`has-session`), `commands/move_bot.py:118,124` | subprocess `["tmux",…]` | all / by id |
| **Generated unit** | `composer.py:507` | `kill-session` baked into `ExecStop` | by `bot_id`, default socket |

Distinct subcommands a socket layer must cover: `new-session`, `has-session`, `send-keys`, `capture-pane`, `kill-session`, `ls`/list-sessions, `list-panes`.

**Prior art:** `git log --grep=socket` returns **zero** commits — genuinely new ground. The directly relevant precedent is the **unit-naming unification** (`4f10b28` #394, `758c944` #395): a single bot.conf field (`BOT_SERVICE`) resolved everywhere via `bot_conf_get` (now in `lib-common.sh:468`), with a fallback for un-regenerated bots, plus `tests/test_lifecycle_names.py` asserting cross-script agreement. **This plan mirrors that template exactly**, adding socket as the third identity axis. Existing tmux-env isolation tests (`#367`) are the natural home for socket-isolation tests. Marker-based idle detection (`f6a9311` #400) is socket-agnostic and undisturbed.

**No active plan conflicts.** The nearest neighbor is the fleet-observability subsystem (`lib/fleet-pulse.sh`, `lib/bot-vitals.sh`, the `fleet-observability` protocol): it only *reads* sessions (`has-session`/`capture-pane`) and pushes to the manager. It is **complementary**, but its pulse checks read the same sessions this plan re-targets, so they must update in lockstep (the harness extension forces this).

## Architecture

Target state: each bot runs its own tmux server identified by a stable socket name carried in bot.conf.

```
            BEFORE (single server, SPOF)                 AFTER (per-bot servers)
          ┌───────────────────────────┐         ┌──────────┐ ┌──────────┐ ┌──────────┐
          │  default socket (1 server)│         │ -L mgr   │ │ -L wkr1  │ │ -L wkr2  │
          │  [mgr][wkr1][wkr2][wkr3]…  │         │ [mgr]    │ │ [wkr1]   │ │ [wkr2]   │
          └───────────────────────────┘         └──────────┘ └──────────┘ └──────────┘
          one death ⇒ all sessions gone          one death ⇒ exactly one bot down

  bot.conf gains:  TMUX_SOCKET=<own socket>   MANAGER_TMUX_SOCKET=<manager's socket>
  Resolution:      tmux_socket_for_bot() + bot_tmux() wrapper in lib-common.sh (SSOT, like BOT_SERVICE)
  Own-socket calls:    bot_tmux <cmd>                    →  tmux -L "$TMUX_SOCKET" <cmd>
  Cross-socket calls:  dispatch → worker's socket;  report/pulse/sprint → MANAGER_TMUX_SOCKET
  Fleet enumerators:   iterate every bot's socket and union (replaces single `tmux ls`)
  Generated ExecStop:  socket-aware kill (composer.py:507)
```

The socket is resolved from a single helper so every script agrees, exactly as `BOT_SERVICE` is resolved via `bot_conf_get` today.

## Phases

### Phase 1: Socket-resolution foundation (SSOT + composition)
#### 1a. lib-common.sh helpers
Add `tmux_socket_for_bot()` (bot identity → socket name, per Fork F2) and a `bot_tmux()` wrapper that prepends `-L "<socket>"` to `"$_TMUX_BIN"`. Do **not** bake `-L` into `_TMUX_BIN` itself (Fork F4). Give `check_tmux_session()` an optional socket argument (backward-compatible default).
#### 1b. composer emits socket fields
Emit `TMUX_SOCKET` unconditionally near `BOT_ID` (`composer.py:300-303`) and `MANAGER_TMUX_SOCKET` beside the existing `MANAGER_TMUX` block (`composer.py:441-448`), derived from the **same** source as `MANAGER_TMUX` (Fork F3). No unit-file change (socket flows through bot.conf).
#### 1c. precedent-style test
Add `tests/test_lifecycle_sockets.py` mirroring `test_lifecycle_names.py`: for a mock bot whose `BOT_SERVICE`/slug/socket all differ, assert every lifecycle script resolves the identical socket.
*Standalone value:* bot.conf carries the socket; helper + tests exist; **no runtime behavior changes yet** (vars emitted but unused).

### Phase 2: Own-socket lifecycle
Route every **own-slug** call through `bot_tmux`: `start-bot.sh` (`:96,195,204,208,230-236`), `keepalive.sh` (`:59,62,88`), `pre-stop-handoff.sh` (`:36,37`), `bench-cold-start.sh` (`:185,186,189,218`), `fleet-memory-check.sh` (`:125,134`), and `check_tmux_session` call sites. Fix the generated `ExecStop` (`composer.py:507`) to `tmux -L <socket> kill-session` (or `kill-server`). `spin-up-bot.sh` inherits via `start-bot.sh`.
*Standalone value:* a bot starts on its own server and is supervised on it; `keepalive` detects death on the correct socket.

### Phase 3: Cross-socket dispatch + reporting
Patch the **peer-targeting** scripts to use the peer's socket: `report-back.sh:65,67` and `fleet-pulse.sh:66` and `sprint-trigger.sh` → `MANAGER_TMUX_SOCKET`; `dispatch.sh:18,25,27` (and `dispatch-task.sh` delegate) → the **target worker's** socket (resolved from the worker identity); `bot-sweep-cron.sh:33,39,46,48` → arg's socket. Add a `has-session` precheck on the peer socket that logs on miss (send-keys to a missing target exits 0 — silent failure otherwise).
*Standalone value:* manager↔worker comms work across isolated servers.

### Phase 4: Fleet-wide observability fan-out
Replace single-socket enumeration with per-socket fan-out: `reconcile-fleet.sh:46` and `status.py:96` must iterate every known bot's socket and union results (and preserve orphan/unbound detection — see Risks). Make `doctor.py:204` and `commands/move_bot.py:118,124` resolve the per-bot socket in Python. Confirm `tail-fleet.sh` (log-only) needs no change.
*Standalone value:* `claudlobby status`/`reconcile`/`doctor` stay correct under per-bot sockets.

### Phase 5: Validation, migration, docs
Extend `validate-bot-change.sh` to stand up `valmgr`/`valbot` on **distinct** sockets and assert dispatch + report-back + keepalive + fleet-pulse all work cross-socket (teardown via per-socket `kill-server`). Add the blast-radius acceptance test. Cut over one real low-stakes bot, verify round-trip, then migrate the fleet (Fork F5). Update `documentation/architecture/overview.md` (runtime model `:132-159,198`), the runtime section of the root docs, the manager dispatch protocol (operators must use socket-aware dispatch), and `library/lessons/migration/tmux-server-env-inheritance.md` (state whether `.tmux-env` stays belt-and-suspenders or is superseded), coordinating with the fleet-observability subsystem.
*Standalone value:* validated, migrated, documented.

## Decision Forks

### Fork F1: Socket granularity — per-bot vs per-fleet
- **Context:** How finely to split servers. The incident dropped a whole fleet at once.
- **Options:**
  - **(a)** Per-**bot** socket — blast radius = 1; maps 1:1 to the existing per-bot service; matches "distinct identities / isolated state."
  - **(b)** Per-**fleet** socket — fewer servers (3 vs ~16); simpler enumeration; but a server death still drops a whole fleet (up to ~10 bots).
- **Lean:** **(a) per-bot.** tmux servers cost only a few MB each, so "fewer servers" is not a real saving, and per-fleet leaves a large residual blast radius. Per-bot is the only option that fully removes shared fate.
- **Ratifier:** Human (fleet owner). *Note: owner approved forging "the new design" after raising per-fleet as a question — this fork records that the recommendation is per-bot and must be explicitly locked.*
- **Status:** open
- **Evidence:** Incident analysis (Current State); `PROJECT_MISSION.md` distinct-identities principle.

### Fork F2: Socket naming convention
- **Context:** Socket names share one namespace per host user (`/tmp/tmux-$(id -u)/`), so they must be unique across **all** fleets on the host — a stricter constraint than session names (which are per-server).
- **Options:**
  - **(a)** `tmux-<bot_id>` — simple, readable; **collision risk** if two fleets reuse a bot_id.
  - **(b)** `<BOT_SERVICE>` — already globally unique (e.g. `<prefix>.<bot_id>`); reuses the unit-identity SSOT; but `BOT_SERVICE` can be empty (the test harness sets it `""`).
  - **(c)** `<fleet>-<bot_id>` — explicit fleet scoping; a third naming scheme to maintain.
- **Lean:** **(b) `<BOT_SERVICE>`, falling back to `tmux-<bot_id>` when `BOT_SERVICE` is empty.** Reuses the globally-unique identity the unit-naming work already established; the fallback covers test/un-regenerated bots.
- **Ratifier:** Framework/manager (engineering judgment).
- **Status:** open
- **Evidence:** `4f10b28`/`758c944` established `BOT_SERVICE` as the global unit identity; `validate-bot-change.sh:46-50` sets `BOT_SERVICE=""`.

### Fork F3: Manager-socket resolution source
- **Context:** `MANAGER_TMUX` today derives from `fleet.teams` only, so a sub-manager gets `MANAGER_TMUX=<self>` and does **not** point upward to a top manager. `MANAGER_TMUX_SOCKET` inherits whatever this resolves to.
- **Options:**
  - **(a)** Mirror existing `MANAGER_TMUX` resolution exactly — emit `MANAGER_TMUX_SOCKET` from the same value; no hierarchy change.
  - **(b)** Prefer `bot.reports_to` (already parsed, available on `bot`) when present — threads the full hierarchy, including sub-managers reporting up.
  - **(c)** Require managers to be listed in a top-level team's `workers`.
- **Lean:** **(a) mirror exactly.** This plan isolates sockets; it must not silently redesign the reporting hierarchy. Any upward-reporting change is a separate, explicitly-scoped decision.
- **Ratifier:** Manager.
- **Status:** open
- **Evidence:** `composer.py:441-448`; `config.py:184-185,230-234`.

### Fork F4: Socket plumbing mechanism
- **Context:** ~17 scripts shell to tmux with no chokepoint. Own-socket and cross-socket calls need *different* sockets; fleet enumerators need *all* sockets.
- **Options:**
  - **(a)** Bake `-L "$TMUX_SOCKET"` into `_TMUX_BIN` resolution (`lib-common.sh:72-82`) — one-line coverage of own-socket calls, but **wrong** for cross-socket calls and fleet enumerators, and dangerous when `TMUX_SOCKET` is unset.
  - **(b)** Explicit `bot_tmux()` wrapper + `tmux_socket_for_bot()` helper; route own-socket calls through it; handle cross-socket and enumerators explicitly.
  - **(c)** Per-call `-L` literally everywhere — maximal churn, no abstraction.
- **Lean:** **(b) explicit wrapper.** Keeps `_TMUX_BIN` bare for special cases (enumerators, cross-socket, tests), makes socket targeting visible at each call, and lets the wrapper error loudly if a socket is required but unset.
- **Ratifier:** Framework.
- **Status:** open
- **Evidence:** Census shows 4 Python sites + 1 generated `ExecStop` + 2 fleet enumerators + peer-targeted dispatch/report that a single `_TMUX_BIN` bake-in cannot serve correctly.

### Fork F5: Migration / cutover strategy
- **Context:** Moving a running fleet from the default socket to per-bot sockets. During any window where some bots are on private sockets and others on default, cross-socket dispatch/report-back break (peer not found on the expected socket).
- **Options:**
  - **(a)** Big-bang: regenerate, run `pre-stop-handoff` for all, stop all, restart all on private sockets. Brief full-fleet bounce (~2 min, same as the incident recovery), no mixed-mode.
  - **(b)** Rolling per-bot: restart bots one-by-one — minimizes per-bot downtime but creates an extended mixed-mode window where manager↔worker comms are partially broken.
  - **(c)** Per-fleet staged: cut over an entire fleet (manager + its workers) together, fleet by fleet — bounded mixed-mode (only cross-fleet comms affected during the window).
- **Lean:** **(a) big-bang during a quiet window**, preceded by `pre-stop-handoff` so WIP/context is preserved. A single-host fleet already tolerates a ~2-min bounce; this avoids the silent cross-socket breakage of mixed mode.
- **Ratifier:** Human (operational call).
- **Status:** open
- **Evidence:** Cross-socket dependency in `dispatch.sh`/`report-back.sh`; `pre-stop-handoff.sh` already exists for graceful stops.

## Companion Plans

- **Fleet-observability subsystem** (`lib/fleet-pulse.sh`, `lib/bot-vitals.sh`, the `fleet-observability` protocol, `documentation/`): complementary, not conflicting. Its pulse/vitals checks *read* the sessions this plan re-targets (`fleet-pulse.sh:108,110,301`), so its checks update in lockstep with Phase 3/4. The extended validation harness exercises both together.
- No other planning document touches tmux/socket/session creation (`git log --grep=socket` = 0; active plans reviewed).

## Dependencies

| Dependency | Blocks | Risk |
|---|---|---|
| F1 (granularity), F2 (naming), F4 (mechanism) locked | Phase 1 implementation | Low — recommendations are clear; deferring lock stalls all phases |
| Phase 1 (helper + bot.conf fields) | Phases 2, 3, 4 | Low — additive, no behavior change |
| Phase 2 (own-socket lifecycle) | Phase 4 (enumerators need bots actually on sockets) | Med — touches the start/supervise hot path |
| Phase 3 (cross-socket comms) | Phase 5 end-to-end validation | Med — silent-failure surface (send-keys exits 0 on miss) |
| F5 (migration) locked | Phase 5 cutover | Med — operational/outage decision |
| Fleet-observability check parity | Phase 4/5 | Low — same author surface; forced by harness |

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Cross-socket peer resolved wrong → `send-keys` to missing target **exits 0** (silent dispatch/report loss) | High | Harness asserts the full round-trip; add `has-session` precheck on the peer socket that logs a warning on miss (Phase 3) |
| Orphan/unbound detection regression — a rogue bot on an unknown socket is invisible to per-socket enumeration | High | `reconcile-fleet.sh` scans `/tmp/tmux-$(id -u)/` for socket files and flags any socket not mapped to a known bot (Phase 4) |
| Socket files accumulate in `/tmp/tmux-$(id -u)/` from dead servers | Low | `ExecStopPost` cleanup + reconcile reaps stale socket files |
| `TMUX_TMPDIR` differs between the service env and an interactive shell → `-L <name>` resolves to *different* servers | High | Normalize/pin `TMUX_TMPDIR` (or rely on the unset default) consistently across unit env and helpers; document and assert in the lifecycle test |
| `_TMUX_BIN` bake-in (if F4-a chosen) targets `-L ""` when `TMUX_SOCKET` unset | High | F4 lean = explicit wrapper that errors loudly when a required socket is unset |
| Migration mixed-mode breaks manager↔worker comms mid-window | Med | F5 lean = big-bang after `pre-stop-handoff`; never leave the fleet split across socket schemes |
| Operator/manager dispatch muscle-memory + protocol docs assume the default socket | Med | Phase 5 updates the dispatch protocol; ship a socket-aware dispatch helper so operators never hand-type `-L` |
| `ExecStop` (`composer.py:507`) keeps killing on the default socket post-cutover (stale stop hook) | Med | Phase 2 makes `ExecStop` socket-aware; big-bang regenerate ensures all units carry the new stop hook |
| Scope creep into reporting-hierarchy redesign | Med | F3 lean = mirror existing resolution; hierarchy changes are explicitly out of scope |

## Validation Strategy

Per claudlobby's **MANDATORY** empirical-validation rule (`documentation/validating-bot-changes.md`), composition tests prove the var lands; only running the code proves behavior. Evidence must be cited in the PR.

1. **Unit (composition):** `tests/test_lifecycle_sockets.py` — for a mock bot with distinct `BOT_SERVICE`/slug/socket, assert every lifecycle script resolves the identical socket. Recompose a real bot; assert `TMUX_SOCKET` and `MANAGER_TMUX_SOCKET` appear in `bot.conf`.
2. **Behavioral (extended `validate-bot-change.sh`):** stand up `valmgr` + `valbot` on **distinct** sockets and assert objectively:
   - **dispatch:** manager `send-keys` to the worker's socket appears in the worker pane.
   - **report-back:** worker `send-keys` to `MANAGER_TMUX_SOCKET` puts a `[BOTREPORT]` line in the manager pane.
   - **keepalive:** killing the bot's session on **its** socket is detected by `has-session` on that socket and triggers a restart.
   - **fleet-pulse:** `notify_manager` reaches the manager pane (`[FLEET-PULSE]`).
   - Teardown via per-socket `tmux -L <socket> kill-server` (name-based `kill-session` on the default socket will not find them — would leak servers).
3. **Acceptance (the crown jewel — proves the SPOF is fixed):** with ≥2 bots up on private sockets, run `tmux -L <one-bot-socket> kill-server` and assert **only that bot's session is gone and every other bot's session is still up**. Blast radius = 1.
4. **Staged rollout:** cut over one real low-stakes bot, verify a live dispatch + report-back round-trip, before the fleet-wide migration.

## Complexity and Sequencing

| Phase | Size | Depends on | Parallel with |
|---|---|---|---|
| P1 Foundation (helper + composer + test) | M | Forks F1/F2/F4 locked | — |
| P2 Own-socket lifecycle | L | P1 | P3 (code only) |
| P3 Cross-socket dispatch/report | L | P1 | P2 (code only; e2e test needs P2) |
| P4 Observability fan-out | M | P2 | P3 |
| P5 Validation + migration + docs | M | P2, P3, P4; Fork F5 locked | — |

**Complexity profile:** S:0, M:3, L:2, XL:0. **Critical path:** P1 → P2 → P3 → P5 (P4 overlaps P3). P2 and P3 code can be written in parallel; P3's end-to-end validation gates on P2 (workers must actually be on sockets). The lifecycle-test (P1c) and the harness extension (P5) are the regression backstops the unit-naming work proved are worth the cost.

## Adversarial Review Findings

Pre-handoff stress test — blind spots surfaced before this plan leaves the author's hands:

- **Silent send-keys failures are the dominant hazard.** `send-keys` to a nonexistent `-t` target returns 0. Under one socket, a typo'd session is obvious (nothing happens, but the server is shared). Under per-bot sockets, a wrong-socket dispatch *also* returns 0 with no error and no delivery. Every cross-socket call **must** precheck the peer with `has-session` on the intended socket and log on miss. This is folded into Phase 3 and is the #1 thing `/ironclad` should scrutinize.
- **`TMUX_TMPDIR` is a latent footgun.** `-L <name>` resolves under `$TMUX_TMPDIR` (default `/tmp`). If the systemd user service and an interactive operator shell disagree on `TMUX_TMPDIR`, the same `-L <name>` is two different servers — the manager would dispatch into a phantom. Must be pinned/asserted, not assumed. Added to Risks.
- **Orphan detection silently regresses.** `reconcile-fleet.sh`'s value is catching `unbound` sessions (running but not in fleet.yaml) via `tmux ls`. Per-bot sockets remove the single list to scan, so a rogue server becomes invisible unless reconcile enumerates socket *files* in `/tmp/tmux-$(id -u)/`. Without this, the plan would *quietly weaken* a safety net while strengthening another. Explicit Phase 4 requirement + Risk.
- **The `_TMUX_BIN` "one-line fix" is a trap.** Baking `-L` into the binary resolver looks elegant and covers the most call sites, but it is wrong for the exact calls that matter most (cross-socket, enumerators) and fails open when `TMUX_SOCKET` is unset. Adversarial review pushed F4's lean to the explicit wrapper.
- **The operator (manager) is itself a bot on a socket.** Post-cutover, the manager's own dispatch (`tmux send-keys -t <worker>`) must become `tmux -L <worker-socket> send-keys -t <worker>`. Hand-typed dispatch and the dispatch-protocol docs change too — not just the scripts. A socket-aware dispatch helper is required so operators don't memorize sockets. Added to Phase 5 + Risks.
- **This plausibly trips the mission's approval gate.** `PROJECT_MISSION.md` requires approval for "architectural changes to the manager-worker pattern or dispatch mechanism." Per-bot sockets change how dispatch targets sessions. Frame this as an explicitly-approved architectural change (the fleet owner directed this plan), not standing-permission work — and call it out for the ratifier.
- **The env-bleed lesson partially dies — say so.** `library/lessons/migration/tmux-server-env-inheritance.md` and the `.tmux-env` workaround exist because of the shared server. Isolated servers largely remove that bleed. Leaving the lesson unupdated would mislead future readers; Phase 5 must state whether `.tmux-env` stays (belt-and-suspenders) or is retired.
