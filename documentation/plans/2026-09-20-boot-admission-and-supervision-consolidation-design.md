---
title: Boot admission and supervision consolidation
type: spec
status: approved
owner: chrisrogers37
created: 2026-09-20
updated: 2026-09-21
issue: "#1573"
supersedes: none
---

> **Amended 2026-09-21 after ironclad cycle 1.** The design rulings in §11 stand
> unchanged. Three things in the *evidence and the mechanics* did not survive
> review and are corrected in place, each marked **CORRECTION**: the claim that a
> launchd host has no stagger (§1, §2 — it has one, and the gate must replace it),
> the gate state's scope (§6.2 — per **install root**, not host-wide), and the
> boot-progress marker's lifetime (§6.2 step 4 / §6.3 — acquire → **release**, and
> the freshness window is carried by launcher liveness, not by mtime alone).
> Two ratified numbers are under open forks in the epic and are flagged where they
> appear: the wait cap (**F13**) and where the slot count is derived (**F14**).

# Boot admission and supervision consolidation

## 1. Summary

After a cold boot of an 18-bot macOS host on 2026-09-19, 11 of 18 Telegram pollers never came up, and one fleet's keepalive bounce loop turned that into an alert storm (#1573). The transcripts show why: every session spawns about five MCP servers, all 18 sessions started in the same instant, the MCP startup phase took over five minutes, and the Telegram channel server missed Claude Code's startup timeout and was never retried. Two facts in our own source make that structural: nothing sets the MCP startup timeout, and the boot stagger exists only in the systemd unit, so a launchd host has no stagger at all.

> **CORRECTION (2026-09-21).** The second half of that sentence is **false**, and it was load-bearing. `lib/start-bot.sh:14-47` — the #304 "Boot-mass mitigation" (`bc0990f`, merged `efa2092`) — is a fleet-wide `mkdir` lock with an 8 s hold, a 60 s stale force-claim and a 120 s wait cap. It is **platform-neutral and runs on launchd today**, and it appears in zero lines of the original spec, epic and per-PR plans. The launchd host has a serializer; it is single-holder, not priority-aware, and at 18 bots × 8 s against its own 120 s cap the tail of the fleet provably ran **unserialized** on the incident boot — a contributing mechanism the analysis below never considered. The causal story is not overturned (contended MCP startup is still the failure), but the remedy changes shape: the gate is an **extension of an existing mechanism**, not a new one, and it deletes the lock in the commit that lands it (epic fork **F9**, ratified by the operator: *"Definitely dont want a second mechanism."*). §6.2's "the gate applies to every invocation of `start-bot.sh`", Goal 2's "no per-supervisor expression of that policy anywhere" and §12's "do not add a … ordering anywhere" would all have been falsified on delivery had the lock survived.

This spec makes the divergence impossible rather than patching it. Boot policy becomes ONE value (`BootPolicy`) composed into ONE artifact (`bot.conf`), which both supervisors already deliver identically. Bring-up becomes ONE mechanism (a priority admission gate inside `start-bot.sh`, the one program both supervisors run) that every bring-up on the host goes through: cold boot, keepalive bounce, rolling restart, manual spin-up. Boot progress becomes ONE platform-neutral signal (a marker the core writes itself). Every `systemctl` and `launchctl` invocation moves behind ONE adapter with five verbs, and the two unit renderers consume ONE `SupervisionSpec`. Three tests pin the three boundaries so the next divergence fails in CI instead of on a host. Nothing needs configuring: every default ships in the package tier of `system.yaml`, and the slot count is derived from the host.

## 2. Evidence

Originally taken at `1053659`. **Re-verified at `97cdae4`** (main after PR A #1684) on 2026-09-21; PR A moved several of these, and the current lines are given below. Every citation here was re-checked by grep at that tip.

- **`lib/start-bot.sh:14-47` — the #304 boot lock (added 2026-09-21).** The host-wide serializer the original evidence block missed entirely. Platform-neutral. Its staleness test compares `date +%s` against a `stat` mtime, so a forward clock step force-claims a live lock and a backward step makes the age negative and the lock is never cleared. `lib/validate-bot-change.sh` neuters it at six sites (`:1416, :1523, :1565, :1625, :1703, :1836`, `BOOT_LOCK_HOLD_S=0`), which is why no existing scenario can observe it.
- `claudlobby/supervision.py:120-123` — `render_systemd_unit(spec, *, boot_delay_s)` renders `ExecStartPre=/bin/sleep N` (PR A moved this out of `composer.py`; `compose_systemd_unit` is now the thin caller at `claudlobby/composer.py:1446-1455`). `render_launchd_plist(spec)` at `:176` takes no rung. The comment at `claudlobby/supervision.py:172-175` says the DRY fix would be "a BOOT_DELAY env var honored by start-bot.sh itself". It was never built — and it is now evaluated as a real alternative rather than quoted in passing (epic fork **F16**).
- `claudlobby/composer.py:3379` — `_BOOT_STAGGER_SECONDS = 3`; `bot_boot_delay_s` at `:3489-3513` computes a host-global ladder (managers first, then workers, across fleets), via `_host_boot_rung_bases` at `:3427`. Only the systemd renderer consumes it.
- `claudlobby/supervision.py:141-145` — the unit's own comment: `Type=simple` + a spawner `ExecStart` + `RemainAfterExit=yes` is what makes systemd's SubState a boot-progress signal, and `service_is_starting` (`lib/lib-common.sh:4216-4275`) reads `activating` as "the ExecStartPre stagger". `service_is_starting` returns 1 on any non-Linux OS at `:4218`, so a launchd host has no boot-progress signal for keepalive at all. **It has four call sites, not the two its header names** — `lib/keepalive.sh:324`, `lib/fleet-pulse.sh:307`, `lib/fleet-pulse.sh:824`, `lib/validate-bot-change.sh:2469` — and three of them sit behind a `[ -n "$BOT_SERVICE" ] &&` short-circuit that makes the new marker rung unreachable for a bot whose service name is empty.
- `lib/lib-common.sh:4170-4182` — `_BOOT_GRACE_S_DEFAULT = 300`, the bound on how long that predicate may call a unit mid-boot. Its comment states a budget this design **falsifies**: "the ExecStart phase is bounded by start-bot.sh's own `RC_READY_TIMEOUT_S` (90s default), so 300s is >3x headroom" — the gate moves the host-wide wait *into* `ExecStart`. Its purpose (the #933 manufactured-all-clear bound) stands and is re-based onto launcher liveness (see §6.3 and epic fork **F15**).
- `lib/keepalive.sh:188-211` — the restart ladder is an OS `if`/`elif` with `systemctl --user restart` and `launchctl kickstart -k` inline; `_bridge_heal` (`:222-262`) bounces through it, with a per-bot attempt budget and a per-bot lock but no host-wide admission. **Every branch ends in `start-bot.sh`** — directly, or as the supervisor's `ExecStart` — which is why the gate catches every keepalive bounce with the ladder left exactly as it is.
- `lib/keepalive.sh:88-99` — the shipped pid-reuse remedy (`59457c0`, #1425): `kill -0` **paired with** `marker_age_within`, because "`kill -0` alone let a RE-USED pid block a bot indefinitely — proven live within minutes of deploy". Any new liveness test in this design uses the pair, never `kill -0` alone.
- The readiness ceiling: PR A extracted it into `wait_bridge_ready_state` (`lib/lib-common.sh:1259-1302`), wall-clock and carrying an explicit clock-step fold (`_step_s = max(timeout_s, 60)`; a tick whose delta is negative or exceeds `_step_s` shifts the origin instead of counting, `:1268-1274`, `:1293-1298`). Before that it was `_rc_iters = RC_READY_TIMEOUT_S * 2` iterations of a probe plus `sleep 0.5`; under the boot's load each probe cost about 1.7 s, so "90 s" read as five minutes (measured on 2026-09-19: `POLL_START` 18:02:23, `TIMEOUT` 18:07:25). **The gate's own wait must reuse that fold**, on a host class whose clock steps during boot.
- `lib/start-bot.sh:268-276` — every start ran `claude plugin update` for every required plugin (46 runs on the day's boots); PR A's `plugin_ensure` (`lib/lib-common.sh:4037`) now gates that on a per-(boot epoch, plugin) stamp written under `with_lock` at `$CLAUDLOBBY_ROOT/state/boot/plugins.lock` (`:4071-4072`). Those stamps have **one writer and no other reaper**; nothing else may delete them.
- Direct `systemctl`/`launchctl` invocations in `lib/`: the landed ratchet allowlist totals **109** across 20 scripts (`lib-common.sh` 15, `setup-fleet` 11, `keepalive.sh` 9, `rehearse-briefing-timer.sh` 9, `reconcile-fleet.sh` 8, `validate-bot-change.sh` 7, `rehearse-keepalive-swap.sh` 6, `coldstart-harness.sh` 5, `install-host-service-systemd.sh` 5, `spin-down-bot.sh` 5, `install-bot.sh` 4, `install-bot-systemd.sh` 4, `migrate-fleet-to-system.sh` 4, `spin-up-bot.sh` 3, the rest smaller). The original figure of 108 was the pre-test estimate. `CALL_PATTERN` counts **comments and help strings** as well as calls, by design — so the five boot-path scripts' post-migration floors are 3 / 0 / 1 / 1 / 1, not zeros, and only `spin-up-bot.sh` can leave the list.
- `lib/lib-common.sh:3928` — `boot_rung_for` reads the rung back out of the systemd unit text (`-1` on a launchd host). Its consumers are `lib/selfstart-snapshot.sh:692`, `lib/boot-capture.sh:161` and `:253`, and `tests/test_selfstart_snapshot.sh:1181` — **three files, not one**, and `boot-capture.sh` is part of the instrumentation this design's own proof reads.
- `lib/lib-common.sh:347` — `with_lock` is `flock` where present, else an mkdir lock directory with a 30 s budget (and a waiter that gives up **runs unlocked**, `:355-363`); `:3031` — `marker_age_within <marker> <max_age_s>`, the estate's "marker younger than N" predicate; `:3887` — `resolve_boot_epoch` answers on both OSes (`uptime -s`, `kern.boottime`, `/proc/uptime`); `:466` — the source-time `trap '_lc_cleanup' EXIT`, which any later `trap … EXIT` **replaces** (`:463-465` states the convention; measured).
- `lib/rolling-restart.sh:86-104` — `rr_bot_ceiling` derives the per-bot budget as `RC_READY_TIMEOUT_S + 120`, enumerating what the margin covers; `lib/weekly-worker-restart.sh:88-97` carries the same derivation. A gate that adds a wait *inside* that interval either shares this derivation or contradicts it; this design shares it (the hold ceiling **is** `ready_timeout_s + 120`).
- Claude Code documents `MCP_TIMEOUT` (milliseconds) as the MCP server startup timeout, with no default stated; `MCP_TOOL_TIMEOUT` is the per-call limit and is not involved.
- Recovery on 2026-09-19: with the bounce loop paused and in-flight bring-ups drained, the last dark bot restarted alone reached `BRIDGE_READY` in 58 s; restarted alone on a calm host, the same bot's MCP phase took 1 s and named the Telegram server. Serialization is the whole difference.

## 3. Goals

1. A bot's boot policy is defined once, composed into `bot.conf` once, and read by the runtime from there only.
2. Every bring-up on a host passes through one admission gate that bounds concurrency and orders managers first, with no per-supervisor expression of that policy anywhere.
3. Keepalive can tell "mid-boot" from "dead" on both OSes from one signal the core writes itself.
4. No `systemctl` or `launchctl` invocation outside one adapter file, enforced by a test that only ever gets stricter.
5. The two unit renderers cannot learn different facts: a round-trip test parses both outputs and asserts one spec.
6. Zero operator configuration: defaults ship in the package `system.yaml`; the slot count derives from the host.
7. The three 2026-09-19 amplifiers are gone: the MCP startup timeout is set, the readiness ceiling is wall-clock and names why it fired, plugin updates run once per host boot.

## 4. Non-goals

- Migrating every one of the 108 supervisor call sites in one PR. The adapter and the ratchet land first; the boot path migrates; the timer installers, `setup-fleet`, `reconcile-fleet.sh` and the rehearsal scripts migrate afterwards under the ratchet, each a small PR.
- Changing how Claude Code starts MCP servers, or reconnecting a failed MCP server without a session restart (Claude Code offers no non-interactive door for that; the bounce stays the heal, now serialized).
- Repeat-aware or summarized alerts (#1573 ask 2). With the gate in place the storm cannot form; the alert text change is a follow-up if the deploy shows it is still wanted.
- The Pi's self-start instrument beyond reading the new marker: its boot classification (`boot_start_class`, #1043) keeps reading platform facts because it is a measurement, not a decision.
- **Questioning whether every declared bot needs a live session at boot at all.** This design optimizes the *simultaneity* of an 18-to-21-way bring-up and never asks whether the population needs to exist — and the estate already holds the pieces for the alternative (`spin-down-bot.sh` as the RAM lever; an `INBOUND-WOKEN` class in `lib/selfstart-snapshot.sh` for bots that come up on demand and work normally). It is **not ruled out on the merits here**; it is out of scope because a lazy bring-up changes what a fleet *is*, which is a mission decision and not a supervision one. Recorded so the next reader knows the gate is bounding a population nobody re-examined, rather than one that was defended.

## 5. Principles

Ask what a supervisor does for us: it starts our launcher at boot, keeps our unit registered, can kick it, and reports whether it is loaded. Everything else about how a bot comes up is our logic. The design has three layers, each with one owner and one test at its boundary.

| layer | owner | may differ per OS? | pinned by |
|---|---|---|---|
| core process | `start-bot.sh`, `keepalive.sh`, the lib doors; policy from `bot.conf`; state from markers | never | the ratchet: no supervisor binary invoked here |
| adapter | one file, five verbs, two spellings each | only inside the verb bodies | contract tests run against fake `systemctl` and fake `launchctl`, asserting identical outcomes |
| renderer | two writers, one `SupervisionSpec` | idioms only (`RemainAfterExit` vs `KeepAlive`) | the round-trip equivalence test |

A difference is allowed only as two spellings of the same verb or two idioms for the same spec property. Nothing else may differ, and the tests are what make "may not" mean "cannot".

## 6. Architecture

### 6.1 `BootPolicy`, the one truth

A frozen dataclass in a new module `claudlobby/boot.py`, computed once per bot at compose time:

| field | source | default | rendered into `bot.conf` as |
|---|---|---|---|
| `admission_slots` | package `system.yaml` `host.boot.admission_slots`; `auto` derives `clamp(cpu_count // 4, 1, 4)` at compose time — **under fork F14**, which proposes deriving at *acquire* time instead, because a synced fleet overlay means whichever machine ran `generate` bakes *its* core count into the target host's `bot.conf` | `auto` | `BOOT_ADMISSION_SLOTS` |
| `admission_wait_max_s` | `host.boot.admission_wait_max_s` — **under fork F13**, which proposes deriving it from `ceil(bots_on_host / slots) × hold_ceiling_s`, because measured drain exceeds the flat value on both first-class hosts | `1200` | `BOOT_ADMISSION_WAIT_MAX_S` |
| `hold_ceiling_s` | derived: `ready_timeout_s + 120` — the **same** margin `lib/rolling-restart.sh:86-104` already derives for its per-bot budget, so the gate's hold and the restart drivers' window cannot be two numbers | `320` with the defaults | `BOOT_HOLD_CEILING_S` |
| `boot_grace_s` | derived: `admission_wait_max_s + ready_timeout_s` (epic fork F15). Replaces `_BOOT_GRACE_S_DEFAULT = 300` as the source of truth; `KEEPALIVE_BOOT_GRACE_S` stays the fallback for an un-regenerated `bot.conf`, exactly the shape F4 locks for `RC_READY_TIMEOUT_S` | `1400` with the defaults | `BOOT_GRACE_S` |
| `priority` | `fleet.manager_bots()` | managers `0`, workers `1` | `BOOT_PRIORITY` |
| `mcp_timeout_ms` | `host.boot.mcp_timeout_ms` | `180000` | `MCP_TIMEOUT` (exported; read by Claude Code) |
| `ready_timeout_s` | derived: `max(90, mcp_timeout_ms // 1000 + 20)` — one number, so the readiness ceiling can never be shorter than the MCP startup timeout it waits on (plan fork F3); the existing `RC_READY_TIMEOUT_S` env var stays as a fallback for an un-regenerated `bot.conf` (plan fork F4) | `200` with the default MCP timeout | `RC_READY_TIMEOUT_S` |
| `plugin_update_once_per_boot` | `host.boot.plugin_update_once_per_boot` | `true` | `BOOT_PLUGIN_UPDATE_ONCE` |

The `host.boot` block is host-scoped like `host.jobs`: an operator's `system.yaml` may override a key; nobody has to. `bot.conf` is already the single carrier both supervisors deliver, and it is read at session start by `start-bot.sh` under `set -a`, so every key reaches both the launcher and the `exec claude` environment with no new plumbing. Each key is rendered exactly once; a conformance test asserts that, and asserts that neither rendered unit contains a sleep, a timeout, or any `BOOT_*` value.

### 6.2 The admission gate, in `start-bot.sh`

State lives under `$CLAUDLOBBY_ROOT/state/boot/<boot-epoch>/`: `tickets/` and `slots/`. The gate **replaces the #304 boot lock in place** (§1 CORRECTION, epic fork F9): same file, same position — immediately after `load_bot_conf` at `lib/start-bot.sh:12`, above the seeding and plugin phases the stagger was spreading and above the session teardown at `:135`, so a queued restart leaves the live session up while it waits (epic fork F10) — and the same atomic primitive, done properly.

> **CORRECTION (2026-09-21) — the scope.** "Host-wide by construction, since the root is host-wide" is **false**. `CLAUDLOBBY_ROOT` is per-**install**, exported from each bot's `bot.conf` and sourced at `lib/start-bot.sh:12`, and a host routinely carries more than one root (a live install, a maintainer checkout, the exported trees the harnesses create). The bound is **per install root**: a bring-up from a second root is invisible to the gate, and a hand run is covered only when it uses the same root. The directory choice stands (epic F2, locked); the claim does not.

> **CORRECTION (2026-09-21) — epoch keying.** The gate's own state is keyed on `resolve_boot_epoch`, as this directory's two existing tenants already are (`plugins-updated.<epoch>.<plugin>`; `runtime/_host/boot-capture/<epoch>`). A prior-epoch tree is stale **unconditionally**, never by reading a pid a reboot has made meaningless: pids restart low, so a slot surviving a power loss or a hard reset is near-certain to name a live process afterwards — every waiter would see a live holder and the host would queue to the cap at exactly the boot the gate exists for.

Sequence, executed by `start-bot.sh`:

1. Write `$BOT_DIR/data/.boot-queued` (the boot-progress marker, see 6.3) **carrying the launcher's pid**, and a ticket `tickets/<priority: 1 digit>-<arrival: exactly 19 digits>-<unit-name>`. One arrival format, not a disjunction: `date +%s%N`, validated all-digits and width 19, else `printf '%010d%09d' "$(date +%s)" 0` — the same quantity at second resolution, so it sorts *with* ns stamps rather than before every one of them. The unit name comes from `svc_unit_name`; if it resolves empty the gate logs loudly and proceeds **ungated** rather than sharing a ticket path with another bot.
2. Loop: reap stale tickets, slots and markers; if any ticket sorts before ours (lexicographic: priority, then arrival), wait; else if a slot directory `slots/<n>` for `n < BOOT_ADMISSION_SLOTS` can be created atomically (`mkdir`), take it, record our pid inside, remove our ticket, and continue. Lexicographic sort on a zero-padded priority and a fixed-width epoch is the entire ordering rule, so "managers first, then arrival order" is a property of the ticket name, not a second mechanism.
   - **The take is two operations** (`mkdir`, then the pid file), so a slot with no pid file **younger than a stated claim grace is HELD, never reapable**. A reader that cannot see the holder must not answer the same as one that found no holder, and here the unreachable answer would license a *delete*.
   - **Reclaim is one atomic rename** to a unique non-existent name (`mv slots/<n> slots/.reap.<unique>`, then `rm -rf`), not `rmdir` + `mkdir`: exactly one of N concurrent reclaimers wins. `mv` onto an *existing* directory is not a mutex — it moves the source inside it — which is why `mkdir` is the take primitive and `mv`-to-a-fresh-name is only the reclaim primitive.
   - **Liveness is `kill -0` paired with `marker_age_within <slot>/pid <hold_ceiling_s>`**, the remedy `lib/keepalive.sh:88-99` already ships for #1425. `kill -0` alone is unsound under pid reuse. A slot past the hold ceiling is reaped whatever the pid says.
   - The loop polls every 2 s and **carries `wait_bridge_ready_state`'s clock-step fold verbatim**, for the arrival stamp and the marker-freshness read as well as the elapsed count. Without it a single forward NTP correction during boot makes every waiter's elapsed time exceed the cap in the same instant.
   - The reap covers tickets, slots and markers. It does **not** touch `plugins-updated.*` stamps: those are written under `with_lock`, and an unlocked delete from every waiter every 2 s would re-arm the per-start `claude plugin update` the amplifier fix removed.
3. Past the waiter's **effective** cap, proceed anyway: remove our own ticket **first** (a timed-out launcher that keeps its ticket head-of-lines every later waiter for the rest of its life), log `ADMISSION_TIMEOUT` with the queue length and the held slots, emit `boot_admission_timeout`, and go on without a slot. A gate that could strand a bot forever would be the failure it replaces.
   - **The cap is dispersed per ticket**: `effective_cap = admission_wait_max_s + arrival_rank × (ready_timeout_s / slots)`. Every ticket at a cold boot is minted within the same second, so a flat cap expires for every waiter in the same second and the escape releases the whole remaining queue at once into a host that is by then provably saturated — the storm it replaces, delayed and concentrated. Dispersion degrades one bot at a time. (Whether the cap's *base* is also derived rather than flat is epic fork **F13**.)
4. Create the session (which touches `data/.spawn` as today), run the readiness poll, then **release the slot, the ticket and the marker together**. A trap on exit releases them too — and that trap must name `_lc_cleanup` explicitly, because `lib-common.sh` sets its own EXIT trap at source time and a later bare `trap … EXIT` replaces it. A `kill -9` leaves a slot whose pid is dead, which the next waiter reaps.

Log lines: `ADMISSION_WAIT queue=<n> slots=<held>/<max> priority=<p>`, `ADMISSION_GRANTED slot=<n> after <s>s`, `ADMISSION_RELEASED` — written to `$BOT_DIR/logs/startup.log`, which the gate sets up itself. **Only the verdict goes to stdout**, because the caller captures it. Events: `boot_admission_timeout` and `boot_admission_unavailable`; the normal path is not an event, **but it is a measurement** — the grant line's wait and the hold are recorded as metric samples, or the slot formula, the cap and the hold ceiling cannot be revised from the very run that was supposed to validate them.

The gate applies to every invocation of `start-bot.sh`, which is every bring-up on the install: the supervisors' boot launch, keepalive's restart ladder (both spellings end in `start-bot.sh`), `rolling-restart.sh`, `spin-up-bot.sh`, and a hand run. A keepalive bounce launched during another bot's MCP phase therefore queues instead of colliding, which closes #1573 ask 1 without touching the bounce's own budget. **A caller that brings bots up serially is already the bound** and must not also pay the queue's cap: `keepalive-all.sh`, `rolling-restart.sh`, `weekly-worker-restart.sh` and `spin-up-bot.sh` set a scoped `BOOT_ADMISSION_CALLER_CAP_S` — a `lib/`-internal call convention, never an operator key, and deliberately not `BOOT_ADMISSION_WAIT_MAX_S`, whose precedence F4 locks the other way.

### 6.3 One boot-progress signal

`service_is_starting` gains a first rung on both OSes: a `.boot-queued` marker whose recorded launcher pid is **live** and whose age is within `BOOT_GRACE_S` means starting. The existing systemd SubState read stays as a Linux-only second rung. Keepalive's dead-session branch already consults `service_is_starting`, so a queued bot on a launchd host is left alone instead of being kickstarted into a livelock. `data/.spawn` keeps its current meaning (session created) and its current readers.

> **CORRECTION (2026-09-21) — the marker has ONE lifetime: acquire → RELEASE.** The original text sized the window at `BOOT_ADMISSION_WAIT_MAX_S + RC_READY_TIMEOUT_S`, which only makes sense if the marker survives the readiness poll, while §6.2 step 4 deleted it *at grant* — making the second term dead arithmetic and leaving both readings with a named live failure. **Deleted at grant:** on launchd `service_is_starting` returns 1 unconditionally, so between grant and the session spawn — a span containing the whole plugin/marketplace block with a `with_timeout 60` per unregistered marketplace — a bot has no marker, no session and no boot signal, and keepalive's dead-session branch restarts it. That is the #1002 livelock moved, not closed. **Kept until release:** 1400 s of mtime against an existing deliberate cap of 300 s whose own comment says why it exists — "without it a wedged spawner would suppress both the restart and the alarm forever … the manufactured all-clear shape (#933)".
>
> The resolution is not to pick a number but to change what the window is a statement **about**. The marker carries its launcher's pid; the rung requires that pid to be **live**; and the reaper removes a marker whose launcher is dead. A SIGKILLed or OOM-killed launcher therefore stops suppressing the watchdog within one poll — measured: bash runs an EXIT trap on SIGTERM but not on SIGKILL, which is exactly the case 300 s of mtime could never cover — while a *live* launcher legitimately inside its own wait is not restarted out from under itself. `boot_grace_s` is then derived (`admission_wait_max_s + ready_timeout_s`, epic fork F15) rather than being a second, independently-set number, and `_BOOT_GRACE_S_DEFAULT`'s comment is rewritten in the same commit that falsifies it.
>
> The marker is written by **every** `start-bot.sh` run, not only a boot one, so it inherits the `.spawn` lesson `lib/boot-capture.sh:19-25` records: "after a restart it is not missing, it is a plausible timestamp describing a DIFFERENT event, so a later reader gets a confident wrong answer rather than an absent one." Any reader that must tell a boot-queue from a restart-queue reads the boot epoch stamped beside the pid.

### 6.4 The three riders

- `MCP_TIMEOUT` exported from `bot.conf`. Claude Code documents it as the MCP server startup timeout in milliseconds.
- The readiness poll measures `date +%s` against `RC_READY_TIMEOUT_S` and stops on wall-clock time; the `TIMEOUT` line names the last `bridge_state` result (`no_bridge`, `not-ours`, `no_token`, `unknown`) so the log says why, not only that.
- Plugin updates: install-if-missing stays as it is at every start; `claude plugin update` runs at most once per host boot, guarded by a stamp `state/boot/plugins-updated.<boot-epoch>` written under `with_lock`; `resolve_boot_epoch` supplies the epoch on both OSes, and an unresolvable epoch falls back to today's per-start behaviour with a log line saying so.

### 6.5 The supervisor adapter

One file, `lib/supervisor.sh`, sourced by `lib-common.sh`, owning five verbs: `svc_enroll <bot_dir>`, `svc_disenroll <bot_dir>`, `svc_kick <bot_dir>`, `svc_state <bot_dir>` (prints one of `loaded-active`, `loaded-inactive`, `not-loaded`, `unknown`), `svc_is_registered <bot_dir>`. Each verb has a systemd body and a launchd body selected by the existing `detect_os`, and each resolves the unit name the same way keepalive does today (`BOT_SERVICE` from `bot.conf`, with the pre-rename fallback kept until its last user is gone). Contract tests run every verb twice, against a fake `systemctl` and a fake `launchctl` on PATH, and assert the same observable outcome (which unit was named, which action was requested, the printed state), so a verb cannot gain a behaviour on one OS only.

The ratchet: `tests/test_supervisor_ratchet.py` greps `lib/` for direct `systemctl` or `launchctl` invocations outside `lib/supervisor.sh`, compares against a checked-in allowlist of `(script, count)` pairs measured when the test is written, and fails on any new site or any count that grew. Shrinking is free. This is the pattern `tests/test_defaults_registry.py` already uses for grandfathered defaults.

### 6.6 The renderer boundary

`claudlobby/supervision.py` gains `SupervisionSpec` (label, bot directory, launcher path, environment, stop command, log paths, root). Both writers take one spec and emit their format; `compose_systemd_unit` loses `boot_delay_s` and the `ExecStartPre` line; `compose_launchd_plist` changes only its signature. The idiom table is the whole allowed difference:

| spec property | systemd idiom | launchd idiom |
|---|---|---|
| launcher exits, session survives | `Type=simple` + `RemainAfterExit=yes` + `KillMode=process` | `RunAtLoad` with the launcher as `ProgramArguments`; `KeepAlive` keyed on `SuccessfulExit=false`, so a clean launcher exit is not restarted |
| restart on launcher failure | `Restart=on-failure`, `RestartSec=5` | `KeepAlive` `SuccessfulExit=false` |
| environment the launcher needs | `Environment=` lines (`CLAUDLOBBY_ROOT`, `TMUX_TMPDIR`) | `EnvironmentVariables` dict with the same two keys plus `PATH` and `HOME`, which a LaunchAgent does not inherit; `tests/test_composer.py::test_launchd_systemd_path_parity` already pins that pair and the round-trip test generalizes it |
| stop tears the session down | `ExecStop` kills the bot's tmux server; `ExecStopPost` removes `.tmux-env` | launchd has no stop hook: `bootout` only signals the launcher, which has already exited. This is a PROPERTY the plist cannot carry, so the adapter's `svc_disenroll` performs the same teardown (kill the bot's tmux server, remove `.tmux-env`) on launchd, and the round-trip test asserts the spec's stop command appears in the systemd unit AND is what `svc_disenroll` runs on launchd (a contract assertion, since there is no unit text to parse) |

`tests/test_supervision_roundtrip.py` renders both for every bot in a fixture fleet, parses the unit as INI and the plist as XML, maps each back to a spec, and asserts equality. The one property a plist cannot carry (the stop hook) is asserted through the adapter instead, so the equivalence covers OUTCOMES: the same launcher, the same environment keys, the same restart rule, the same teardown, on both supervisors. A fact one renderer learns that the other does not is a failing test, which is the property asked for.

`boot_rung_for` is retired with the stagger; `selfstart-snapshot.sh`'s "not yet due" becomes "a fresh `.boot-queued` marker, or no `.spawn` yet within the admission window", which is truthful on both OSes and needs no arithmetic.

## 7. Data flow

compose (`BootPolicy` from the package host section) → `bot.conf` → supervisor starts `start-bot.sh` → gate (ticket, slot) → session → readiness poll → release → `BRIDGE_READY` or `TIMEOUT <why>`. Keepalive tick → `service_is_starting` (marker first) → if dead, `svc_kick` → `start-bot.sh` → gate. Plugin update → once per boot epoch under the host lock.

## 8. Failure posture

- A slot holder that dies without releasing: reaped by the next waiter on **paired** liveness (`kill -0` *and* a freshness window on the pid file), never on `kill -0` alone. A holder that lives forever (a hung readiness poll) releases at its own ceiling, which is now wall-clock, and is reaped past `hold_ceiling_s` whatever its pid says.
- A slot whose `mkdir` has landed but whose pid file has not been written yet: **HELD** inside the claim grace, never reapable. Absence of evidence is not evidence of absence, and here it would license a delete that puts two launchers on one slot.
- **The gate degrading into a synchronized release** — the anti-pattern this design has to name, because two of its failure paths share it. Every ticket at a cold boot is minted in the same second, so anything that expires "after N seconds" expires for all of them at once. The cap is dispersed by arrival rank; the clock fold stops an NTP step from doing the same thing in one instant; and a test with `waiters = 3 × (cap/hold)` asserts releases are spread rather than simultaneous.
- The wait cap: remove our ticket, then proceed, disclosed, evented. Never strand, and never head-of-line every later waiter for the rest of this launcher's life.
- No `state/boot/` yet: created on first use; unwritable → proceed without a gate, one loud log line, `boot_admission_unavailable` event. The gate degrades to today's behaviour, never to a stuck bot.
- A wrong `BOOT_ADMISSION_SLOTS` value (empty, non-numeric, zero): treated as 1 with a log line; the composer never emits such a value, and a hand-edited one must not crash a boot under `set -u`.
- **A `bot.conf` with no `BOOT_*` keys at all** — an un-regenerated fleet — is a *third* state beside those three, and the one "zero operator configuration" actually rests on. Every read is `${KEY:-<composed default>}`, so such a fleet degrades silently to a one-slot gate with today's timings rather than aborting under `set -u`.
- Boot epoch unresolvable: plugin update runs per start as today, disclosed.
- The adapter on an OS it does not know: every verb prints `unknown` or returns 1 with a log line; nothing is invoked.

## 9. Testing

- Composer: `BootPolicy` defaults, the `auto` slot derivation (monkeypatched cpu counts), each key rendered exactly once into `bot.conf`, neither unit carrying policy, the round-trip equivalence, the systemd writer no longer accepting a rung.
- Bash suites (`tests/test_boot_admission.sh` through the existing `test_sh_suites.py` wrapper): priority order (a worker ticket waits behind a later manager ticket), slot cap, stale reclaim, wait cap with the event, release on exit and on trap, the marker's lifecycle, plugin-once stamp keyed on the epoch.
- Adapter contract tests with fake binaries; the ratchet with its allowlist.
- The harness gains one scenario: two throwaway bots started together with one slot; the second shows `.boot-queued`, a keepalive tick leaves it alone, it launches after the first's readiness poll ends; zero real Telegram, the stubbed `claude` as in every scenario.
- The standing gates: two-leg names-plus-counts, the harness run directly after any `lib/` change, committed-code mutants, CI on Linux, the macOS red baseline compared by name.

## 10. Rollout

Two PRs, each through the standing gauntlet and deployed to the host one manager at a time:

- **PR A** — `BootPolicy` into `bot.conf` with `MCP_TIMEOUT`, the wall-clock ceiling naming why, plugin-once, `SupervisionSpec` and the round-trip test (the systemd writer still emitting the rung until B), `lib/supervisor.sh` with the five verbs, the contract tests, and the ratchet with every current site grandfathered. Deploy: pull, generate both fleets, one manager restart each to confirm the new `bot.conf` keys in the session environment. This PR alone may prevent most of the failure.
- **PR B** — the gate (replacing the #304 lock), the marker, `service_is_starting` wired into **all four** of its consumers, the harness scenario with its positive control, and the proof. Deploy: pull, generate, one manager restart each, **to the primary host only**. Proof: **two** planned reboots — one on PR A alone as the control, taken *before* the gate is built, and one on A+B — read on the gate's own `ADMISSION_GRANTED` distribution rather than on an aggregate, plus the bridge tally, the alert tally (`session_missing`, `service_down`, `bridge_down` — not `bridge_down` alone, which a gate that stalled every bot would satisfy perfectly), `lib/selfstart-snapshot.sh`'s page taken before any rescue, and `lib/bench-cold-start.sh` before and after.
- **PR C** — the systemd stagger, `bot_boot_delay_s`, `_host_boot_rung_bases` and `boot_rung_for` retired, the rung's three consumer files re-based onto the admission window (`selfstart-snapshot.sh`'s `VALID_AT` and `boot-capture.sh`'s `BOUND_AT` given the **same** replacement bound so the two cannot fork), and the adapter migration of the five boot-path scripts if F12 lands that way.

**The 2026-09-19 figure ("7 of 18") is NOT COMPARABLE** and is printed as such wherever it appears: it was produced by an incident rather than by this instrument, and the same rule `lib/selfstart-snapshot.sh` already applies to its own prior figure applies here. PR B's control reboot is the comparable baseline.

All three PRs change `lib/` scripts, which the host reads **on demand at each bring-up** — so the `git pull` **is** the rollout and the manager restart is a check, not a gate. A revert is a pull. `bot.conf` keys land at generate and are inert without the scripts that read them. Whether the highest-blast-radius `lib/` change in the program also ships behind a temporary per-fleet arming flag is epic fork **F17**, open.

## 11. Decisions ratified (2026-09-20)

1. Admission slots, not a ladder (the ladder's number was wrong by ten times and would be guessed again; slots serialize bounces too). *Re-affirmed 2026-09-21 against the composed-delay counter-plan, epic fork F16: the criticism was of the ladder's number, not of where the delay lives, so the counter-plan was owed an evaluation rather than a sentence — it is recorded, and rejected because a delay cannot serialize a keepalive bounce or a mid-phase rolling restart, which is the case #1573 ask 1 is about.*
2. Host-level, in the package `system.yaml`, zero operator configuration; slots derived from the host. *Where "from the host" is resolved — compose time or acquire time — is epic fork **F14**, open: under a synced fleet overlay, compose-time derivation bakes the composing machine's core count into the target host's `bot.conf`.*
3. Managers first as ticket priority inside the one gate; no second mechanism. *Strengthened 2026-09-21 by the operator's ruling — "Definitely dont want a second mechanism" — which makes the pre-existing #304 boot lock's **deletion** part of the delivery rather than a tidy-up (epic fork F9).*
4. Defaults: `auto` slots (1 on a four-core host, 2 to 3 on the macOS host), 180 s MCP timeout, a readiness ceiling derived from it (200 s), 20 minute wait cap. *The wait cap is under epic fork **F13**, open: measured drain (`ceil(bots/slots) × hold_ceiling`) is 4200 s on the four-core host and 1800 s on the launchd host at 2 slots, so the flat 1200 s is the design's throughput ceiling rather than a margin. Two derived keys are added beside these — `hold_ceiling_s` and `boot_grace_s` (F15) — neither an operator choice.*
5. Two PRs as above, mechanical gates only. *Amended 2026-09-21: **three**. The systemd stagger retirement moves to PR C (epic fork F11, locked by the operator's "deploys only to the primary host, never to the Pi" — it changes the behaviour of the one host class this program does not deploy to), and the adapter migration of the five boot-path scripts is epic fork F12, open, leaning PR C.*

## 12. What not to do

- Do not add a per-supervisor sleep, timeout or ordering anywhere; the conformance test forbids it and so does the spec.
- **Do not leave a predecessor in place.** The same forty lines at the head of `lib/start-bot.sh` have been reworked seven times (#87, #304, #1002, #1050, #1530, #1570, PR A) and none of the seven removed one. The gate is the eighth and the first to claim consolidation, so it deletes the #304 lock in the commit that lands it, drops the six harness sites that neuter that lock, and is pinned by a grep assertion that `start-bot.sh` holds no second serialization primitive.
- **Do not let a failure path proceed all at once.** "Proceed, disclosed" is the right posture and it is not enough on its own: at a cold boot every waiter reaches the same failure path in the same second.
- **Do not reap on `kill -0` alone, and do not reap what you merely cannot see.** Both are answers to an unreachable holder, and neither may license a delete.
- Do not invoke `systemctl` or `launchctl` outside `lib/supervisor.sh` in any file this work touches.
- Do not make the gate a hard requirement: every failure path proceeds and discloses.
- Do not touch `library/`, fleet manifests, or any operator `.env`.
- Do not widen the scope to the 80-odd grandfathered call sites; they shrink in follow-ups under the ratchet.

## 13. Success criteria

- [ ] `tests/test_supervision_roundtrip.py` passes and fails when either renderer is given a fact the other lacks (shown with a temporary edit, restored).
- [ ] `tests/test_supervisor_ratchet.py` passes with the allowlist and fails on one added direct call (shown, restored).
- [ ] Both rendered units carry no timeout and no `BOOT_*`; `bot.conf` carries each policy key once. (The no-`sleep` half lands with PR C.)
- [ ] `tests/test_boot_admission.sh`: priority (seeded deterministically, never raced), cap, stale reclaim, the pid-less claim grace, concurrent reclaim, **a reused pid**, the wait cap with its ticket removal, a clock step mid-wait, a dispersed over-cap release, trap release with `_lc_cleanup`, the marker lifecycle, the marker reaper, unavailable, epoch keying, a `%N`-less `date`, and the zero/one cases — all green.
- [ ] `lib/start-bot.sh` holds **no** second serialization primitive (`grep -n 'BOOT_LOCK' lib/ -r` is empty), and `service_is_starting` is passed a bot dir at **all four** call sites with no `[ -n "$BOT_SERVICE" ] &&` short-circuit in front of any of them.
- [ ] Harness scenario green **with its positive control** (the same scenario at 2 slots shows both granted at once), and the harness's macOS pass/fail pair matches the number recorded in the PR plan **by name** — it will move, because PR B adds checks.
- [ ] Live after PR B, read against the **PR-A-only control reboot**: every bot's startup log carries `ADMISSION_GRANTED`; grants are monotonic and managers-first; zero `boot_admission_timeout`; zero `boot_admission_unavailable`; zero `session_missing` / `service_down` / `bridge_down`; and no `RESTART` line in any `keepalive.log` inside a window that extends **past the wait cap**. The expected completion time is derived from the host's resolved slot count and recorded *before* the reboot, so a slow-but-correct boot is not read as a failure.

Linear: neither.
