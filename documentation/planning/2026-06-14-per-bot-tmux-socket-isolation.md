---
title: "Per-Bot tmux Socket Isolation"
type: plan
status: draft
owner: clog
tags: [runtime, supervision, tmux, reliability, dispatch]
created: 2026-06-14
updated: 2026-06-15
ironclad: cycle-1 complete (CHANGES-NEEDED, 0 blockers); ALL 5 forks locked — F1/F3/F5 ratified by Chris 2026-06-15; lock-only pass pending
---

# Per-Bot tmux Socket Isolation

## Goal

Give every fleet bot its own tmux server (its own `-L <socket>`) instead of sharing one per-user server, so the death of one server can no longer drop the entire fleet's sessions at once. This hardens the dispatch substrate (`tmux send-keys`) that the manager-worker pattern depends on, directly serving the north star — **"Trivial to run a fleet of distinct, cooperating bots on cheap hardware"** (`PROJECT_MISSION.md`) — and its principle that bots are *"distinct identities … isolated state, not threads of a single account."* Today a bot's session is the one piece of its identity that is **not** isolated.

## Current State

Verified against the codebase (2026-06-14); every line anchor below was independently re-confirmed against the live repo during ironclad cycle 1. **No script uses `-L`/`-S` anywhere** — a repo-wide grep returns zero matches; every tmux call (shell and Python) talks to the implicit default per-user socket at `/tmp/tmux-$(id -u)/default`. There is exactly one tmux server per host user.

**The failure this fixes (observed incident):** the shared tmux server died and dropped every bot's session simultaneously across all fleets on the host. Each per-bot systemd/launchd service only spawns a detached `tmux new-session -d` and then exits, so the service still reads `active` even after its session is gone. The fleet-health sweep showed every bot as `SERVICE=ok` but `SESSION=missing` at the same instant — the unmistakable signature of a single-server death rather than N independent restarts. The watchdog (`lib/keepalive.sh`) then rebuilt the whole fleet bot-by-bot over ~2 minutes. The single shared server is a fleet-wide single point of failure. **Note:** the reference host runs **multiple fleets**, so this is not a single-fleet deployment — relevant to Fork F1.

**The one-server coupling is already documented** in `lib/start-bot.sh:106-119` and in the lesson `library/lessons/migration/tmux-server-env-inheritance.md:5` ("tmux runs one server per user … every session spawned afterward inherits that frozen env"). The per-session `.tmux-env` file mechanism (`lib/start-bot.sh:121,140-142`) exists specifically to work around env bleed on that shared server.

**Key facts that shape the design:**

- **Session name = bot directory slug** (`basename`), via `tmux_session_name()` at `lib/lib-common.sh:250-252`; created at `lib/start-bot.sh:195`. (The `claude --name "<LABEL>-<date>-<HHMM>"` value on the process line is Claude Code's own session label — `lib/start-bot.sh:98,155` — **not** the tmux session name. Three distinct axes already exist: **unit name** = `BOT_SERVICE`, **session name** = slug, and now a proposed **socket** — keep all three separate.)
- **bot.conf is the single source of truth** that auto-propagates into the session. `compose_bot_conf()` (`claudlobby/composer.py:268-457`, written at `:1132`) emits `KEY=value` lines; `lib/start-bot.sh:140-142` re-sources bot.conf into `.tmux-env` via `set -a; . bot.conf; set +a`. **Any new var added to bot.conf reaches both the start-bot parent and the claude session with zero unit-file changes.**
- **Manager identity is already derivable at compose time.** `MANAGER_TMUX` is emitted at `claudlobby/composer.py:441-448` from `fleet.teams` (`TeamConfig{name,manager,workers}`, `config.py:230-234`). A worker gets `MANAGER_TMUX=<team.manager>`; a manager gets `MANAGER_TMUX=<self>`. (`BotConfig.reports_to`/`manages` exist at `config.py:184-185` but feed only CLAUDE.md org rendering, **not** `MANAGER_TMUX`.)
- **Unit files barely change.** Both systemd (`composer.py:487-519`) and launchd (`composer.py:526-556`) units pass only the bot dir to `start-bot.sh`; everything else flows through bot.conf. **Two exceptions:** the generated systemd `ExecStop` at `composer.py:507` (`tmux kill-session -t {bot.bot_id}`) runs on the default socket and will silently no-op once a bot moves to a private socket; the `ExecStopPost` at `composer.py:508` removes `.tmux-env` (socket-agnostic, fine).

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
| **Report (worker→mgr)** | `report-back.sh:23,65,67` | `send-keys` (with `\|\| true`) | **peer (`MANAGER_TMUX`)** |
| **Pulse push (→mgr)** | `fleet-pulse.sh` `notify_manager()`: **`:65` `check_tmux_session \|\| return 0`** + **`:66` `send-keys` (`2>/dev/null \|\| true`)**; per-bot checks `:108,110,301` | `has-session`,`send-keys`,`capture-pane` | **peer + own slug** |
| Sprint nudge (→mgr) | `sprint-trigger.sh:20…` | `check_tmux_session`,`capture-pane`,`send-keys` | **peer (`MANAGER_TMUX`)** |
| Sweep cron | `bot-sweep-cron.sh:33,39,46,48` | `has-session`,`capture-pane`,`send-keys` (bypasses helper) | **peer (session-name arg, no bot_dir)** |
| **Fleet enumerate** | `reconcile-fleet.sh:46` (bare `tmux ls`), `:191` `kill-session` | `ls`,`kill-session` | **all sessions, one socket** |
| Test harness | `validate-bot-change.sh:37,38,56,57,76` | bare `tmux` new/capture/kill | literal `valbot`/`valmgr` |
| Python | `status.py:96` (`ls`), `doctor.py:204` (`has-session`), `commands/move_bot.py:118,124` | subprocess `["tmux",…]` | all / by id |
| **Generated unit** | `composer.py:507` (`ExecStop`), `:508` (`ExecStopPost`) | `kill-session` | by `bot_id`, default socket |

Distinct subcommands a socket layer must cover: `new-session`, `has-session`, `send-keys`, `capture-pane`, `kill-session`, `ls`/list-sessions, `list-panes`. Note the existing `sanitize_tmux_input()` helper used on the pulse send (`fleet-pulse.sh:66`) — preserve it through the wrapper.

**Prior art:** `git log --grep=socket` returns **zero** commits — genuinely new ground (confirmed). The directly relevant precedent is the **unit-naming unification** (`4f10b28` #394, `758c944` #395, both confirmed present and matching): a single bot.conf field (`BOT_SERVICE`) resolved everywhere via `bot_conf_get` (now in `lib-common.sh:468`), with a fallback for un-regenerated bots, plus `tests/test_lifecycle_names.py` asserting cross-script agreement. **This plan mirrors that template exactly**, adding socket as the third identity axis. Existing tmux-env isolation tests (`#367`) are the natural home for socket-isolation tests. Marker-based idle detection (`f6a9311` #400) is socket-agnostic and undisturbed.

**No active plan conflicts.** The nearest neighbor is the fleet-observability subsystem (`lib/fleet-pulse.sh`, `lib/bot-vitals.sh`, the `fleet-observability` protocol): it only *reads* sessions and pushes to the manager. It is **complementary**, but its pulse checks read the same sessions this plan re-targets, so they must update in lockstep (the harness extension forces this).

## Architecture

Target state: each bot runs its own tmux server identified by a stable socket name carried in bot.conf, reached through a single safe wrapper.

```
            BEFORE (single server, SPOF)                 AFTER (per-bot servers)
          ┌───────────────────────────┐         ┌──────────┐ ┌──────────┐ ┌──────────┐
          │  default socket (1 server)│         │ -L mgr   │ │ -L wkr1  │ │ -L wkr2  │
          │  [mgr][wkr1][wkr2][wkr3]…  │         │ [mgr]    │ │ [wkr1]   │ │ [wkr2]   │
          └───────────────────────────┘         └──────────┘ └──────────┘ └──────────┘
          one death ⇒ all sessions gone          one death ⇒ exactly one bot down

  bot.conf gains:  TMUX_SOCKET=<own socket>   MANAGER_TMUX_SOCKET=<manager's socket>   TMUX_TMPDIR=<pinned>
  Resolution:      tmux_socket_for_bot() (SSOT, like BOT_SERVICE) + bot_tmux()/bot_tmux_send() wrappers in lib-common.sh
  Own-socket calls:    bot_tmux "$TMUX_SOCKET" <cmd>          →  tmux -L "$TMUX_SOCKET" <cmd>
  Cross-socket sends:  bot_tmux_send <peer_socket> <session> <text>  (precheck + logged miss, NO silent || true)
  Fleet enumerators:   iterate every bot's socket and union (replaces single `tmux ls`)
  Generated ExecStop:  socket-aware kill (composer.py:507)
```

The socket is resolved from a single helper so every script agrees, exactly as `BOT_SERVICE` is resolved via `bot_conf_get` today. **All cross-socket sends go through one safe-send wrapper** (`bot_tmux_send`) so the silent-failure mode is closed in one place rather than per-call.

## Phases

### Phase 1: Socket-resolution foundation + safe-send wrapper (SSOT)
#### 1a. lib-common.sh helpers
- `tmux_socket_for_bot()` — bot identity → socket name (Fork F2), with the empty-`BOT_SERVICE` **production guard** (fail-fast when `FLEET_NAME` is set; bare-`bot_id` fallback only when `FLEET_NAME` unset, i.e. the test harness).
- `bot_tmux(<socket> <args…>)` — wrapper prepending `-L "<socket>"` to `"$_TMUX_BIN"`. **Unset-socket contract (Fork F4):** if `<socket>` is empty while `FLEET_NAME` is set → return non-zero + stderr error (never `-L ""`); if `FLEET_NAME` unset → pass through to the default socket (backward-compat for un-regenerated/test bots).
- `bot_tmux_send(<peer_socket> <session> <text>)` — the **one** safe cross-socket send: `has-session -t <session>` on `<peer_socket>` first; on miss, **emit a `send_miss` event** to the caller's `data/events/fleet-*.jsonl` ledger (fields: target, socket, session, caller) **and** a stderr breadcrumb, then return non-zero — **this replaces the bare `|| true`** at every cross-socket call site. On hit, two-step `send-keys` (text, then `Enter`) preserving `sanitize_tmux_input`. Residual TOCTOU (session dies between precheck and send) is logged best-effort, not swallowed.
- `check_tmux_session()` gains an optional socket argument (backward-compatible default).
#### 1b. composer emits socket fields
Emit `TMUX_SOCKET` unconditionally near `BOT_ID` (`composer.py:300-303`) and `MANAGER_TMUX_SOCKET` beside the existing `MANAGER_TMUX` block (`composer.py:441-448`), derived from the **same** source as `MANAGER_TMUX` (Fork F3), each with a defined unset/fallback policy. Emit a **pinned `TMUX_TMPDIR`** in bot.conf *and* the unit `Environment=`/`EnvironmentVariables` (single fleet-wide value; default `/tmp` made explicit) — see Risk 2.
#### 1c. precedent-style test
Add `tests/test_lifecycle_sockets.py` mirroring `test_lifecycle_names.py`: for a mock bot whose `BOT_SERVICE`/slug/socket all differ, assert every lifecycle script resolves the identical socket **and the identical `TMUX_TMPDIR`**, and that an empty-`BOT_SERVICE`+`FLEET_NAME`-set bot fails the guard.
*Standalone value:* bot.conf carries socket + pinned tmpdir; SSOT helper + safe-send wrapper + tests exist; **no runtime behavior changes yet** (vars emitted, wrappers unused until Phase 2).

### Phase 2: Own-socket lifecycle (safe-send applies here, not just Phase 3)
Route every **own-slug** call through `bot_tmux`: `start-bot.sh` (`:96,195,204,208,230-236`), `keepalive.sh` (`:59,62,88`), `pre-stop-handoff.sh` (`:36,37`), `bench-cold-start.sh` (`:185,186,189,218`), `fleet-memory-check.sh` (`:125,134`), and `check_tmux_session` call sites. Fix the generated `ExecStop` (`composer.py:507`) to `tmux -L <socket> kill-session` (socket known at generate-time). Add a **transient reconcile guard** (Risk 3): until Phase 4 lands, `reconcile-fleet.sh` must treat an empty default-socket `tmux ls` during/after migration as a **warning, not a clean bill of health**. `spin-up-bot.sh` inherits via `start-bot.sh`.
*Standalone value:* a bot starts on its own server and is supervised on it; `keepalive` detects death on the correct socket; the cross-socket safe-send is already the supervised path.

### Phase 3: Cross-socket dispatch + reporting
Patch the **peer-targeting** scripts to route through `bot_tmux_send` against the peer's socket:
- `report-back.sh:65,67` → `MANAGER_TMUX_SOCKET` (with a defined fallback, cf. the existing `${MANAGER_TMUX:-claude-bot}` at `:23`); the bare `|| true` is removed in favor of the wrapper's logged miss.
- `fleet-pulse.sh` `notify_manager()` → **patch BOTH `:65` (the `check_tmux_session` must target the manager's socket, else it always `return 0`s post-migration and pulse alerts die silently) and `:66` (the send)**.
- `sprint-trigger.sh` → `MANAGER_TMUX_SOCKET`.
- `dispatch.sh:18,25,27` (+ `dispatch-task.sh` delegate) → the **target worker's** socket, resolved from the worker identity.
- `bot-sweep-cron.sh:33,39,46,48` → **interface change required:** cron passes a session-name arg, not a bot_dir, so `tmux_socket_for_bot()` has nothing to resolve from. Either change the cron invocation to pass the bot_dir, or add a session-name→socket reverse lookup. Specify which before implementing.
*Standalone value:* manager↔worker comms work across isolated servers, with misses logged instead of silently dropped.

### Phase 4: Fleet-wide observability fan-out
Replace single-socket enumeration with per-socket fan-out: `reconcile-fleet.sh:46` and `status.py:96` iterate every known bot's socket and union results, **and scan `/tmp/tmux-$(id -u)/` socket files to preserve orphan/unbound detection** (a rogue bot on an unknown socket is otherwise invisible — Risk 3). Remove the Phase-2 transient guard. Make `doctor.py:204` and `commands/move_bot.py:118,124` resolve the per-bot socket in Python. Confirm `tail-fleet.sh` (log-only) needs no change.
*Standalone value:* `claudlobby status`/`reconcile`/`doctor` stay correct under per-bot sockets; orphan detection restored.

### Phase 5: Validation, migration, docs (socket-aware harness)
Extend `validate-bot-change.sh` — its own setup/teardown uses **bare `tmux` against literal `valbot`/`valmgr`**, so it must become socket-aware (stand up `valmgr`/`valbot` on **distinct** sockets; specify how `TMUX_SOCKET` enters the hand-written test bot.conf; teardown via per-socket `kill-server` or servers leak). Assert dispatch + report-back + keepalive + fleet-pulse all work cross-socket, plus the blast-radius acceptance test. Execute the migration (Fork F5). Update `documentation/architecture/overview.md` (runtime model `:132-159,198`), the runtime section of the root docs, the **manager dispatch protocol** (operators must use a socket-aware dispatch helper, not hand-typed `tmux send-keys -t`), and `library/lessons/migration/tmux-server-env-inheritance.md`. **`.tmux-env` fate (decided here, up front):** keep it as belt-and-suspenders for v1 (do not expand scope); revisit retiring it post-migration — retiring touches `composer.py:508` (ExecStopPost) and Phase-2 start-bot.
*Standalone value:* validated, migrated, documented.

## Decision Forks

### Fork F1: Socket granularity — **LOCKED (per-bot)**
- **Context:** How finely to split servers. The incident dropped a whole fleet at once.
- **Options:**
  - **(a)** Per-**bot** socket — blast radius = 1; maps 1:1 to the existing per-bot service; matches "distinct identities / isolated state."
  - **(b)** Per-**fleet** socket — fewer servers; but a server death still drops a whole fleet (up to a full team).
- **Lean:** **(a) per-bot.** tmux servers cost only a few MB each. **Note (ironclad):** on a *single-fleet* host, per-fleet ≡ per-bot on blast radius at slightly lower plumbing cost — but **the reference host is multi-fleet**, so per-fleet would leave intra-fleet blast radius (a whole team) intact. Per-bot is the only option that fully removes shared fate on this host.
- **Ratifier:** **Human (fleet owner) — LOCKED (Chris, 2026-06-15): per-bot.** Single-fleet-equivalence caveat acknowledged N/A (this host is multi-fleet).
- **Status:** locked
- **Evidence:** Incident analysis; ironclad cycle 1 (both lenses: lean holds); `PROJECT_MISSION.md`.

### Fork F2: Socket naming convention — **LOCKED (b) + guard**
- **Context:** Socket names share one namespace per host user (`/tmp/tmux-$(id -u)/`), so they must be unique across **all** fleets on the host.
- **Options:** (a) `tmux-<bot_id>` — collision risk across fleets. **(b) `<BOT_SERVICE>`** — confirmed fleet-prefixed and host-wide unique. (c) `<fleet>-<bot_id>` — a third naming scheme.
- **Decision:** **(b) `<BOT_SERVICE>`, with a production guard.** ironclad confirmed `BOT_SERVICE` is host-wide unique, but the original empty-`BOT_SERVICE` → bare `tmux-<bot_id>` fallback uses an un-prefixed `bot_id` that **collides across fleets and reintroduces the SPOF**. Guard: `tmux_socket_for_bot()` **fails fast when `BOT_SERVICE` is empty while `FLEET_NAME` is set**; the bare-`bot_id` fallback is permitted **only** for the test harness (`FLEET_NAME` unset). A collision-safe fallback (`tmux-<bot_id>-<dir-hash>`) is an acceptable alternative.
- **Ratifier:** Framework/manager — **LOCKED** (ironclad cycle 1, manager).
- **Status:** locked
- **Evidence:** `4f10b28`/`758c944`; `validate-bot-change.sh:46-50` sets `BOT_SERVICE=""`; ironclad Risk-F2.

### Fork F3: Manager-socket resolution source — **LOCKED (mirror)**
- **Context:** `MANAGER_TMUX` derives from `fleet.teams` only; a sub-manager gets `MANAGER_TMUX=<self>`.
- **Decision:** **(a) mirror existing `MANAGER_TMUX` resolution** — emit `MANAGER_TMUX_SOCKET` from the same value; no hierarchy redesign. **Explicit accepted limitation:** a sub-manager's `MANAGER_TMUX(_SOCKET)=self`, so a sub-manager's *upward* report-back is non-functional — this is **pre-existing, inherited, and out of scope** here (any upward-reporting fix is a separate decision). *(Live proof during review: the reviewing sub-manager's own `MANAGER_TMUX=self`; reporting up required an explicit override.)*
- **Ratifier:** Manager — **LOCKED** (ironclad cycle 1; Chris confirmed the accepted limitation, 2026-06-15). 
- **Status:** locked
- **Evidence:** `composer.py:441-448`; `config.py:184-185,230-234`.

### Fork F4: Socket plumbing mechanism — **LOCKED (explicit wrapper)**
- **Context:** ~17 scripts shell to tmux with no chokepoint; own-socket, cross-socket, and fleet-enumerator calls need different sockets.
- **Decision:** **(b) explicit `bot_tmux()`/`bot_tmux_send()` wrappers + `tmux_socket_for_bot()` helper.** Bake-in to `_TMUX_BIN` is confirmed wrong for cross-socket calls, fleet enumerators, and the 4 Python sites, and fails open when `TMUX_SOCKET` is unset. **Unset-socket contract (defined):** hard error when `FLEET_NAME` set but socket empty; default-socket pass-through only when `FLEET_NAME` unset.
- **Ratifier:** Framework — **LOCKED** (ironclad cycle 1).
- **Status:** locked
- **Evidence:** Census (4 Python sites + generated `ExecStop` + 2 enumerators + peer-targeted dispatch/report); ironclad Risk-4.

### Fork F5: Migration / cutover strategy — **LOCKED (a) big-bang**
- **Context:** Moving a running fleet from the default socket to per-bot sockets. Any window where some bots are on private sockets and others on default breaks cross-socket comms (peer not found on the expected socket). ironclad found the original Phase-5 "cut over one bot, then migrate the rest" **is itself** a mixed-mode window — a 4th option the lean didn't name.
- **Options:**
  - **(a)** **Big-bang simultaneous** — regenerate all → `pre-stop-handoff` all → **quiesce dispatch** (no new sends in flight) → stop all → start all on private sockets. No prod canary. **Manager restart ordering:** start **managers before workers**, so a worker's first report has a live manager socket (workers-first loses early reports; the new safe-send logs the miss rather than hanging, but the report is still lost).
  - **(d)** **Canary-then-big-bang, scoped** — migrate one bot **with no active peer-socket comms during the window** (a leaf worker quiesced, or the **manager migrated last**), validate, then big-bang the rest. Avoids the silent mixed-mode only if the canary genuinely has no peer sends.
  - (b) Rolling per-bot / (c) per-fleet staged — extended mixed-mode; not recommended.
- **In-flight buffer loss (must address):** `pre-stop-handoff` preserves Claude **session context**, **not** unsubmitted tmux input buffers. The bounce loses any dispatch mid-typed or queued but not yet processed. The quiesce step must ensure no dispatch is in flight before stopping.
- **Decision:** **(a) big-bang simultaneous, managers-started-first, with an explicit dispatch quiesce** (Chris, 2026-06-15). No prod canary; reach for (d) was declined.
- **Ratifier:** **Human (operational) — LOCKED (Chris, 2026-06-15): option (a), managers-first.**
- **Status:** locked
- **Evidence:** Cross-socket dependency in `dispatch.sh`/`report-back.sh`; `pre-stop-handoff.sh`; ironclad Fork-F5.

## Ratifier Decisions (for Chris)

All three resolved (Chris, 2026-06-15):

- **Q1 — F5 migration (operational):** **(a) big-bang simultaneous**, managers restart before workers. ✓ locked
- **Q2 — F1 granularity:** **lock per-bot** (single-fleet-equivalence caveat N/A on this multi-fleet host). ✓ locked
- **Q3 — F3 scope:** **accept** the sub-manager upward-report-back limitation as out of scope. ✓ locked

## Companion Plans

- **Fleet-observability subsystem** (`lib/fleet-pulse.sh`, `lib/bot-vitals.sh`, the `fleet-observability` protocol): complementary. Its pulse/vitals checks *read* the sessions this plan re-targets (`fleet-pulse.sh:65,66,108,110,301`), so they update in lockstep with Phase 3/4; the `send_miss` event (Phase 1) feeds the same ledger the manager already reads. The extended harness exercises both together.
- No other planning document touches tmux/socket/session creation (`git log --grep=socket` = 0; active plans reviewed).

## Dependencies

| Dependency | Blocks | Risk |
|---|---|---|
| F2/F3/F4 locked (done); F1/F5 ratified | Phase 1 impl (F2/F4); Phase 5 cutover (F5) | Low for F2/F3/F4; F5 gates migration |
| Phase 1 (helpers + safe-send + bot.conf fields + tmpdir) | Phases 2, 3, 4 | Low — additive, no behavior change |
| Phase 2 (own-socket lifecycle + transient reconcile guard) | Phase 4 | Med — touches start/supervise hot path |
| Phase 3 (cross-socket via safe-send) | Phase 5 end-to-end validation | Med — silent-failure surface now centralized in `bot_tmux_send` |
| Phase 4 (enumerator fan-out + orphan scan) | removes Phase-2 transient guard | Low |
| Fleet-observability check parity | Phase 3/4 | Low — forced by harness |

## Risks

| Risk | Sev | Mitigation |
|---|---|---|
| Cross-socket `send` to a missing target **exits 0**; `\|\| true` + `2>/dev/null` swallow it; TOCTOU after a precheck | High | **All** cross-socket sends route through `bot_tmux_send` (Phase 1) which prechecks on the peer socket, removes `\|\| true`, and on miss emits a `send_miss` event to `data/events/*.jsonl` + stderr. Applies from **Phase 2**. Patch **both** `fleet-pulse.sh:65` (check) and `:66` (send), not just `:66`. |
| `TMUX_TMPDIR` drift → same `-L <name>` resolves to two servers, silently | High | **Pin** a single fleet-wide `TMUX_TMPDIR` in bot.conf + unit `Environment=`; `tmux_socket_for_bot()`/`bot_tmux()` rely on it; lifecycle test asserts every script + unit agree. **"Rely on unset" is removed** — it is trust, not enforcement. |
| Empty-`BOT_SERVICE` fallback `tmux-<bot_id>` collides across fleets → reintroduces SPOF | High | F2 guard: fail-fast when `BOT_SERVICE` empty while `FLEET_NAME` set; bare-id fallback only for the test harness (or `tmux-<bot_id>-<dir-hash>`). |
| reconcile-fleet blindness window (Phase 2 before Phase 4): empty default-socket `tmux ls` → orphan detection silently passes → false clean bill of health | Med | Transient Phase-2 guard: treat empty session list as a **warning**; Phase 4 adds socket-file scan of `/tmp/tmux-$(id -u)/`. Call out in the migration runbook. |
| Partial-migration / unset-socket contract undefined (stale `bot.conf` pre-regen) | Med | Defined `bot_tmux()` unset contract (F4) + `MANAGER_TMUX_SOCKET` fallback policy; lifecycle test covers the un-regenerated case. |
| Migration mixed-mode breaks manager↔worker comms; in-flight dispatch lost in bounce | Med | F5 lean = big-bang after `pre-stop-handoff` **+ dispatch quiesce** (handoff preserves Claude context, not unsubmitted tmux input); managers-started-first. |
| Operator/manager dispatch assumes default socket | Med | Phase 5 updates the dispatch protocol + ships a socket-aware dispatch helper so operators never hand-type `-L`. |
| Phase 5 harness more invasive than "extend" implies (bare `tmux` on literals; setup/teardown + test bot.conf must go socket-aware) | Med | Phase 5 resized **M→L**; harness setup/teardown socket-aware; specify `TMUX_SOCKET` injection into the hand-written test bot.conf; teardown via per-socket `kill-server`. |
| `bot-sweep-cron.sh` has only a session-name arg, no bot_dir to resolve a socket from | Med | Phase 3: change cron invocation to pass bot_dir, or add a session-name→socket reverse lookup (decide before impl). |
| Scope creep into reporting-hierarchy redesign | Med | F3 locked = mirror existing; hierarchy changes explicitly out of scope. |

## Validation Strategy

Per claudlobby's **MANDATORY** empirical-validation rule (`documentation/validating-bot-changes.md`), composition tests prove the var lands; only running the code proves behavior. Evidence cited in the PR.

1. **Unit (composition):** `tests/test_lifecycle_sockets.py` — for a mock bot with distinct `BOT_SERVICE`/slug/socket, assert every lifecycle script resolves the identical socket **and `TMUX_TMPDIR`**; assert the empty-`BOT_SERVICE`+`FLEET_NAME`-set guard fails fast. Recompose a real bot; assert `TMUX_SOCKET`, `MANAGER_TMUX_SOCKET`, `TMUX_TMPDIR` in `bot.conf`.
2. **Behavioral (socket-aware `validate-bot-change.sh`):** `valmgr` + `valbot` on **distinct** sockets; assert objectively — **dispatch** (manager send appears in worker pane), **report-back** (worker `[BOTREPORT]` reaches manager pane via `bot_tmux_send`), **report-miss** (kill the manager session, assert a `send_miss` event is emitted — proving the silent-failure path is now observable), **keepalive** (killing the bot's session on **its** socket is detected and restarted), **fleet-pulse** (`notify_manager` reaches the manager pane, with `:65` checking the manager socket). Teardown via per-socket `kill-server`.
3. **Acceptance (crown jewel — proves the SPOF is fixed):** with ≥2 bots up on private sockets, `tmux -L <one-bot-socket> kill-server` and assert **only that bot's session is gone and every other bot's session is still up**. Blast radius = 1.
4. **Migration dry-run:** rehearse the chosen F5 path in a scratch fleet (quiesce → handoff → stop → managers-first start) before the real cutover.

## Complexity and Sequencing

| Phase | Size | Depends on | Parallel with |
|---|---|---|---|
| P1 Foundation (helpers + safe-send + composer + tmpdir + test) | M | F2/F4 locked | — |
| P2 Own-socket lifecycle (+ transient reconcile guard) | L | P1 | P3 (code only) |
| P3 Cross-socket dispatch/report (via safe-send) | L | P1 | P2 (code only; e2e needs P2) |
| P4 Observability fan-out (+ orphan socket-file scan) | M | P2 | P3 |
| P5 Validation + migration + docs (socket-aware harness) | **L** | P2, P3, P4; F5 ratified | — |

**Complexity profile:** S:0, M:2, L:3, XL:0 (Phase 5 raised M→L per ironclad). **Critical path:** P1 → P2 → P3 → P5 (P4 overlaps P3). P2/P3 code parallel; P3 e2e gates on P2. The lifecycle test (P1c) and the harness extension (P5) are the regression backstops the unit-naming work proved worth the cost.

## Adversarial Review Findings

Self-adversarial pass (v0) surfaced six blind spots; **ironclad cycle 1 (rajan + navi)** converged on the same cluster and tightened them. Net state after hardening:

- **Silent send-keys is the dominant hazard — now centralized.** Closed in one place via `bot_tmux_send` (precheck + logged `send_miss` event, no `|| true`), applied from Phase 2, patching both `fleet-pulse.sh:65` and `:66`. Residual TOCTOU is logged, not swallowed.
- **`TMUX_TMPDIR` footgun — now enforced, not trusted.** Pinned + asserted; "rely on unset" removed.
- **Orphan detection regression — explicitly preserved.** Phase 4 scans socket files; Phase 2 carries a transient warning guard.
- **`_TMUX_BIN` bake-in trap — avoided.** F4 locked to explicit wrappers with a defined unset contract.
- **The operator is itself a bot on a socket.** Phase 5 updates the dispatch protocol + ships a socket-aware helper.
- **Mission approval gate.** Per-bot sockets change dispatch targeting → `PROJECT_MISSION.md` "architectural changes to the manager-worker pattern or dispatch mechanism" gate. Framed as an explicitly-approved architectural change (owner directed this plan).
- **The env-bleed lesson partially dies — decided up front.** `.tmux-env` kept belt-and-suspenders for v1; retirement deferred and scoped (touches `composer.py:508` + Phase-2 start-bot).

## Ironclad Cycle 1 — Resolutions

Review: PR #414 comment ([issuecomment-4704311264](https://github.com/Claudfather/Claudlobby/pull/414#issuecomment-4704311264)) — CHANGES-NEEDED, 0 blockers, design sound, PII clean, line anchors + precedent verified.

| Finding | Resolution |
|---|---|
| R1 cross-socket silent-failure under-specified (missed `fleet-pulse:65`; TOCTOU; log destination; should gate Phase 2) | `bot_tmux_send` wrapper in **Phase 1**, applied from **Phase 2**; patches `:65`+`:66`; `send_miss` event channel defined; `|| true` removed |
| R2 `TMUX_TMPDIR` "rely on unset" not a mitigation | Pinned in bot.conf + unit; asserted in lifecycle test; "rely on unset" removed from risk table |
| F2 empty-`BOT_SERVICE` fallback collides → SPOF | F2 **locked** with fail-fast production guard |
| R3 reconcile blindness window | Transient Phase-2 warning guard + Phase-4 socket-file scan |
| R4 partial-migration/unset contract undefined | `bot_tmux()` unset contract + `MANAGER_TMUX_SOCKET` fallback defined (F4) |
| F5 canary == mixed-mode; restart ordering; in-flight buffer loss | F5 **reworked**: option (d) added, managers-first, dispatch quiesce, buffer-loss noted; left OPEN for ratifier |
| Gap: Phase-5 harness undersized | Resized **M→L**; socket-aware setup/teardown specified |
| Gap: `bot-sweep-cron` resolution | Phase-3 interface change flagged (pass bot_dir or reverse-lookup) |
| Gap: `.tmux-env` fate | Decided up front: keep v1, defer retirement |
| F1/F3/F4 leans hold | F3/F4 **locked**; F1 left for human ratifier with the single-fleet caveat noted |

**Convergence:** 0 blockers. **All 5 forks locked** — F2/F4 (framework) + F3 (manager) via ironclad cycle 1; F1/F3/F5 ratified by Chris 2026-06-15. Next: a lock-only `/ironclad` pass to confirm post-lock consistency, then ready for `/implement-plan`.
