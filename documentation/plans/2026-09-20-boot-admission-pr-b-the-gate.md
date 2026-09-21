---
title: PR B — the admission gate, the boot-progress marker, the migration, the stagger retired
type: plan
status: draft
date: 2026-09-20
epic: documentation/plans/2026-09-20-boot-admission-plan.md
spec: documentation/plans/2026-09-20-boot-admission-and-supervision-consolidation-design.md
issue: "#1573"
---

# PR B — the admission gate, the boot-progress marker, the migration, the stagger retired

## Summary

Every bring-up on a host passes through one priority admission gate inside `start-bot.sh`, bounded by the host-derived slot count and ordered managers first. A `data/.boot-queued` marker becomes the boot-progress signal `service_is_starting` reads first on both OSes, so keepalive never kickstarts a queued bot. Keepalive's restart ladder, `spin-up-bot.sh`, `spin-down-bot.sh` and the two bot installers move onto the adapter and the ratchet shrinks. The systemd stagger, `bot_boot_delay_s` and `boot_rung_for` are retired; the self-start instrument reads the markers. The proof is a planned reboot of the host.

## Evidence (main at `1053659`, plus PR A)

- `lib/start-bot.sh:221` `CLAUDE_CMD` (`. .tmux-env; exec claude …`); `:285` `tmux new-session`; `:287` `touch data/.spawn`; the readiness poll follows (PR A moved it into `wait_bridge_ready_state`).
- `lib/keepalive.sh:318-324` the dead-session branch guards on `service_is_starting "$BOT_SERVICE"` (service name only; `BOT_DIR` in scope); `:185-208` the restart ladder to replace with `svc_kick` plus the existing `start-bot.sh` fallback on rc 2.
- `lib/lib-common.sh:3784` `service_is_starting` returns 1 off Linux; `:3602` `boot_rung_for` reads `ExecStartPre` from unit text and returns -1 on launchd.
- `lib/selfstart-snapshot.sh:107-124, 300, 653-659` the rung-derived "not yet due" and its disclosure when no rung is readable.
- `claudlobby/composer.py:1339` the `ExecStartPre` line; `:2766-2779, 2869` the rung threaded into `compose_bot`; `:3332` `_BOOT_STAGGER_SECONDS`; `:3380` `_host_boot_rung_bases`; `:3442` `bot_boot_delay_s`; `:4633` the comment about not passing the rung. Tests: `tests/test_composer.py:2160-2196` (stagger tests; the triple test stays), `:2490` (`test_bot_boot_delay_places_each_bot_on_its_own_rung`).
- Prior art: `560c3c9` "order the host ladder managers-first, then workers (#1002)" — the intent the ticket priority preserves.
- `lib/spin-up-bot.sh` (3 direct calls), `lib/spin-down-bot.sh` (5), `lib/install-bot.sh` (4), `lib/install-bot-systemd.sh` (4), `lib/keepalive.sh` (9): the sites this PR moves; the ratchet allowlist shrinks by their counts.

## Implementation Plan

### Dependencies
PR A merged and deployed (the `BOOT_*` keys in `bot.conf`; `lib/supervisor.sh`; `wait_bridge_ready_state`).

### Blocks
The follow-up migrations (`setup-fleet`, `reconcile-fleet.sh`, timer installers, rehearsal scripts).

### Steps

### Task 1: the admission gate library

**Files:** `lib/lib-common.sh` (or `lib/boot-admission.sh` sourced by it — same F1 reasoning as the adapter; lean: a separate file), `tests/test_boot_admission.sh` (new).

- [ ] **Step 1 (tests first):** `tests/test_boot_admission.sh` with a temp `CLAUDLOBBY_ROOT`: (a) with 1 slot, a worker ticket that arrives first still waits when a manager ticket arrives before the worker is granted — the manager is granted first; (b) with 2 slots and three waiters, exactly two hold slots at once; (c) a slot directory whose recorded pid is dead is reclaimed by the next waiter; (d) a waiter past `BOOT_ADMISSION_WAIT_MAX_S` returns the `timeout` verdict and the caller proceeds; (e) `boot_admission_release` removes the slot and a trap-driven release works when the caller exits mid-hold; (f) the `.boot-queued` marker exists while waiting and is gone after grant; (g) an unwritable state dir yields the `unavailable` verdict without stalling.
- [ ] **Step 2:** The library:

```bash
boot_admission_acquire <bot_dir>   # reads BOOT_* from bot.conf; writes data/.boot-queued and a ticket; loops (2s) until granted; prints granted:<slot> | timeout | unavailable; rc 0 on granted, 0 on timeout/unavailable too (the caller proceeds), never blocks past the cap
boot_admission_release <bot_dir>   # removes this bot's slot (by recorded pid) and its ticket if any; idempotent
_boot_admission_reap               # removes tickets/slots whose pid is dead, and plugin stamps (state/boot/plugins-updated.<epoch>.*) whose epoch is not the current boot's
```
Ticket name: `<priority:1 digit>-<arrival epoch, 19 digits zero-padded ns or s+seq>-<bot-service>`; slot dirs `slots/<n>` with a `pid` file inside; `state/boot/` created with `mkdir -p`; the grant rule: no ticket sorts before ours AND a slot `mkdir` succeeds. Log lines exactly as the spec §6.2 names them; the single event `boot_admission_timeout` through `emit_fleet_event`, and `boot_admission_unavailable` on the unwritable path.
- [ ] **Step 3:** Verify the suite through `test_sh_suites.py`. Commit: `feat(boot): the admission gate — one priority queue and host-derived slots for every bring-up`.

### Task 2: wiring, the marker, keepalive's first rung, the harness scenario

**Files:** `lib/start-bot.sh`, `lib/lib-common.sh` (`service_is_starting`), `lib/keepalive.sh`, `lib/validate-bot-change.sh`, `tests/test_service_is_starting.sh` (new or extend an existing suite).

- [ ] **Step 1:** `start-bot.sh`: after `bot.conf` is sourced and before `tmux new-session` (`:285`): `_adm="$(boot_admission_acquire "$BOT_DIR")"`; `trap 'boot_admission_release "$BOT_DIR"' EXIT` (compose with the existing traps; `install_error_trap` arms `set -E`); after the readiness poll ends (READY or TIMEOUT), `boot_admission_release`. The marker is written by the acquire and removed on grant/timeout/unavailable.
- [ ] **Step 2:** `service_is_starting <service> [bot_dir]` (F8): when `bot_dir` is given and `data/.boot-queued` is younger than `BOOT_ADMISSION_WAIT_MAX_S + RC_READY_TIMEOUT_S` (from that bot's `bot.conf`), return 0 on any OS; else the existing Linux SubState logic; else 1. Keepalive's call at `:324` passes `"$BOT_DIR"`. The unit-file comment in the composer about `activating` is rewritten to name the marker as the first rung.
- [ ] **Step 3 (tests):** a bash suite for `service_is_starting`: marker fresh → 0 on Darwin and Linux; marker stale → falls through (1 on Darwin; the stubbed SubState on Linux); no marker → unchanged behaviour.
- [ ] **Step 4 (harness):** a new block in `lib/validate-bot-change.sh`: two throwaway bots with `BOOT_ADMISSION_SLOTS=1` in their `bot.conf`, started together through the real `start-bot.sh` with the stubbed `claude`; assert: the second holds `data/.boot-queued` while the first is inside its readiness window; one keepalive tick against the second logs a SKIP (starting) and issues no restart; after the first's poll ends, the second is granted and its marker is gone. Reap both.
- [ ] **Step 5:** Verify the suites, then the harness directly. Commit: `feat(start-bot): every bring-up passes the admission gate, and a queued bot is mid-boot on both supervisors`.

### Task 3: the boot path on the adapter

**Files:** `lib/keepalive.sh`, `lib/spin-up-bot.sh`, `lib/spin-down-bot.sh`, `lib/install-bot.sh`, `lib/install-bot-systemd.sh`, `lib/supervisor.sh`, `tests/supervisor_ratchet_allowlist.json`, `tests/test_rolling_restart.sh` and any suite that stubs `systemctl`/`launchctl` for these scripts.

- [ ] **Step 1:** Keepalive's ladder (`:185-208`) becomes `svc_kick "$BOT_DIR" || "$LIB_DIR/start-bot.sh" "$BOT_DIR"`, keeping the log line per branch by reading `svc_kick`'s printed description. The heal bounce goes through the same call (nothing else changes in `_bridge_heal`).
- [ ] **Step 2:** `install-bot.sh` and `install-bot-systemd.sh` become the bodies of `svc_enroll`'s two spellings (moved into `lib/supervisor.sh`); the two scripts stay as thin wrappers that call the verb (CLAUDE.md names them; setup-fleet calls them). `spin-up-bot.sh` calls `svc_enroll`; `spin-down-bot.sh` calls `svc_disenroll` for the supervision leg and keeps its receipt and purge legs.
  - **Fold the `ExecStopPost` idiom while you are in here** (PR A review, finding 9). One property is currently spelled in three places: the spec field carries `rm -f <dir>/.tmux-env`, `claudlobby/supervision.py:158` prepends the `/bin/` prefix in the template (`ExecStopPost=/bin/{spec.stop_post_command}`), and `lib/supervisor.sh`'s launchd teardown re-spells the same `rm -f` by hand. Nothing is broken today — output is byte-identical to pre-#1573 and the round-trip parser strips the prefix symmetrically — but "one spec property, two idioms" is the module's whole point, and this task already owns the `svc_disenroll` half.
- [ ] **Step 3:** The ratchet allowlist shrinks to the measured counts (expect: keepalive 0, spin-up 0, spin-down 0, the two installers 0); the ratchet test's shrink output is pasted into the PR body.
- [ ] **Step 4:** Verify: every suite that exercises these scripts, then the harness directly. Commit: `refactor(lib): the boot path speaks to the supervisor through the adapter only`.

### Task 4: the stagger retired

**Files:** `claudlobby/composer.py`, `claudlobby/supervision.py`, `tests/test_composer.py`, `tests/test_supervision_roundtrip.py`, `tests/test_boot_policy_conformance.py`, `lib/lib-common.sh` (`boot_rung_for`), `lib/selfstart-snapshot.sh`, `tests/test_selfstart_snapshot.py`, `tests/test_roster_doors.py` if it names `boot_rung_for`.

- [ ] **Step 1:** Remove `boot_delay_s` from `compose_systemd_unit`, the `ExecStartPre` line, `_BOOT_STAGGER_SECONDS`, `bot_boot_delay_s`, `_host_boot_rung_bases`, and the rung threading in `compose_bot` (`:2766-2779, 2869, 4633`). The `RemainAfterExit` triple and its test stay. The composer's unit comment block names the marker as the boot-progress signal.
- [ ] **Step 2:** `tests/test_composer.py`: the three stagger tests and the rung test are replaced by one: no composed unit ever contains `ExecStartPre`. `tests/test_supervision_roundtrip.py`: the `RETIRED_IN_PR_B` allowance is deleted. `tests/test_boot_policy_conformance.py`: add the no-`sleep` assertion on both units.
- [ ] **Step 3:** `boot_rung_for` retired; `selfstart-snapshot.sh`: "not yet due" = `data/.boot-queued` fresh, or no `data/.spawn` newer than the boot within `BOOT_ADMISSION_WAIT_MAX_S`; the "no readable rung" disclosure becomes "no admission markers (units predate PR B)". Its tests updated accordingly.
- [ ] **Step 4:** Verify + the naked-bot gate against the current baseline (`bot.conf` is not a recorded surface; expect no drift — if it drifts, stop and report). Commit: `refactor(compose): the systemd stagger and the rung reader retired — the gate is the only bring-up mechanism`.

### Task 5: docs, CHANGELOG

- [ ] `CLAUDE.md`: the runtime-model paragraph names the admission gate and the marker; the `start-bot.sh`, `keepalive.sh`, `spin-up-bot.sh`, `spin-down-bot.sh`, `install-bot*.sh`, `selfstart-snapshot.sh` rows updated in one sentence each. `documentation/environment-variables.md`: the `BOOT_*` keys, the markers, the events. `documentation/fleet-update-lifecycle.md`: one paragraph — a bring-up now queues. `CHANGELOG.md`: one bullet per task. Commit: `docs: the admission gate, the marker and the retired stagger`.

### Task 6: gauntlet, deploy, the reboot proof

- [ ] Mutants: priority ignored (worker granted before a waiting manager), cap ignored (a third holder), stale slot never reaped, wait cap never fires, marker never removed, `service_is_starting` ignoring the marker, keepalive falling back to a direct `launchctl`, the stagger line returning.
- [ ] The two-leg gate, the harness directly, CI, the final review folded once.
- [ ] Deploy: pull, generate both fleets, one manager restart per fleet through the proven sequence; read the manager's startup log for `ADMISSION_GRANTED slot=0 after 0s` (a calm host grants at once) and `RELEASED`.
- [ ] Proof: a planned reboot of the host (operator-scheduled). Read the bridge tally (`bridge_state` per bot) at 5, 10 and 15 minutes; the target is every bridge up without a bounce and zero `bridge_down` alerts, against the 2026-09-19 baseline of 7 of 18 after the first bring-up. Post the tally in the deploy comment behind `no_names`.

## Test Plan

Bash suites: `test_boot_admission.sh`, the `service_is_starting` suite, the updated suites for the migrated scripts. Python: the composer, conformance, round-trip and self-start tests updated. The harness scenario. The standing two-leg gate, the harness directly, CI.

## Verification Checklist

- [ ] With one slot, two concurrent `start-bot.sh` runs grant one and queue the other; the queued one is granted after the first's poll ends (harness).
- [ ] A keepalive tick against a queued bot on `_OS=Darwin` issues no restart (suite + harness).
- [ ] `grep -rn 'ExecStartPre' claudlobby tests` prints only the one negative test.
- [ ] `tests/supervisor_ratchet_allowlist.json` no longer lists `keepalive.sh`, `spin-up-bot.sh`, `spin-down-bot.sh`, `install-bot.sh`, `install-bot-systemd.sh`.
- [ ] Live: the restarted managers' logs carry `ADMISSION_GRANTED` and `ADMISSION_RELEASED`.
- [ ] Live, after the planned reboot: every bridge up without a bounce; zero `bridge_down` alerts in the first fifteen minutes.

## What NOT To Do

- Do not make the gate block indefinitely or fail closed; the wait cap and the unavailable path both proceed.
- Do not remove the `RemainAfterExit` triple or its test.
- Do not migrate call sites beyond the five boot-path scripts; the ratchet handles the rest later.
- Do not touch `library/`, fleet manifests or operator `.env` files.

## Context

area: supervision / boot path · effort: L · risk: high (every bring-up on every host passes the new gate) · priority: P1

Linear: neither.
