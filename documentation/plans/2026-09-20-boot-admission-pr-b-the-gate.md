---
title: PR B — the admission gate extends the boot lock, and one boot-progress marker
type: plan
status: active
owner: chrisrogers37
created: 2026-09-20
updated: 2026-09-21
epic: documentation/plans/2026-09-20-boot-admission-plan.md
spec: documentation/plans/2026-09-20-boot-admission-and-supervision-consolidation-design.md
issue: "#1573"
---

# PR B — the admission gate extends the boot lock, and one boot-progress marker

## Summary

`lib/start-bot.sh` already carries a host-wide bring-up serializer — the #304 boot-mass mitigation at `:14-47`, a fleet-wide `mkdir` lock with a fixed 8 s hold and no ordering. PR B **replaces that lock in place** with the same idea done properly: N slots instead of one, managers first instead of arrival order, a real wait cap instead of 120 s, and a reaper instead of an age-based force-claim. A `data/.boot-queued` marker carrying the launcher's pid becomes the boot-progress signal `service_is_starting` reads first on both OSes, wired into **every** consumer of that door, so neither keepalive nor fleet-pulse can mistake a queued bot for a dead one. The proof is two planned reboots: one on PR A alone (the control), one on A+B, compared on the gate's own grant distribution rather than on an aggregate that queueing would satisfy for the wrong reason.

Re-forged after ironclad cycle 1 (12 blockers / 31 risks / 10 gaps / 7 questions / 16 observations). The two lowest-ROI units of the previous draft — the adapter migration and the stagger retirement — are a decision fork (F12) and a lock (F11) rather than tasks; see `## Deferred`.

## Evidence

Re-taken at **`97cdae4`** (`boot/pr-b-the-gate`, = `main` after PR A #1684 merged). The previous block was anchored at `1053659`, pre-PR-A, and 14 of its citations had drifted; every line below was re-verified by grep at this tip.

### The mechanism this PR extends

- `lib/start-bot.sh:14-47` — **the #304 boot-mass mitigation** (`bc0990f`, merged `efa2092`). A fleet-wide `mkdir` lock at `${TMPDIR:-/tmp}/.claudlobby-fleet-boot.lock`, `BOOT_LOCK_HOLD_S=8`, a 60 s age-based stale force-claim, a 120 s wait cap that proceeds without the lock, and a detached `( sleep 8; rmdir ) & disown` release. It is **platform-neutral and runs on launchd today.** It sits above the seeding block (`:108`, `:116`), above the session teardown (`:135`) and above the plugin/marketplace block (`:225-276`).
- `lib/start-bot.sh:31-35` — its staleness test compares `date +%s` against a `stat` mtime, so a forward clock step force-claims a live lock and a backward step makes the age negative and the lock is never cleared.
- `lib/validate-bot-change.sh:1416, 1523, 1565, 1625, 1703, 1836` — the harness neuters that lock at six sites with `BOOT_LOCK_HOLD_S=0`, which is why no existing scenario can observe it.

### The insertion surface

- `lib/start-bot.sh:4` `set -euo pipefail`; `:12` `load_bot_conf "$BOT_DIR"`; `:11` `install_error_trap "$BOT_DIR"`.
- `lib/start-bot.sh:233-234` `LOG=` + `setup_log_dir` assigned **inside** `if command -v "$CLAUDE" … && [ -n "${FLEET_PLUGINS_REQUIRED:-}" ]`, and again unconditionally only at `:292-293` — so a bot with no required plugins has no `LOG` before `:292`.
- `lib/start-bot.sh:221` `CLAUDE_CMD`; `:278` `bot_tmux "$TMUX_SOCKET" new-session …` (not a bare `tmux`); `:284` `touch "$BOT_DIR/data/.spawn"`; `:485` and `:506` the two `pane_send_verified` calls after the readiness poll.
- `lib/lib-common.sh:466` `trap '_lc_cleanup' EXIT`, set **at source time**, with the convention stated at `:463-465`: "Source lib-common.sh before setting your own EXIT trap — this overwrites any existing trap … set it after sourcing and call `_lc_cleanup` explicitly." `install_error_trap` at `:4969` installs an **ERR** trap, not an EXIT one.

### The doors

- `lib/lib-common.sh:4216` `service_is_starting`, `:4217` `local svc="${1:?Usage: service_is_starting <bot_service>}"`, `:4218` `[ "$_OS" = "Linux" ] || return 1`. Header invariant at `:4187`: "One predicate, two consumers, so detection and healing can never disagree about whether a bot is booting."
- `lib/lib-common.sh:4170-4182` `_BOOT_GRACE_S_DEFAULT=300`, whose comment states its budget: "budgeted against ONE PHASE … the ExecStart phase is bounded by start-bot.sh's own `RC_READY_TIMEOUT_S` (90s default), so 300s is >3x headroom", and its purpose: "without it a wedged spawner would suppress both the restart and the alarm forever … the manufactured all-clear shape (#933)". Read at `:4264` through the `KEEPALIVE_BOOT_GRACE_S` env override.
- `lib/lib-common.sh:3031` `marker_age_within <marker> <max_age_s>`; `:347` `with_lock`; `:3887` `resolve_boot_epoch`; `:1814-1821` `emit_fleet_event <type> <source> <data_json> [bot_dir] [bot_id]`.
- `lib/lib-common.sh:1259-1302` `wait_bridge_ready_state` — PR A's clock-step fold: `_step_s=60` raised to `timeout_s` at `:1271`, and `:1296` `if [ "$delta" -lt 0 ] || [ "$delta" -gt "$_step_s" ]` shifts the origin instead of counting.
- `lib/keepalive.sh:88-99` — the shipped pid-reuse remedy (`59457c0`, #1425): `kill -0 "$prev"` **paired with** `marker_age_within "$pidf" …`, because "`kill -0` alone let a RE-USED pid block a bot indefinitely — proven live within minutes of deploy".
- `lib/keepalive.sh:184-187` — "the touch is what SPACES heal retries. Keep it true for any new restart branch"; `:188-211` `restart_bot_service`; `:318-324` the dead-session branch, guarded at `:324` by `[ -n "${BOT_SERVICE:-}" ] && service_is_starting "$BOT_SERVICE"`.
- `lib/supervisor.sh:189-226` `svc_kick` — propagates the restart command's own status and **reserves rc 2** for "no branch applies"; `:235-245` `svc_enroll` dispatching onto the two installer scripts.
- `lib/fleet-pulse.sh:306-308` (`$bot_dir` in scope, `_svc_starting` computed once for both checks), `:315` `session_missing`, `:333` `service_down`, `:347` the bridge check gated on a live session, `:359` `bridge_down`, `:575` `_CRITICAL_ESCALATION_TYPES`, `:813-824` the summary leg (`$_s_bot_dir` in scope).
- `lib/validate-bot-change.sh:2428` `BP_DIR`, `:2469` `bp_starting() { service_is_starting "$BP_SVC"; }`, `:2419-2452` the #1002 boot-window scenario with `ExecStartPre=/bin/sleep 4`.
- `lib/rolling-restart.sh:86-104` `rr_bot_ceiling` = `RC_READY_TIMEOUT_S + 120`, the margin enumerated as "pre-stop-handoff, spin-up, the tmux session spawn, and the poller's own settle"; `lib/weekly-worker-restart.sh:88-97` carries the same derivation.
- `lib/keepalive-all.sh:73-97` a serial `for` loop over every bot; `lib/spin-up-bot.sh:35-70` a three-way ladder (kick / migrate / enroll), not an enroll-only script.
- `claudlobby/boot.py:72-80` `derive_slots` = `clamp((cpu or 1) // 4, 1, 4)`, run in the composer; `:162-176` the `auto` / explicit resolution and the legal `admission_wait_max_s: 0`.
- `560c3c9` "order the host ladder managers-first, then workers (#1002)" — the intent the ticket priority preserves.

### Ratchet floors, measured

`CALL_PATTERN` (`tests/test_supervisor_ratchet.py:96`) counts every line containing the binary as a bare word, **comments and help strings included** — its own docstring says so. Measured at this tip, the non-call lines are `lib/keepalive.sh:290, 299, 300`, `lib/spin-down-bot.sh:42` (the #828 standing-posture warning CLAUDE.md itself quotes), `lib/install-bot.sh:10`, `lib/install-bot-systemd.sh:93`. So the post-migration floors are **keepalive 3, spin-up 0, spin-down 1, install-bot 1, install-bot-systemd 1** — only `spin-up-bot.sh` can leave the allowlist, and `test_allowlist_has_no_zero_count_entries` (`:190-199`) forbids a `0` entry.

### Rehearsals

Every operational block below was run for real before it was written down — `/bin/bash` 3.2.57(1) on Darwin, against faked state in a temp dir. One line per block; the two that did not behave were rewritten, not described.

| # | block | result |
|---|---|---|
| R0 | `mv` as a slot mutex | **REJECTED** — `mv src dst` where `dst` is an existing directory *moves src inside it* (`dst/src` appeared) rather than failing. `mv` is not a mutex; `mkdir` is. The block was rewritten around `mkdir` before it reached this doc. |
| R1 | ticket name + sort rule | PASS — a priority-0 manager ticket minted *after* a priority-1 worker ticket still sorts first under `LC_ALL=C sort`. `date +%s%N` yields 19 digits on this host. |
| R2 | `%N`-less `date` guard | PASS — a stub `date` printing `1789998593N` is rejected by the all-digits/width guard, and the fallback `printf '%010d%09d' "$(date +%s)" 0` returns a 19-digit stamp that sorts **with** real ns stamps rather than before every one of them. |
| R3 | slot take | PASS — 8 concurrent `mkdir slots/0` racers, exactly 1 winner. |
| R4 | pid-less slot | PASS — a slot whose `mkdir` landed but whose `pid` file has not been written yet is **HELD** inside a 10 s claim grace and reapable only past it. The unreachable answer does not license a delete (`claudlobby/source_state.py`'s rule, #1146's direction). |
| R5 | slot reclaim | PASS — 6 concurrent reclaimers doing `mv slots/0 slots/.reap.$$.<uniq>`, exactly 1 winner; the losers' source is already gone. |
| R6a | dead holder | PASS — a recorded pid that is dead fails `kill -0` and the slot is reclaimable. |
| R6b | **reused** pid | PASS — a live `sleep` whose pid sits in a back-dated slot holds the *fresh* slot and **not** the back-dated one, because liveness is `kill -0` **paired** with `marker_age_within <slot>/pid <hold_ceiling_s>`. `ps -o lstart=` was the first form tried and is **not** used: it is unmeasurable under this sandbox, and the paired form is the remedy this estate already shipped for #1425. |
| R6c | marker rung | PASS — a marker naming a **live** launcher reads mid-boot; the instant that launcher is killed the same fresh marker stops suppressing. The 1400 s window is a statement about a live launcher, never about a file's mtime — #933's manufactured-all-clear bound, kept. |
| R6d | grace derivation | PASS — `boot_grace_s = admission_wait_max_s + ready_timeout_s = 1400`, derived from the two values it must agree with. |
| R7 | marker reaper | PASS — a marker whose recorded launcher pid is dead is removed by the reaper, so a SIGKILLed launcher stops suppressing within one poll rather than for the whole window. |
| R7b | one marker lifetime | PASS — acquire → **release**: a granted-but-not-ready bot (the whole plugin/marketplace block and the tmux spawn) still reads mid-boot, and the marker is gone only after the readiness poll ends. No gap for keepalive's dead-session branch to fire into. |
| R8 | wait-cap clock fold | PASS — PR A's fold verbatim: with a +3600 s tick and a −4000 s tick injected, 8 s is counted in both the clean run and the stepped run. A clock jump cannot expire every waiter's cap in the same instant. |
| R9 | cap dispersion | PASS — effective cap `= cap + arrival_rank × dispersion`; run at `dispersion = 20`, ranks 0/1/17 gave **1200 / 1220 / 1540 s**. At the prescribed `dispersion = ready_timeout_s / slots` the spread is wider still (1 slot, 200 s: ranks 0/1/20 → 1200 / 1400 / 5200 s, computed alongside the drain table). Either way the over-cap escape degrades one bot at a time instead of releasing the queue in one instant. |
| R10 | timeout removes its ticket | PASS — after the head waiter's timeout deletes its own ticket, the next waiter is head of queue immediately. |
| R11 | **the EXIT-trap dispute** | PASS, and it settles the lens disagreement: a bare `trap … EXIT` set *after* sourcing lib-common **replaces** the source-time `_lc_cleanup` trap and leaks its `mktemp -d` (the directory survived the exit); naming `_lc_cleanup` in the composed trap cleans it up. adversarial-review is right; engineering-review's and plan-health-audit's "cleared" verdicts are wrong. |
| R12 | ERR ≠ EXIT | PASS — `set -E` + an ERR trap and an EXIT trap both fire and occupy independent slots, so `install_error_trap` is not what a `trap … EXIT` composes with. |
| R13 | `svc_kick` rc branch | PASS — `rc=0; svc_kick \|\| rc=$?; [ "$rc" -eq 2 ]` fires on rc 2 only (1 of 2 cases); `\|\|` fires on both, i.e. it also fires on a *failed* restart. |
| R14 | epoch keying | PASS — `state/boot/<boot-epoch>/{tickets,slots}`: a prior-epoch tree is classified stale with no pid read at all. |
| R15 | `set -u` log abort | PASS — a conditionally-assigned `LOG` read under `set -euo pipefail` aborts (rc 1), so the gate must own `LOG`/`setup_log_dir` above its own first log line. |
| R16 | empty `BOT_SERVICE` | PASS — `${1:?}` aborts the caller on an empty first argument (nine harness `bot.conf`s and every pre-generate fleet have `BOT_SERVICE=""`); `${1:-}` does not. |
| R17 | verdict on stdout only | PASS — the log line goes to `$LOG` and only `granted:0` reaches the caller's command substitution. |

### Drain arithmetic, computed before Task 1 rather than after the reboot

`drain = ceil(bots_on_host / slots) × hold_ceiling`. `hold_ceiling` is the *whole* hold, which under F10's placement spans seeding, plugins, the session spawn and the readiness poll.

| host class | cpu | bots | `derive_slots` | hold 200 s | hold 58 s (measured single-bot bring-up on a calm host) |
|---|---|---|---|---|---|
| four-core Linux host | 4 | 21 | **1** | 4200 s — **exceeds** the 1200 s cap | 1218 s — **exceeds** |
| launchd host | 8 | 18 | 2 | 1800 s — **exceeds** | 522 s — fits |
| launchd host | 12 | 18 | 3 | 1200 s — equals the cap exactly | 348 s — fits |

Tail-bot dark time at 1 slot / 21 bots: **19.3 min** at the optimistic hold, **66.7 min** at the ceiling. That cost is stated here because the operator who reads "every bridge up without a bounce" will not otherwise expect it; the trade (a dark bot that comes up beats one that never does) is still the right one, but it is a trade.

## Implementation Plan

### Dependencies

PR A merged **and deployed** (#1684, `3f26afe`): the `BOOT_*` keys in `bot.conf`, `lib/supervisor.sh`, `wait_bridge_ready_state`, `plugin_ensure`, the ratchet.

### Blocks

PR C (`## Deferred`): the adapter migration of the five boot-path scripts if F12 lands that way, the stagger retirement and everything that reads the rung, and the removal of PR B's temporary arming carrier if F17 lands that way.

### Steps

### Task 0: the before-leg, and the PR-A-only control reboot

The previous draft had no Task 0 and first mentioned the two-leg gate in its deploy task, by which point the tree is dirty and a baseline can no longer be taken. PR A had one; PR B needs it more, because PR B *adds* harness checks, so the harness count must move and nothing recorded what it should move to.

- [ ] **Step 1 (before-leg):** in a spare worktree at `origin/main` with its own venv, run `./.venv/bin/pytest --tb=no -ra > /tmp/run_before.txt 2>&1; echo $?` and record **rc**, the scoped `FAILED`/`ERROR` names from inside the summary block, and the count line — all three, per CLAUDE.md's recipe. Unsandboxed. Then `bash lib/validate-bot-change.sh` directly and record its pass/fail pair by name. Write both into the evidence dir.
- [ ] **Step 2 (the control reboot):** take a planned reboot of the primary host on **PR A alone, now**, before Task 1 is written. It costs one of the already-budgeted reboots, the installed units still carry the stagger and the #304 lock so it is a clean control, and either outcome is worth more than the same reboot taken last: if the tally is already healthy, the gate's marginal value has collapsed and F12's cut becomes obvious; if it is not, the gate is proven necessary against an A-carrying boot rather than against the 2026-09-19 incident. Record: the per-bot `BRIDGE_READY`/`TIMEOUT` instants from each `logs/startup.log`, the count ready at 5/10/15/25 min, every `session_missing` / `service_down` / `bridge_down` event in the window, and `lib/selfstart-snapshot.sh`'s page **run before any rescue** (#1002/#1043 — the snapshot is void once anyone intervenes).
- [ ] **Step 3:** print the 2026-09-19 figure ("7 of 18 after the first bring-up") as **NOT COMPARABLE** wherever it appears, for the reason `lib/selfstart-snapshot.sh` already gives about its own prior figure: it was produced by an incident rather than by this instrument, and it must be re-derived before anyone claims better, worse or flat. Step 2's tally is the comparable baseline; nothing else is.

### Task 1: the admission gate — replacing the #304 boot lock in place

**Files:** `lib/boot-admission.sh` (new, sourced by `lib-common.sh` right after `lib/supervisor.sh` — the path is chosen here, not left to the implementer), `lib/lib-common.sh` (the source line; `_BOOT_GRACE_S_DEFAULT`'s comment), `lib/start-bot.sh` (**delete `:14-47`**), `claudlobby/boot.py` + `claudlobby/system.yaml` + `system.yaml.example` (the two derived keys, F13/F15), `tests/test_boot_admission.sh` (new), `tests/test_boot_policy.py`, `tests/test_boot_policy_conformance.py`.

- [ ] **Step 1 — delete the predecessor in the same commit that lands the gate.** Remove `lib/start-bot.sh:14-47` entirely, comment block included. The gate takes its position (immediately after `load_bot_conf` at `:12`), its atomic primitive (`mkdir`) and its job, and does all three properly. Drop `BOOT_LOCK_HOLD_S=0` from every harness site that sets it (`lib/validate-bot-change.sh:1416, 1523, 1565, 1625, 1703, 1836`) — six sites, and the new scenario must not add a seventh. Add a grep assertion to `tests/test_boot_admission.sh` that `lib/start-bot.sh` contains **no** second serialization primitive (no `mkdir`-lock, no `BOOT_LOCK`, no `flock` outside the gate's own door), and put "the boot lock returns" on the mutant list.

- [ ] **Step 2 (tests first):** `tests/test_boot_admission.sh` under a temp `CLAUDLOBBY_ROOT`, auto-collected by `tests/test_sh_suites.py:33`. Cases, each written before its code:
  - **(a) priority, deterministically.** *Not* a race: pre-seed a priority-0 manager ticket into `tickets/` **before** the worker launcher starts, then start the worker with 1 slot and assert it waits. The previous draft's phrasing ("a manager ticket arrives before the worker is granted") depended on injecting inside a window the step never described; this one is checkable by someone who did not write the plan. A second cell asserts the granted order from the **grant log lines**, not from a sampled file.
  - **(b)** 2 slots, three waiters: exactly two hold slots at once, and the third's grant follows a release.
  - **(c)** a slot whose recorded pid is dead is reclaimed by the next waiter (R6a).
  - **(c2)** a slot whose `mkdir` landed but whose `pid` file does not exist yet is **not** reclaimed inside the claim grace, and is past it (R4).
  - **(c3)** two concurrent reclaimers, exactly one winner (R5).
  - **(c4) a REUSED pid:** spawn a real `sleep`, write its pid into a back-dated slot, assert the slot is reclaimed anyway (R6b). **On the mutant list**: drop the `marker_age_within` half and this cell must go red.
  - **(d)** a waiter past its effective cap returns `timeout`, **removes its own ticket first**, and a later waiter is granted immediately afterwards (R10).
  - **(d2)** a clock step mid-wait (stub `date` to jump +3600 s then −4000 s): the waiter neither gives up early nor jumps the queue (R8).
  - **(d3)** `waiters = 3 × (cap / hold)`: releases past the cap are **spread**, not simultaneous (R9).
  - **(e)** `boot_admission_release` removes the slot, the ticket and the marker; a trap-driven release works when the caller exits mid-hold; and `$_LC_TMPDIR` is **gone** after the caller exits (R11 — the mutant is "drop `_lc_cleanup` from the composed trap").
  - **(f)** the marker exists from acquire until **release**, and is present for a granted-but-not-ready bot (R7b).
  - **(f2)** the reaper removes a marker whose recorded launcher pid is dead (R7).
  - **(g)** an unwritable state dir yields `unavailable` without stalling; **(g2)** a prior-epoch `state/boot/<epoch>/` tree is stale unconditionally, with no pid read (R14); **(g3)** a `%N`-less `date` on PATH still produces a 19-digit sortable stamp (R2); **(g4)** the zero/one cases — `slots >= bots_on_host` grants on the first loop, and `admission_wait_max_s: 0` proceeds immediately and emits `boot_admission_timeout` exactly once.

- [ ] **Step 3 — the library.** `lib/boot-admission.sh`:

```bash
boot_admission_acquire <bot_dir>    # prints granted:<slot> | timeout | unavailable on STDOUT ONLY; rc 0 always
boot_admission_release <bot_dir>    # removes this bot's slot, its ticket and its marker; idempotent
_boot_admission_reap                # tickets, slots and markers whose holder is dead or past its hold ceiling
```

  - **State** lives under `$CLAUDLOBBY_ROOT/state/boot/<boot-epoch>/{tickets,slots}`, keyed on `resolve_boot_epoch` exactly as this directory's two existing tenants already are (`plugins-updated.<epoch>.<plugin>`, `lib/lib-common.sh:4071`; `runtime/_host/boot-capture/<epoch>`, `lib/boot-capture.sh:136-137`). A prior-epoch tree is stale **unconditionally** — never by reading a pid that a reboot has made meaningless. An unresolvable epoch falls back to the un-keyed path with a log line, as `plugin_ensure` does.
  - **Ticket name** `<priority: 1 digit>-<arrival: exactly 19 digits>-<unit-name>`. One format, not a disjunction: `date +%s%N`, validated all-digits and width 19, else `printf '%010d%09d' "$(date +%s)" 0` — which is the same quantity at second resolution and therefore sorts *with* ns stamps (R2). The unit name comes from `svc_unit_name` (`lib/supervisor.sh`), the one owner of that name; if it resolves empty the gate logs loudly and **proceeds ungated** rather than sharing a ticket path with another bot (R16 — nine harness `bot.conf`s carry `BOT_SERVICE=""`).
  - **Take** is `mkdir "slots/<n>"` — the atomic mutex (R3) — then `printf '%s\n' "$$" > "slots/<n>/pid"`. A slot with no `pid` file younger than `BOOT_ADMISSION_CLAIM_GRACE_S` (10 s, a compose-time constant, not an operator key) is **HELD**, never reapable (R4).
  - **Reclaim** is `mv "slots/<n>" "slots/.reap.$$.<epoch-ns>"` then `rm -rf` — one atomic rename to a unique non-existent name, exactly one winner (R5). Never `rmdir` + `mkdir`, and never `mv` onto an existing directory (R0).
  - **Liveness** is `kill -0 <pid>` **paired with** `marker_age_within "slots/<n>/pid" "$BOOT_HOLD_CEILING_S"` — the remedy `lib/keepalive.sh:88-99` already ships for #1425 (R6b). `kill -0` alone is not sufficient and this plan says so where the code will say it. A slot past the hold ceiling is reaped regardless of pid state.
  - **The wait loop** polls every `BOOT_ADMISSION_POLL_S` (default 2; overridable so the harness can run at sub-second quanta — a `lib/`-internal call convention, never an operator key) and carries **PR A's clock fold verbatim** (`lib/lib-common.sh:1268-1274`, `:1293-1298`): `_step_s = max(cap, 60)`, and a tick whose delta is negative or exceeds `_step_s` shifts the origin instead of counting (R8). The same fold governs the ticket's arrival stamp and the marker-freshness read.
  - **The cap** is `BOOT_ADMISSION_WAIT_MAX_S` (F13) **dispersed per ticket**: effective cap `= cap + arrival_rank × (ready_timeout_s / slots)`, `arrival_rank` computed once at acquire as the number of tickets sorting before ours (R9). Every ticket at a cold boot is minted within the same second, so a flat cap expires them all in the same second and reconstitutes the storm it replaces; dispersion degrades one bot at a time.
  - **The timeout path removes its own ticket before returning** (R10), then logs `ADMISSION_TIMEOUT`, emits `boot_admission_timeout` and proceeds without a slot.
  - **`_boot_admission_reap` does not touch plugin stamps.** The previous draft made the 2 s poll the unlocked GC for PR A's `plugins-updated.<epoch>.*` stamps, which `plugin_ensure` writes under `with_lock` (`lib/lib-common.sh:4037`, `:4072`) — a delete landing just after a write re-arms `claude plugin update` for every later bot, the amplifier PR A removed. Stamps are epoch-keyed and tiny; the `data-sweep` precedent applies and they stay with their writer.
  - **An un-regenerated `bot.conf` has none of these keys at all**, which is a third state beside empty, non-numeric and zero — and the one "no operator step" actually depends on. Every `BOOT_*` read is `${KEY:-<default>}` with the composed default, so a fleet that has not regenerated degrades silently to a one-slot gate with today's timings rather than aborting under `set -u`. Cell: a `bot.conf` carrying **no** `BOOT_*` key starts, grants, and logs.
  - **The marker carries the boot epoch beside the pid**, because `.boot-queued` is written by *every* `start-bot.sh` run, not only a boot one — the `.spawn` lesson `lib/boot-capture.sh:19-25` records verbatim: "after a restart it is not missing, it is a plausible timestamp describing a DIFFERENT event, so a later reader gets a confident wrong answer rather than an absent one." The epoch is what lets a later reader tell a boot-queue from a restart-queue; PR C's rung re-base (D4) depends on it existing from PR B onward, because no boot before it lands can be backfilled.
  - **Log destination.** The gate hoists `LOG="$BOT_DIR/logs/startup.log"` + `setup_log_dir "$LOG"` itself, above its own first line (R15: `LOG` is conditionally assigned at `:233` and would be unbound under `set -u`). `start-bot.sh`'s later assignment at `:292-293` becomes idempotent. Every gate line goes to `$LOG`; **only** the verdict goes to stdout, because the caller captures it (R17). Lines exactly as spec §6.2 names them: `ADMISSION_WAIT queue=<n> slots=<held>/<max> priority=<p>`, `ADMISSION_GRANTED slot=<n> after <s>s`, `ADMISSION_RELEASED`. **`ADMISSION_RELEASED`, not `RELEASED`** — one token, used in the step, in Task 5 and in the checklist.
  - **Events.** `emit_fleet_event "boot_admission_timeout" "start-bot" "$_json" "$BOT_DIR" "$BOT_ID"` where `_json` carries `{"queue":<n>,"slots_held":<h>,"slots_max":<m>,"waited_s":<s>,"priority":<p>,"rank":<r>}`; `boot_admission_unavailable` with `{"reason":"<state dir unwritable|epoch unresolvable|unit name empty>","dir":"<path>"}`. Both are `source="start-bot"`. Register both in `claudlobby/known_values.py`'s event-type registry in the same commit, or `claudlobby brief` cannot return them (#903).
  - **Recording the normal path.** `ADMISSION_GRANTED … after <s>s` already carries the wait; emit it as a `metric_sample` (`boot.admission_wait_s`, `boot.admission_hold_s`, subject = the bot's instance alias) so the slot formula, the cap and the hold ceiling can be revised **from the reboot that was supposed to validate them**. Dormant by the plane's standing arming, non-blocking, exit 0 on every path. Without this the proof leaves nothing on disk and the constants stay unfalsifiable.

- [ ] **Step 4 — the two derived keys.** `claudlobby/boot.py` gains, alongside the ratified six:
  - `hold_ceiling_s` — derived `ready_timeout_s + 120`, rendered `BOOT_HOLD_CEILING_S`. The `+ 120` is **not a new guess**: it is the margin `lib/rolling-restart.sh:86-104` already derives and enumerates ("pre-stop-handoff, spin-up, the tmux session spawn, and the poller's own settle"), so the gate's hold ceiling and the restart drivers' per-bot budget are the same number by construction rather than two derivations of one boot-timing truth.
  - `boot_grace_s` — derived `admission_wait_max_s + ready_timeout_s`, rendered `BOOT_GRACE_S` (F15). `_BOOT_GRACE_S_DEFAULT = 300` and the `KEEPALIVE_BOOT_GRACE_S` env override become the **fallback for an un-regenerated `bot.conf`**, exactly the shape F4 locks for `RC_READY_TIMEOUT_S`. `lib/lib-common.sh:4170-4182`'s comment is rewritten **in this commit** against the new phase bound: PR B moves the host-wide wait *into* `ExecStart`, so the sentence "the ExecStart phase is bounded by start-bot.sh's own `RC_READY_TIMEOUT_S` (90s default), so 300s is >3x headroom" is no longer true and must not survive as the next reader's model. The manufactured-all-clear bound (#933) is **kept, not widened**: it is now carried by liveness rather than by time — the marker names its launcher's pid, and a dead launcher stops suppressing within one poll (R6c, R7), which is a *tighter* bound than 300 s of mtime for the SIGKILL case the old comment could not cover at all.
  - Conformance: both keys appear exactly once in `bot.conf`, and neither rendered unit carries them.

- [ ] **Step 5:** verify the suite through `test_sh_suites.py`; `tests/test_bash_parse.py` covers the new file by construction. Commit: `feat(boot): the admission gate replaces the #304 boot lock — one priority queue, host-derived slots, one reaper`.

### Task 2: the marker, the wiring, and every consumer of the door that changed

**Files:** `lib/start-bot.sh`, `lib/lib-common.sh` (`service_is_starting`), `lib/keepalive.sh`, `lib/fleet-pulse.sh`, `lib/validate-bot-change.sh`, `tests/test_service_is_starting.py` (**extended — it already exists**), `tests/test_door_consumers.py` (new), `tests/test_boot_admission.sh`.

- [ ] **Step 1 — the call site.** In `lib/start-bot.sh`, immediately after `load_bot_conf "$BOT_DIR"` (`:12`) and where `:14-47` used to be (F10):

```bash
LOG="$BOT_DIR/logs/startup.log"; setup_log_dir "$LOG"
_adm="$(boot_admission_acquire "$BOT_DIR")"
trap 'boot_admission_release "$BOT_DIR"; _lc_cleanup' EXIT
```

  The trap names **`_lc_cleanup`**, not `install_error_trap`. The previous draft's parenthetical pointed at the wrong trap: `install_error_trap` installs an **ERR** trap (`lib/lib-common.sh:4969`, R12), while the EXIT trap a bare `trap … EXIT` overwrites is the source-time `trap '_lc_cleanup' EXIT` at `:466`, whose own comment at `:463-465` states the rule. Measured (R11): the bare form leaks one `mktemp -d` per bot start, on every bot, on every host, forever. The mutant is "drop `_lc_cleanup` from the composed trap"; the cell is "`$_LC_TMPDIR` is gone after `start-bot.sh` exits". Both `exit` sites (`:131`, `:381`) are covered by an EXIT trap, and a `( … ) &` subshell does not run the parent's EXIT trap (measured on the same shell), so nothing detached can fire an early release.

  Release happens **after the readiness poll ends** (READY or TIMEOUT) and **before** the two `pane_send_verified` calls at `:485` / `:506`, deliberately: the TUI-draw wait is 10–19 s and longer under load, it contends for no MCP capacity, and holding a slot across it would inflate the hold ceiling for no benefit. Stated here because the previous draft left it as an unanswered question.

- [ ] **Step 2 — one marker, one lifetime.** `data/.boot-queued` is written by `boot_admission_acquire` carrying **the launcher's pid and the boot epoch**, and removed by `boot_admission_release` — acquire → **release**, nowhere else. The previous draft had it removed at *grant* while sizing its freshness window at `cap + ready_timeout`, which is dead arithmetic and, on launchd (`service_is_starting` returns 1 unconditionally at `:4218`), leaves a bot with no marker, no session and no boot signal across the entire plugin/marketplace block at `:225-276` — a span containing a `with_timeout 60` per unregistered marketplace — straight into keepalive's dead-session branch (`lib/keepalive.sh:302-331`). That is the #1002 livelock moved, not closed. One lifetime, written identically here and in spec §6.2 step 4.

- [ ] **Step 3 — `service_is_starting <service> [bot_dir]` (F8).**
  - `local svc="${1:-}"` — **not** `${1:?}`, which aborts a non-interactive caller on an empty first argument (R16).
  - **Rung 1, both OSes, first:** when `bot_dir` is given and `data/.boot-queued` exists, the marker names a **live** pid (`kill -0`) **and** is younger than `BOOT_GRACE_S` from that bot's `bot.conf` → return 0. Liveness is what makes the 1400 s window honest (R6c); a dead launcher's fresh marker suppresses nothing.
  - **Rung 2, Linux only, unchanged:** the existing SubState read.
  - Else 1.
  - The `activating/*` arm and the three comment copies explaining it (`lib/lib-common.sh:4191`, `:4247`, `:4256-4270`; `claudlobby/supervision.py:141-145`) are **untouched in PR B** — their producer, the composed `ExecStartPre`, still exists because F11 moves the stagger retirement to PR C. The comment that *does* change is `_BOOT_GRACE_S_DEFAULT`'s (Task 1 Step 4), because that one is falsified by PR B itself.

- [ ] **Step 4 — the door consumer table, and a test that keeps it honest.** Every unlisted consumer of a changed door is a defect of the *plan*, not of a call site. `service_is_starting`'s consumers, grep-derived at `97cdae4` (`grep -rn 'service_is_starting' lib/ tests/ claudlobby/`, excluding its own definition and prose):

| consumer | line | bot dir in scope | change |
|---|---|---|---|
| `lib/keepalive.sh` | `:324` | `$BOT_DIR` | pass it; **delete** the `[ -n "${BOT_SERVICE:-}" ] &&` short-circuit so the marker rung runs regardless of service resolution |
| `lib/fleet-pulse.sh` | `:307` | `$bot_dir` | pass it; delete the `[ -n "$BOT_SERVICE" ] &&` short-circuit |
| `lib/fleet-pulse.sh` | `:824` | `$_s_bot_dir` | pass it; delete the `[ -n "$_s_svc" ] &&` short-circuit |
| `lib/validate-bot-change.sh` | `:2469` | `$BP_DIR` | pass it (`bp_starting() { service_is_starting "$BP_SVC" "$BP_DIR"; }`) |

  `lib/fleet-pulse.sh` appeared in **no** task of the previous draft, and because the second argument is optional nothing would have broken loudly: fleet-pulse would simply never gain the marker rung, and on launchd `:4218` still returns 1 — so a bot with no session for its whole queue wait (up to 1200 s **by construction**) trips `_svc_starting=0` and emits `session_missing` **and** `service_down` with a debounced operator page, once per queued bot per tick (`:306-321`, `:325-338`; both in `_CRITICAL_ESCALATION_TYPES` at `:575`). That is the alert storm the epic exists to close, re-emitted from the door the plan did not touch — and `8d92885` (#1002) fixed exactly this defect once already, its commit body recording "a `service_down` whose own payload said `state=activating` — self-proving false".

  `tests/test_door_consumers.py` (new) checks in that table and **fails when a new consumer appears**: it re-derives the call-site set by the same grep and compares against a checked-in map, naming the file and line of anything unlisted. Same shape as `tests/test_supervisor_ratchet.py`, same reason — the boundary is greppable, so a test can own it. PR C adds `boot_rung_for`'s row (recorded in `## Deferred`) to the same map rather than repeating the exercise.

- [ ] **Step 5 — extend the suite that already exists.** `tests/test_service_is_starting.py` (#1002) is the suite for this function — classes `TestStatesThatMeanMidStart` (`:100`), `TestStatesThatDoNotMeanMidStart` (`:114`), `TestGraceCap` (`:137`), `TestFailureModes` (`:233`); it already stages `lib-common.sh` + `lib/supervisor.sh` and forces `_OS`. The previous draft proposed a **new bash suite** beside it, which would have left the Python one pinning the pre-PR-B contract — passing against the branch the marker rung bypasses. Extend it instead, and say which assertions move:
  - unchanged: every `activating/*` and `active/exited` case (their producer still exists under F11) and the `TestGraceCap` cases, now parameterized on `BOOT_GRACE_S` with `KEEPALIVE_BOOT_GRACE_S` as the un-regenerated fallback;
  - new: marker fresh **and** live pid → 0 on `_OS=Darwin` and `_OS=Linux`; marker fresh but **pid dead** → falls through; marker **stale** → falls through (1 on Darwin, the stubbed SubState on Linux) — a **stale-marker cell in both directions**; **no** marker → byte-identical to today; a marker-fresh bot with an **empty** `BOT_SERVICE` returns 0; a **granted-but-not-ready** bot returns 0 (not only a queued one).
  - a bash cell in `tests/test_boot_admission.sh` covers only what the Python harness cannot reach (a real concurrent acquire).

- [ ] **Step 6 — the harness scenario (F7), with a controlled hold and a positive control.** A new block in `lib/validate-bot-change.sh`. F7's locked choice stands; its stated rationale does not survive review unamended, and the amendment is here: as specified, the scenario **cannot observe the property**. The harness's throwaway bots have no channel (`BOT_SERVICE=""`, no token, `:1399-1408`), so `bridge_state` answers `no_handle` and `wait_bridge_ready_state` returns on its **first** poll (`lib/lib-common.sh:1284-1292`) — the first bot's hold is sub-second and the shape that passes ("both granted immediately") is indistinguishable from a gate that does nothing.
  - **Controlled hold:** stub `bridge_state` to stay `no_bridge` for a scenario-chosen number of seconds, with `RC_READY_TIMEOUT_S` short, so the first bot's window is *seconds* and deterministic.
  - **Positive control:** the same scenario at `BOOT_ADMISSION_SLOTS=2` must show **both granted at once**. Without it, "contended for the wrong reason" is indistinguishable from "works".
  - **Ordering is asserted from the grant log lines**, never from a sampled file.
  - **Pin the block's own `CLAUDLOBBY_ROOT`** — a single shared root across both bots (as `:1416` already does for its own block), or `state/boot/` is per-bot, they never contend, and the scenario passes having tested nothing.
  - **Write the stub list into the step:** `CLAUDE_BIN` as an `exec cat` stub (`:1393-1397`), a pinned `HOME` with a seeded `settings.json`, `TMPDIR`, one shared `CLAUDLOBBY_ROOT`, a stubbed `bridge_state`, `BOOT_ADMISSION_POLL_S` sub-second. **And the cannot-prove list:** MCP startup contention (the stub spawns no servers, so the one resource the gate rations is absent from the test), supervisor behaviour, and that `MCP_TIMEOUT` reaches a live session. Those are for the reboot, and the plan says so rather than letting a green harness imply them.
  - **Cells:** the second bot holds `data/.boot-queued` while the first is inside its readiness window; one keepalive tick against the second logs a SKIP and issues **no** restart; **one fleet-pulse tick against the second emits neither `session_missing` nor `service_down`** under `_OS=Darwin` (the blocker's own missing cell — the two consumers must agree about a queued bot); after the first's poll ends the second is granted and its marker is gone; **a serial sweep with one queued bot completes inside a stated bound** (`lib/keepalive-all.sh:73-97` runs bots one at a time and keepalive's fallback invokes `start-bot.sh` synchronously at `:209`, so without a scoped cap one queued bot stalls every other bot's watchdog on a 60 s timer — the sweep-abort class). Reap both bots.

- [ ] **Step 7 — the scoped cap for serial callers.** A caller that brings bots up **one at a time is already the bound** and must not also pay the queue's cap. `boot_admission_acquire` honours `BOOT_ADMISSION_CALLER_CAP_S` when set — a `lib/`-internal call convention, never composed, never documented as an operator knob, and deliberately **not** `BOOT_ADMISSION_WAIT_MAX_S` (F4 locks the composed value as the winner over the env for that key). Set it in `lib/keepalive-all.sh`, `lib/rolling-restart.sh`, `lib/weekly-worker-restart.sh` and `lib/spin-up-bot.sh` to the per-bot slack those drivers already derive, so `rr_bot_ceiling`'s `RC_READY_TIMEOUT_S + 120` stays true by construction and a roll can never hard-stop on a bot that is merely queued. This is the mechanism fix; widening two drivers' ceilings would be the instance fix and is explicitly not taken.

- [ ] **Step 8:** verify the suites, then `bash lib/validate-bot-change.sh` **directly** (the standing memory: the macOS harness test fails in both legs of a names diff, so a new failure inside it cancels out — run the harness itself after any change to a harness-exercised script). Commit: `feat(start-bot): every bring-up passes the admission gate, and a queued bot is mid-boot to every consumer`.

### Task 3: the boot path on the adapter — **conditional on F12**

Ship only if F12 lands as (a) *in PR B*. The default and the lean is (b) *PR C*; see `## Deferred` for the full unit with the review's corrections already folded in, so it can be lifted into PR C verbatim.

### Task 4: docs, CHANGELOG

- [ ] `CLAUDE.md`: the runtime-model paragraph names the admission gate and the marker and **says the #304 lock is gone**; a `boot-admission.sh` row in the `lib/` table; the `start-bot.sh`, `keepalive.sh`, `fleet-pulse.sh` rows updated in one sentence each.
- [ ] `documentation/environment-variables.md`: the two new derived keys (`BOOT_HOLD_CEILING_S`, `BOOT_GRACE_S`) as **composed, not operator**; `KEEPALIVE_BOOT_GRACE_S` re-documented as the un-regenerated fallback rather than the source of truth (its current entry at `:98` states a budget PR B falsifies); the two markers; the two events; and `BOOT_ADMISSION_CALLER_CAP_S` / `BOOT_ADMISSION_POLL_S` named explicitly as `lib/`-internal call conventions that no operator sets — a knob with no right-moment surface is a silent switch, and naming them as not-knobs is the surface.
- [ ] `documentation/fleet-update-lifecycle.md`: one paragraph — a bring-up now queues, and the carrier for this change is the **pull**, not the restart (`lib/start-bot.sh` is read on demand per bring-up, so the first keepalive bounce after the pull runs the new gate).
- [ ] `documentation/runbooks/audit-cold-start-timing.md`: its `:164-168` recommendation of `ExecStartPre=/bin/sleep $((RANDOM % 5))` prescribes a mechanism this program is retiring; add a note pointing at the gate. Its measured baseline (8 bots, 36 s wall clock, fully parallel on a four-core host, `:45-60`) is the pre-existing instrument for the serialization cost and is named in Task 5.
- [ ] `CHANGELOG.md`: one bullet per task. Commit: `docs: the admission gate, the marker and the retired boot lock`.

### Task 5: gauntlet, deploy, and the attributable reboot proof

- [ ] **Mutants** (against committed code, never an uncommitted fix pass): priority ignored; cap ignored (a third holder); a pid-less slot reaped inside the claim grace; `kill -0` unpaired (the reused-pid cell must go red); reclaim back to `rmdir` + `mkdir`; the clock fold dropped; the cap un-dispersed; the timeout leaving its ticket; the marker removed at grant; `_lc_cleanup` dropped from the composed trap; `service_is_starting` ignoring the marker; a fleet-pulse site left un-passed; the #304 boot lock returned.
- [ ] **The two-leg gate** against Task 0's before-leg — rc, the scoped names, **and** the count line, all three. The harness count **will** move because PR B adds checks; record the expected new pass/fail pair by name in the checklist before running it, and compare names, never only counts.
- [ ] **CI on Linux** (open the PR early: the macOS red baseline hides harness regressions, and Linux CI is the leg that sees them).
- [ ] **Deploy — to the primary host only, never to the Pi.** Name the carrier honestly: `git pull` **is** the rollout for `lib/start-bot.sh`, because it is read on demand per bring-up; the manager restart is a *check*, not a gate. Pre-pull evidence: the harness scenario green, plus one hand-run `bash lib/start-bot.sh <bot_dir>` from a non-installed checkout against a real bot. Then pull, `generate` both fleets, restart one manager per fleet through the proven sequence, and read that manager's `logs/startup.log` for `ADMISSION_GRANTED slot=0 after 0s` (a calm host grants at once) and `ADMISSION_RELEASED`. Rollback: a revert pull, and — because the gate degrades to today's behaviour on every failure path — a bot already mid-wait finishes its wait rather than stranding. If F17 lands as an arming carrier, arm the throwaway fleet first, then one production fleet, then the rest.
- [ ] **The proof — attribution is intrinsic, not aggregate.** A planned reboot of the primary host, compared against **Task 0 Step 2's PR-A-only control**, not against the 2026-09-19 incident.
  - **Before the reboot**, compute and record the expected completion time from the host's *resolved* slot count and bot count using the drain table above, so a slow-but-correct boot is not read as a failure. At 1 slot and 21 bots this is ~19–67 min; at 3 slots and 18 bots, ~6–20 min.
  - **Window:** read at 5, 10, 15, **25 and 40 min** — past the wait cap. The previous draft's 15-minute window was *shorter than the cap*, so a cap-driven mass release fell outside it entirely.
  - **Primary criterion (the gate's own record):** the `ADMISSION_GRANTED slot=<n> after <s>s` distribution — waits nonzero, grants monotonic, **managers first**, per-bot wait / hold / time-to-ready; zero `boot_admission_timeout` and zero `boot_admission_unavailable`; **every** bot's startup log carries `ADMISSION_GRANTED`. These cannot be produced by PR A alone and cannot be satisfied by queueing, which is exactly what the previous criteria could be.
  - **Secondary:** the bridge tally. The command is written here rather than named: `for d in <bots_dir>/*/; do printf '%s\t%s\n' "$(basename "$d")" "$(bridge_state "$d" || true)"; done` — `bridge_state` is a per-bot-dir shell function (`lib/lib-common.sh:909`) and there is no fleet-wide tally door, which is why the previous draft's criterion named no runnable instrument. "Without a bounce" is defined as: **no `RESTART` line in any bot's `keepalive.log` inside the window**.
  - **Alert bar widened:** zero `session_missing`, zero `service_down`, zero `bridge_down`. `bridge_down` alone was satisfiable *trivially by queueing* — `lib/fleet-pulse.sh:347` gates the bridge check on a live session, so a gate that stalled every bot for the whole window scored a perfect zero.
  - **Restore the self-start instrument's read** (spec §10 asks for it and the previous draft dropped it): run `lib/selfstart-snapshot.sh` **before any rescue**, and record its page including `BOUND_STATE`. Under F11 the rung still exists, so the instrument is unchanged by PR B and its page is a clean second read.
  - **`lib/bench-cold-start.sh` before and after**, so the serialization cost is measured rather than assumed — the repo's standing timing harness, which the previous draft re-invented around.
  - **Record how many bots on the proof host have no channel** (`bridge_state` → `no_handle`, plus any declared tokenless canary). Their slots release ~0.5 s after session creation, so they consume a grant without consuming the resource the gate rations; the tally is uninterpretable without that count (see `## Stated limitations`).
  - **Stated limitation, not an omission:** the systemd leg is proven by test and CI only. The operator's ruling is that deploys go to the primary host and never to the Pi, so no reboot of a systemd host is taken in this program; "divergence between the two supervisors is impossible" is therefore evidenced by the round-trip and conformance tests, not by a live boot. Post the tally in the deploy comment behind `no_names`.

## Door consumer tables

The operator's standing rule for this program: *every fix targets the mechanism, not the instance.* An unlisted consumer of a changed door is handled by the plan owning the door's consumer list, never by patching two call sites. Two doors change or are slated to change; both get a table and both are covered by `tests/test_door_consumers.py`.

- **`service_is_starting`** — the table is in Task 2 Step 4 (4 consumers; 3 of them were unlisted in the previous draft, and 2 of those are the alerting path).
- **`boot_rung_for`** — deferred with F11, table recorded in `## Deferred` (5 consumer lines across 3 files plus a bash suite; 2 of the 3 files were unlisted).

## Stated limitations

Four cycle-1 findings are answered by *stating a bound* rather than by code. Each is real, none is closed by this PR, and leaving them implicit is how a reader inherits a wrong model.

- **The gate rations nothing for a bot with no channel.** `wait_bridge_ready_state` returns on its **first** poll for `no_handle` and for a declared tokenless canary (`lib/lib-common.sh:1281-1292`), so such a bot's slot is released ~0.5 s after session creation while its MCP servers are still spawning — and the spec's causal story (§1: "every session spawns about five MCP servers") applies to it identically. The queue behind it buys nothing for that stretch. PR B does **not** floor the hold, because the floor's value would be a guess of exactly the kind F13 is trying to stop; instead Task 5 records **how many non-channel bots sit on the proof host**, so the tally is interpretable and the floor can be set from data if the run shows it is needed.
- **A kicked bot loses its queue position, and nothing ages a waiter's priority.** Priority is a property of the ticket name, so a later manager ticket legitimately sorts ahead of a waiting worker — and `svc_kick` on Darwin is `launchctl kickstart -k`, which kills the current launcher, so a *queued* bot that is kicked releases its ticket and re-enters at the back with a fresh arrival stamp. PR B closes the route that fires by itself: keepalive's dead-session branch is gated by the marker rung, and the marker now lives to **release**, so keepalive never kicks a queued bot. What remains is a *deliberate* kick — a hand run, or a rolling restart — landing on a bot that is already queued. That is rare, it is an operator action, and the scoped caller cap bounds what it costs. Nothing bounds re-queue count; if the reboot shows repeated re-queues, the remedy is to preserve the original arrival stamp across a re-queue, and that is a PR C item rather than an unstated risk here.
- **The ticket subsystem exists for exactly one property, and it is worth saying what that costs and buys.** Strip priority and the gate is a plain counting semaphore — no arrival epochs, no fixed-width keys, no precedence check, no ticket half of the reaper. It buys ratified decision 3 (managers never queue behind workers), worth roughly **one drain round** for the 1–2 managers on a host: at 1 slot and a 58 s hold, ~19 minutes earlier for the last manager; at 3 slots, ~5. The previous draft's second cost — "who is waiting" recorded twice, in `tickets/` and in each bot's `.boot-queued`, with two lifetimes and one reaper covering only one of them — is **closed**: both are written at acquire, both are removed at release, and `_boot_admission_reap` covers tickets, slots **and** markers.
- **`service_is_starting` still answers differently on the two OSes, and PR B's own test pins that.** The marker rung is platform-neutral, but the Linux SubState rung stays and covers one thing the marker cannot: a start that **failed before `start-bot.sh` ever ran** — a unit in `activating` during `ExecStartPre`, or one whose `ExecStart` never reached the gate. In those windows there is no marker to read because no launcher wrote one. That is why the stale-marker cell expects `1` on Darwin and the stubbed SubState on Linux: it is a real residual, not an oversight. It narrows further when F11's PR C retires the stagger and the `activating` arm loses its producer.

## Test Plan

| suite | what PR B does to it | expected |
|---|---|---|
| `tests/test_boot_admission.sh` (new, via `tests/test_sh_suites.py:33`) | the 16 cells (a)(b)(c)(c2)(c3)(c4)(d)(d2)(d3)(e)(f)(f2)(g)(g2)(g3)(g4), plus the no-second-serializer grep assertion | all green |
| `tests/test_service_is_starting.py` (**existing**, #1002) | extended with 7 marker cells; `TestGraceCap` parameterized on `BOOT_GRACE_S` | green, and red on the marker mutant |
| `tests/test_door_consumers.py` (new) | checks in the `service_is_starting` consumer map | red when an unlisted consumer appears |
| `tests/test_boot_policy.py` | `hold_ceiling_s` and `boot_grace_s` derivations (F15) | green |
| `tests/test_boot_policy_conformance.py` | the two new keys once each in `bot.conf`, absent from both units | green |
| `tests/test_bash_parse.py` | covers `lib/boot-admission.sh` by construction | green |
| `lib/validate-bot-change.sh` | one new block (Step 6), six `BOOT_LOCK_HOLD_S=0` sites removed | count moves; **record the new pass/fail pair by name before running the comparison**, and compare names |
| the two-leg gate | rc + scoped names + count line, against Task 0's before-leg | no new names; count delta explained by the harness block |

## Verification Checklist

- [ ] `grep -n 'BOOT_LOCK\|\.claudlobby-fleet-boot\.lock' lib/ -r` returns **zero** hits (the #304 lock is deleted, and no harness site still sets `BOOT_LOCK_HOLD_S`).
- [ ] `grep -rn 'service_is_starting' lib/ | grep -v 'lib-common.sh'` shows a `bot_dir` second argument at **all four** call sites, and no `[ -n "$BOT_SERVICE" ] &&` short-circuit in front of any of them.
- [ ] `tests/test_door_consumers.py` fails when a fifth `service_is_starting` call site is added (shown, restored) and passes as committed.
- [ ] With one slot, two concurrent `start-bot.sh` runs grant one and queue the other; the queued one is granted after the first's poll ends (harness), **and** at two slots both are granted at once (the positive control).
- [ ] A keepalive tick **and** a fleet-pulse tick against a queued bot on `_OS=Darwin` issue no restart and emit no `session_missing` / `service_down` (suite + harness).
- [ ] The reused-pid cell (c4) goes **red** when the `marker_age_within` half of the liveness pair is removed (shown, restored).
- [ ] `$_LC_TMPDIR` is gone after `start-bot.sh` exits, and the cell goes red when `_lc_cleanup` is dropped from the composed trap (shown, restored).
- [ ] Every gate log token is `ADMISSION_WAIT` / `ADMISSION_GRANTED` / `ADMISSION_RELEASED` / `ADMISSION_TIMEOUT` — `grep -rn 'RELEASED' lib/ documentation/` shows no bare `RELEASED`.
- [ ] `boot_admission_timeout` and `boot_admission_unavailable` are registered in `claudlobby/known_values.py` (otherwise `claudlobby brief` cannot return them — #903).
- [ ] The harness's macOS pass/fail pair matches the number recorded in the Test Plan **by name**, and the two-leg diff introduces no new failure names.
- [ ] Live, on the primary host only: a restarted manager's `logs/startup.log` carries `ADMISSION_GRANTED slot=0 after 0s` and `ADMISSION_RELEASED`.
- [ ] Live, after the planned reboot: every bot's startup log carries `ADMISSION_GRANTED`; grants are monotonic and managers-first; zero `boot_admission_timeout`; zero `boot_admission_unavailable`; zero `session_missing` / `service_down` / `bridge_down`; no `RESTART` line in any `keepalive.log` inside the window — all read against **Task 0's PR-A-only control**, with the 2026-09-19 figure printed NOT COMPARABLE.
- [ ] `lib/selfstart-snapshot.sh`'s page taken **before** any rescue, with `BOUND_STATE` recorded.

## What NOT To Do

- Do not leave a second serialization primitive in `lib/start-bot.sh`. The #304 lock is deleted **in the same commit** that lands the gate; the gate is that lock done properly, not a protocol beside it.
- Do not let the gate block indefinitely or fail closed. Every failure path — unwritable state dir, unresolvable epoch, empty unit name, cap reached — proceeds, logs and (where it is a verdict) events.
- Do not let **the gate degrade into a synchronized release.** Every ticket at a cold boot is minted in the same second, so an undispersed flat cap expires them all in the same second and rebuilds the storm it replaces. This is the anti-pattern the previous draft had no row for and two of its blockers shared.
- Do not reap a pid-less slot inside the claim grace, and do not trust `kill -0` alone. Both are answers to "I cannot see the holder", and neither may license a delete (#1146, #1425).
- Do not reword a comment or a help string to make a ratchet count. `CALL_PATTERN` counts prose; the floors above are **3 / 0 / 1 / 1 / 1**, and `lib/spin-down-bot.sh:42` is the #828 standing-posture warning CLAUDE.md itself quotes.
- Do not make `_boot_admission_reap` the garbage collector for PR A's plugin stamps. They are written under `with_lock`; deleting them unlocked from every waiter every 2 s re-arms the amplifier PR A removed.
- Do not add an operator knob. `BOOT_ADMISSION_CALLER_CAP_S`, `BOOT_ADMISSION_POLL_S` and `BOOT_ADMISSION_CLAIM_GRACE_S` are `lib/`-internal call conventions and compose-time constants; document them as not-knobs rather than leaving them undocumented.
- Do not deploy to the Pi, and do not retire the systemd stagger in the same deploy that first exercises the gate.
- Do not touch `library/`, fleet manifests or operator `.env` files.

## Deferred

Everything below is real work with a real finding behind it. It is deferred, not dropped, and each row carries enough to be lifted verbatim.

| # | unit | why it moved | where it sits |
|---|---|---|---|
| D1 | **Task 3 — the five boot-path scripts onto the adapter** (25 sites, the two installer bodies into `lib/supervisor.sh`, the `ExecStopPost` three-spelling fold) | Pure refactor, zero incident benefit, largest source of new risk in the riskiest deploy of the program. **Verified: no high-ROI unit depends on it** — `restart_bot_service` (`lib/keepalive.sh:189-211`) already ends every branch in `start-bot.sh`, so the gate catches every keepalive bounce with the ladder exactly as it is. Ratchet shrink bought: 109 → 84 (23%). | **F12, open.** Lean: PR C. Corrections already folded, carry them over: (i) `rc=0; svc_kick "$BOT_DIR" \|\| rc=$?; if [ "$rc" -eq 2 ]; then start-bot.sh; fi` — never `\|\|`, which double-starts a bot whose restart **failed** (R13); (ii) `spin-up-bot.sh` is a **three-way** (registered under the composed name → kick; registered only under the pre-rename name with a new unit composed → enroll/migrate; else enroll), not "calls `svc_enroll`" — `:40-42` is a migration branch a naive rewrite restarts forever; (iii) the log breadcrumb is `out="$(safe_mktemp)"; rc=0; svc_kick "$BOT_DIR" >"$out" 2>&1 \|\| rc=$?; desc="$(head -1 "$out")"; cat "$out" >>"$LOG"` — never `desc="$(svc_kick …)"`, which yields the line only after the restart returns, captures the binary's stdout and drops its stderr; (iv) name the wrapper→verb cycle break (`_svc_enroll_systemd` / `_svc_enroll_launchd`, never the verb) or `install-bot-systemd.sh` → `lib-common.sh` → `svc_enroll` → `install-bot-systemd.sh` recurses on the enroll path `setup-fleet` runs per bot, and decide whether the moved Darwin body becomes bare `launchctl` (fakeable, a real behaviour change) or stays absolute (and `svc_enroll` gets no hermetic contract test — `lib/supervisor.sh:54-65`); (v) the ratchet floors are **3 / 0 / 1 / 1 / 1**, not zeros, so the checklist line reduces to `spin-up-bot.sh` alone. |
| D2 | **Task 4 — the systemd stagger, `bot_boot_delay_s`, `_host_boot_rung_bases`, `boot_rung_for` retired** | The stagger is composed **only** into the systemd unit, i.e. only onto the host this program does not deploy to. Retiring it in PR B would ship an unprovable change and simultaneously first-exercise the new mechanism. It can also make boot *worse* there: it starts ~21 launchers at t=0 into `seed_workspace_trust` (`:108`), `seed_all_checkouts` (`:116`) and the plugin block, and on a four-core host `derive_slots` yields **1** slot for ~21 bots. | **F11, locked (PR C).** |
| D3 | `boot_rung_for`'s **consumer table** — `lib/selfstart-snapshot.sh:692`, `lib/boot-capture.sh:161` and `:253`, `tests/test_selfstart_snapshot.sh:1181` | Two of the three files were unlisted in the previous draft. `lib/boot-capture.sh` is part of the instrumentation the reboot proof reads, and an undefined function under `set -euo pipefail` is rc 127 — it would abort the run and empty a recording whose own header says "no boot before this lands can be backfilled". | PR C, into `tests/test_door_consumers.py`'s same map. |
| D4 | **`VALID_AT` / `SELF_START_BOUND`** in `lib/selfstart-snapshot.sh` | Retiring the rung retires the instrument's *validity claim* and its lateness bound, not only its "not yet due" gate: `MAX_RUNG` → `VALID_AT` (`:715-719`) and the alias at `:884`, `:1194`, `:1208-1210`. Post-retirement `$TMP/rungs` is empty **by construction**, so either `MAX_RUNG=0` (the page claims "this is a result" at boot+30–60 s while the gate may legitimately still be queueing, every queued bot reads as a strand and every genuine self-start past that instant is credited `LATE-UNEXPLAINED`) or `BOUND_STATE="NO-LADDER"` permanently suppresses the bound on both OSes and negatively-defined `SELF-STARTED` swallows every unrecorded wake (#1203, measured 3-of-21 printed where 2 was honest). The previous draft's replacement disclosure — "no admission markers (units predate PR B)" — also **states a false reason**: post-retirement the ladder is gone by design and the marker is ephemeral, so "no markers" is the steady state. | PR C. Carry: state what `VALID_AT` *becomes* (`BOOT_EPOCH + max(admission_wait_max_s) + max(ready_timeout_s) + FIRST_TURN_ALLOWANCE_S` read across declared `bot.conf`s, keeping the three-state answer — a `-1`-equivalent for "no `BOOT_*` anywhere", never `0` — and the suppress-and-disclose posture at `:732-734` / `:1266-1283`); give `lib/boot-capture.sh`'s `BOUND_AT` the **same** replacement so the two cannot fork; print the historical baseline NOT COMPARABLE again; add the queued-bot NOT-YET-DUE cell **and** a cell asserting the header refuses to call the page a result. Also: stamp `.boot-queued` with its boot epoch, so `selfstart-snapshot.sh` can tell a boot-queue from a restart-queue — `.boot-queued` is written by *every* `start-bot.sh` run and inherits the `.spawn` lesson `lib/boot-capture.sh:19-25` already records verbatim. |
| D5 | The `activating/*` arm and its **three** comment copies (`lib/lib-common.sh:4191`, `:4247`, `:4256-4270`; `claudlobby/supervision.py:141-145`), the `RETIRED_IN_PR_B` constant (`claudlobby/supervision.py:60`, **a shipped-module parser-level key-drop set**, not a test-side allowance — both ends and the import at `tests/test_supervision_roundtrip.py:41`), the 19 tests (`tests/test_composer.py:2183/2188/2193` plus the 16-test `TestHostBootOffset` at `:2222-2531`), `tests/test_composer_gaps.py:504-508`, `tests/test_boot_capture.sh`, `tests/test_selfstart_snapshot.sh`, and the `ExecStartPre` grep criterion | All consequences of D2. The grep criterion must be re-stated as a **comparison, not a count**: widened to `grep -rn 'ExecStartPre' claudlobby lib tests documentation` with the expected surviving set named **by file**. Measured now, excluding this program's own plan docs: `claudlobby/composer.py` 1, `claudlobby/supervision.py` 5, `lib/lib-common.sh` 6, `lib/validate-bot-change.sh` 3, `lib/rehearse-env-cascade.sh` 1, `lib/selfstart-snapshot.sh` 1, `tests/test_supervision_roundtrip.py` 4, `tests/test_selfstart_snapshot.sh` 4, `tests/test_service_is_starting.py` 3, `tests/test_boot_policy_conformance.py` 2, `tests/test_composer.py` 1, `tests/test_composer_gaps.py` 1, `tests/test_boot_capture.sh` 1, `documentation/runbooks/audit-cold-start-timing.md` 1, `documentation/architecture/system-map-2026-07-30.md` 2. Also: state whether `TestHostBootOffset`'s **managers-first** and **one-bot-per-rung** properties move to `tests/test_boot_admission.sh` as gate-level assertions or are deliberately dropped — `560c3c9` is cited as the intent the ticket priority preserves and nothing currently asserts the gate preserves it. And decide whether the #1002 harness scenario (`lib/validate-bot-change.sh:2419-2452`, `ExecStartPre=/bin/sleep 4`) is retired, retargeted at the marker, or kept as a predicate-only probe with a comment saying composed units no longer reach that state. | PR C. |
| D6 | The `tests/test_roster_doors.py` conditional | **Resolved to "no"** — measured, zero occurrences of `boot_rung_for` in that file. Deleted from the doc rather than carried as a conditional. | closed |
| D7 | `claudlobby/supervision.py:172-175`'s counter-plan (a composed `BOOT_DELAY_S` honoured by `start-bot.sh`) | Evaluated, not built. See **F16**. | F16, locked |

## Context

area: supervision / boot path · effort: **M** on the lean, **L** if F12 keeps Task 3 · risk: high (every bring-up on the primary host passes the new gate, and the carrier is the pull, not the restart) · priority: P1

**Six numbered tasks, one of them conditional:** 0 the before-leg + the control reboot (**S**) · 1 the gate library (**M**) · 2 the marker, the door tables, the harness (**M**) · 3 the adapter migration (**M**, *conditional on F12; the lean is PR C*) · 4 docs (**S**) · 5 gauntlet, deploy, the reboot proof (**M**).

Against the pre-re-forge draft: it was six tasks at **L**. The stagger retirement (old Task 4) is **locked out** to PR C by F11, the adapter migration (old Task 3) is **conditional** under F12, and a new Task 0 adds the before-leg and the PR-A-only control reboot that the proof needs in order to attribute anything. Deferred work: rows D1–D5, ≈ **L**, PR C.

Linear: neither.
