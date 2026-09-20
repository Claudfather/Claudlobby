---
title: Boot admission and supervision consolidation
type: spec
status: draft
date: 2026-09-20
issue: "#1573"
supersedes: none
---

# Boot admission and supervision consolidation

## 1. Summary

After a cold boot of an 18-bot macOS host on 2026-09-19, 11 of 18 Telegram pollers never came up, and one fleet's keepalive bounce loop turned that into an alert storm (#1573). The transcripts show why: every session spawns about five MCP servers, all 18 sessions started in the same instant, the MCP startup phase took over five minutes, and the Telegram channel server missed Claude Code's startup timeout and was never retried. Two facts in our own source make that structural: nothing sets the MCP startup timeout, and the boot stagger exists only in the systemd unit, so a launchd host has no stagger at all.

This spec makes the divergence impossible rather than patching it. Boot policy becomes ONE value (`BootPolicy`) composed into ONE artifact (`bot.conf`), which both supervisors already deliver identically. Bring-up becomes ONE mechanism (a priority admission gate inside `start-bot.sh`, the one program both supervisors run) that every bring-up on the host goes through: cold boot, keepalive bounce, rolling restart, manual spin-up. Boot progress becomes ONE platform-neutral signal (a marker the core writes itself). Every `systemctl` and `launchctl` invocation moves behind ONE adapter with five verbs, and the two unit renderers consume ONE `SupervisionSpec`. Three tests pin the three boundaries so the next divergence fails in CI instead of on a host. Nothing needs configuring: every default ships in the package tier of `system.yaml`, and the slot count is derived from the host.

## 2. Evidence (main at `1053659`)

- `claudlobby/composer.py:1334-1339` — `compose_systemd_unit(..., *, boot_delay_s)` renders `ExecStartPre=/bin/sleep N`; `compose_launchd_plist(bot, fleet, paths)` at `:1390` takes no rung. The comment at `:1386-1389` says the DRY fix would be "a BOOT_DELAY env var honored by start-bot.sh itself". It was never built.
- `claudlobby/composer.py:3332` — `_BOOT_STAGGER_SECONDS = 3`; `bot_boot_delay_s` at `:3442-3466` computes a host-global ladder (managers first, then workers, across fleets). Only the systemd renderer consumes it.
- `claudlobby/composer.py:1349-1359` — the unit's own comment: `Type=simple` + a spawner `ExecStart` + `RemainAfterExit=yes` is what makes systemd's SubState a boot-progress signal, and `service_is_starting` (`lib/lib-common.sh:3784-3814`) reads `activating` as "the ExecStartPre stagger". `service_is_starting` returns 1 on any non-Linux OS at `:3784`, so a launchd host has no boot-progress signal for keepalive at all.
- `lib/keepalive.sh:185-208` — the restart ladder is an OS `if`/`elif` with `systemctl --user restart` and `launchctl kickstart -k` inline; `_bridge_heal` (`:222-262`) bounces through it, with a per-bot attempt budget and a per-bot lock but no host-wide admission.
- `lib/start-bot.sh:294-301, 345, 378` — the readiness ceiling is `_rc_iters = RC_READY_TIMEOUT_S * 2` iterations of a probe plus `sleep 0.5`. Under the boot's load each probe cost about 1.7 s, so "90 s" read as five minutes (measured on 2026-09-19: `POLL_START` 18:02:23, `TIMEOUT` 18:07:25).
- `lib/start-bot.sh:268-277` — every start runs `claude plugin update` for every required plugin (46 runs on the day's boots); `reload-fleet.sh` already carries plugin updates daily.
- Direct `systemctl`/`launchctl` invocations in `lib/`: 108 sites across 20 scripts (`lib-common.sh` 15, `setup-fleet` 11, `keepalive.sh` 9, `reconcile-fleet.sh` 7, `spin-down-bot.sh` 5, `install-bot.sh` 4, `install-bot-systemd.sh` 4, `spin-up-bot.sh` 3, the rest in rehearsal and installer scripts). Counted with `grep -cE '(^|[^A-Za-z_-])(systemctl|launchctl)( |$)'` per script at `1053659`; the ratchet's grandfather list is that measurement, re-taken when the test is written.
- `lib/lib-common.sh:3602` — `boot_rung_for` reads the rung back out of the systemd unit text (`-1` on a launchd host); `lib/selfstart-snapshot.sh` derives "not yet due" from it.
- `lib/lib-common.sh:314` — `with_lock` is `flock` where present, else an mkdir lock directory with a 30 s budget; `:3561` — `resolve_boot_epoch` answers on both OSes (`uptime -s`, `kern.boottime`, `/proc/uptime`).
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
| `admission_slots` | package `system.yaml` `host.boot.admission_slots`; `auto` derives `clamp(cpu_count // 4, 1, 4)` at compose time | `auto` | `BOOT_ADMISSION_SLOTS` |
| `admission_wait_max_s` | `host.boot.admission_wait_max_s` | `1200` | `BOOT_ADMISSION_WAIT_MAX_S` |
| `priority` | `fleet.manager_bots()` | managers `0`, workers `1` | `BOOT_PRIORITY` |
| `mcp_timeout_ms` | `host.boot.mcp_timeout_ms` | `180000` | `MCP_TIMEOUT` (exported; read by Claude Code) |
| `ready_timeout_s` | `host.boot.ready_timeout_s` (absorbs the existing `RC_READY_TIMEOUT_S` env override, which keeps working) | `90` | `RC_READY_TIMEOUT_S` |
| `plugin_update_once_per_boot` | `host.boot.plugin_update_once_per_boot` | `true` | `BOOT_PLUGIN_UPDATE_ONCE` |

The `host.boot` block is host-scoped like `host.jobs`: an operator's `system.yaml` may override a key; nobody has to. `bot.conf` is already the single carrier both supervisors deliver, and it is read at session start by `start-bot.sh` under `set -a`, so every key reaches both the launcher and the `exec claude` environment with no new plumbing. Each key is rendered exactly once; a conformance test asserts that, and asserts that neither rendered unit contains a sleep, a timeout, or any `BOOT_*` value.

### 6.2 The admission gate, in `start-bot.sh`

State lives under `$CLAUDLOBBY_ROOT/state/boot/` (host-wide by construction, since the root is host-wide): `tickets/` and `slots/`. Sequence, executed by `start-bot.sh` before it creates the tmux session:

1. Write `$BOT_DIR/data/.boot-queued` (the boot-progress marker, see 6.3) and a ticket `tickets/<priority>-<arrival-epoch-ns>-<bot-service>` holding the launcher's pid.
2. Loop: reap stale tickets and slots whose recorded pid is dead; if any ticket sorts before ours (lexicographic: priority, then arrival), wait; else if a slot directory `slots/<n>` for `n < BOOT_ADMISSION_SLOTS` can be created atomically (`mkdir`), take it, record our pid inside, remove our ticket, and continue. Poll every 2 s. Lexicographic sort on a zero-padded priority and a fixed-width epoch is the entire ordering rule, so "managers first, then arrival order" is a property of the ticket name, not a second mechanism.
3. Past `BOOT_ADMISSION_WAIT_MAX_S`, proceed anyway: log `ADMISSION_TIMEOUT` with the queue length and the held slots, emit `boot_admission_timeout`, and go on without a slot. A gate that could strand a bot forever would be the failure it replaces.
4. Remove `.boot-queued`, create the session (which touches `data/.spawn` as today), run the readiness poll, then release the slot. A trap on exit releases it too; a `kill -9` leaves a slot whose pid is dead, which the next waiter reaps.

Log lines: `ADMISSION_WAIT queue=<n> slots=<held>/<max> priority=<p>`, `ADMISSION_GRANTED slot=<n> after <s>s`, `ADMISSION_RELEASED`. Events: `boot_admission_timeout` only; the normal path is not an event.

The gate applies to every invocation of `start-bot.sh`, which is every bring-up on the host: the supervisors' boot launch, keepalive's restart ladder (both spellings end in `start-bot.sh`), `rolling-restart.sh`, `spin-up-bot.sh`, and a hand run. A keepalive bounce launched during another bot's MCP phase therefore queues instead of colliding, which closes #1573 ask 1 without touching the bounce's own budget.

### 6.3 One boot-progress signal

`service_is_starting` gains a first rung on both OSes: a `.boot-queued` marker younger than `BOOT_ADMISSION_WAIT_MAX_S` plus `RC_READY_TIMEOUT_S` means starting. The existing systemd SubState read stays as a Linux-only second rung. Keepalive's dead-session branch already consults `service_is_starting`, so a queued bot on a launchd host is left alone instead of being kickstarted into a livelock. `data/.spawn` keeps its current meaning (session created) and its current readers.

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

- A slot holder that dies without releasing: reaped by the next waiter on pid liveness. A holder that lives forever (a hung readiness poll) releases at its own ceiling, which is now wall-clock.
- The wait cap: proceed, disclosed, evented. Never strand.
- No `state/boot/` yet: created on first use; unwritable → proceed without a gate, one loud log line, `boot_admission_unavailable` event. The gate degrades to today's behaviour, never to a stuck bot.
- A wrong `BOOT_ADMISSION_SLOTS` value (empty, non-numeric, zero): treated as 1 with a log line; the composer never emits such a value, and a hand-edited one must not crash a boot under `set -u`.
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
- **PR B** — the gate, the marker, `service_is_starting`, keepalive and spin-up and spin-down and the two bot installers on the adapter (the ratchet shrinks by those sites), the stagger and `boot_rung_for` retired, the self-start instrument on the markers, the harness scenario. Deploy: pull, generate, one manager restart each. Proof: a planned reboot of the host, reading the bridge tally against the 2026-09-19 baseline of 7 of 18 ready after the first bring-up, and the self-start instrument's page.

Both PRs change `lib/` scripts, which the host reads at each bring-up, so a revert is a pull; `bot.conf` keys land at generate and are inert without the scripts that read them.

## 11. Decisions ratified (2026-09-20)

1. Admission slots, not a ladder (the ladder's number was wrong by ten times and would be guessed again; slots serialize bounces too).
2. Host-level, in the package `system.yaml`, zero operator configuration; slots derived from the host.
3. Managers first as ticket priority inside the one gate; no second mechanism.
4. Defaults: `auto` slots (1 on a four-core host, 2 to 3 on the macOS host), 180 s MCP timeout, 20 minute wait cap.
5. Two PRs as above, mechanical gates only.

## 12. What not to do

- Do not add a per-supervisor sleep, timeout or ordering anywhere; the conformance test forbids it and so does the spec.
- Do not invoke `systemctl` or `launchctl` outside `lib/supervisor.sh` in any file this work touches.
- Do not make the gate a hard requirement: every failure path proceeds and discloses.
- Do not touch `library/`, fleet manifests, or any operator `.env`.
- Do not widen the scope to the 80-odd grandfathered call sites; they shrink in follow-ups under the ratchet.

## 13. Success criteria

- [ ] `tests/test_supervision_roundtrip.py` passes and fails when either renderer is given a fact the other lacks (shown with a temporary edit, restored).
- [ ] `tests/test_supervisor_ratchet.py` passes with the allowlist and fails on one added direct call (shown, restored).
- [ ] Both rendered units carry no `sleep`, no timeout, no `BOOT_*`; `bot.conf` carries each policy key once.
- [ ] `tests/test_boot_admission.sh`: priority, cap, stale reclaim, wait cap, marker lifecycle, plugin-once all green.
- [ ] Harness scenario green, and the harness's macOS count unchanged by name from main.
- [ ] Live after PR B: a planned reboot brings every bridge up without a bounce, measured by the bridge tally and zero `bridge_down` alerts in the first fifteen minutes.

Linear: neither.
