---
title: PR A — the boot policy truth, the renderer boundary, the supervisor adapter
type: plan
status: draft
date: 2026-09-20
epic: documentation/plans/2026-09-20-boot-admission-plan.md
spec: documentation/plans/2026-09-20-boot-admission-and-supervision-consolidation-design.md
issue: "#1573"
---

# PR A — the boot policy truth, the renderer boundary, the supervisor adapter

## Summary

Define boot policy once (`BootPolicy`, `claudlobby/boot.py`) from a `host.boot` block in the package `system.yaml`, render it once into `bot.conf`, and pin that both unit renderers carry none of it. Give both renderers one `SupervisionSpec` and a round-trip equivalence test. Add `lib/supervisor.sh` with five verbs, contract tests against fake binaries, and the ratchet test grandfathering today's 108 direct call sites. Fix the three amplifiers: `MCP_TIMEOUT` exported, a wall-clock readiness ceiling that names why it fired, plugin updates at most once per host boot. No behaviour change on the boot path beyond those three.

## Evidence (main at `1053659`)

- `claudlobby/composer.py:814` `compose_bot_conf`; the composed keys today include `BOT_ID`, `FLEET_NAME`, `SERVICE_PREFIX`, `TMUX_TMPDIR`, `CLAUDE_FLAGS` (`:889-912`); nothing boot-policy-shaped.
- `claudlobby/composer.py:1334-1339` the systemd writer with `boot_delay_s`; `:1390` the plist writer; `:3332` `_BOOT_STAGGER_SECONDS`; `:3442` `bot_boot_delay_s`.
- `claudlobby/config.py:1768` `load_host_jobs()` reads `host.jobs` from the package `system.yaml` (host jobs bypass the fleet merge — the same shape `host.boot` needs).
- `claudlobby/system.yaml:52-70` the `host:` section (only `jobs:` today).
- `lib/start-bot.sh:268-277` the per-start plugin install/update loop; `:290-301, 345-396` the readiness loop (`_rc_iters = RC_READY_TIMEOUT_S * 2`, `sleep 0.5`, `_poll_start=$(date +%s)` already captured at `:300`); the two `TIMEOUT` lines at `:386` (not-ours) and `:388` (never reached up, no state named).
- `lib/lib-common.sh:314` `with_lock`; `:3561` `resolve_boot_epoch`; `:876` `bridge_state` (its printed states: `up`, `no_bridge`, `no_handle`, `no_token`, `unknown`, and the not-ours verdict from the session-scoped call).
- `lib/keepalive.sh:185-208` the restart ladder (systemd, pre-rename systemd, launchd `kickstart -k`, `start-bot.sh` fallback) — the shape `svc_kick` reproduces verbatim.
- `tests/test_composer.py:2160-2196` the stagger tests and the `RemainAfterExit` triple; `:4121-4133` the PATH parity tests (the round-trip test generalizes them); `tests/test_macos_supervision.py` the plist structural test.
- `tests/test_fleet_mission.py`, `tests/test_plane_daemon_units.py`, `tests/test_plane_rc_relay_hook.py` pin `system.yaml.example` against the package file: any `system.yaml` change regenerates the example with the tests' own recipe (PR 2 of the check-in series did this in `ffd3ea1`).
- Direct supervisor invocations: 108 across 20 `lib/` scripts (counted per script with `grep -cE '(^|[^A-Za-z_-])(systemctl|launchctl)( |$)'`); the ratchet re-measures at its own tip.

## Implementation Plan

### Dependencies
None.

### Blocks
PR B (the gate reads the `bot.conf` keys and the adapter this PR lands).

### Steps

### Task 0: Worktree and evidence

- [ ] Worktree `~/Projects/claudlobby-worktrees/boot` on branch `boot/admission-consolidation` from `origin/main` (exists; the spec and plans are its first commits). Evidence dir `~/Projects/claudlobby-worktrees/boot-out/` with an `env.sh` of the check-in series' shape (`no_names` over the host names file). Before leg: a full-suite run on the branch's base in a spare worktree with its own venv (the standing two-leg gate).

### Task 1: `BootPolicy` — the truth

**Files:** `claudlobby/boot.py` (new), `claudlobby/config.py`, `claudlobby/system.yaml`, `system.yaml.example`, `tests/test_boot_policy.py` (new).

- [ ] **Step 1 (tests first):** `tests/test_boot_policy.py`: `resolve_boot_policy` returns defaults with an empty host block; `auto` slots derive `clamp(cpu_count // 4, 1, 4)` for cpu counts 2, 4, 8, 12, 64 (→ 1, 1, 2, 3, 4); an explicit integer wins over `auto`; `ready_timeout_s` is `max(90, mcp_timeout_ms // 1000 + 20)` (F3); a manager gets priority 0 and a worker 1 (`fleet.manager_bots()`); invalid values (negative, non-integer strings) raise `ValueError` naming the key.
- [ ] **Step 2:** `claudlobby/boot.py`:

```python
@dataclass(frozen=True)
class BootPolicy:
    admission_slots: int
    admission_wait_max_s: int
    priority: int            # 0 = manager, 1 = worker
    mcp_timeout_ms: int
    ready_timeout_s: int     # derived: max(90, mcp_timeout_ms // 1000 + 20)
    plugin_update_once_per_boot: bool

DEFAULTS = {"admission_slots": "auto", "admission_wait_max_s": 1200,
            "mcp_timeout_ms": 180_000, "plugin_update_once_per_boot": True}

def derive_slots(cpu_count: int | None) -> int: ...      # clamp((cpu or 1) // 4, 1, 4)
def resolve_boot_policy(bot, fleet, host_boot: dict, *, cpu_count: int | None = None) -> BootPolicy: ...
def bot_conf_lines(policy: BootPolicy) -> list[str]: ...  # the six lines, fixed order
```

`bot_conf_lines` renders: `BOOT_ADMISSION_SLOTS=`, `BOOT_ADMISSION_WAIT_MAX_S=`, `BOOT_PRIORITY=`, `export MCP_TIMEOUT=`, `RC_READY_TIMEOUT_S=`, `BOOT_PLUGIN_UPDATE_ONCE=` (`1`/`0`). Only `MCP_TIMEOUT` is exported: it is the one key Claude Code reads; the others are the launcher's.
- [ ] **Step 3:** `claudlobby/config.py`: `load_host_boot() -> dict` beside `load_host_jobs()` (same loader, key `host.boot`, `{}` when absent). `claudlobby/system.yaml`: add the `host.boot` block with every default and a comment per key saying what it governs and that `auto` derives from the host. Regenerate `system.yaml.example` with the pinning tests' own recipe.
- [ ] **Step 4:** Verify: `./.venv/bin/pytest tests/test_boot_policy.py tests/test_fleet_mission.py tests/test_plane_daemon_units.py tests/test_plane_rc_relay_hook.py -q` green. Commit: `feat(boot): BootPolicy — boot policy defined once, from the package host section`.

### Task 2: `bot.conf` carries the policy; the units carry none of it

**Files:** `claudlobby/composer.py`, `tests/test_composer.py`, `tests/test_boot_policy_conformance.py` (new).

- [ ] **Step 1 (tests first):** `tests/test_boot_policy_conformance.py`: compose a fixture fleet (one manager, two workers); for every bot, each of the six keys appears in `bot.conf` exactly once with the resolved value; `MCP_TIMEOUT` is the exported form; the rendered systemd unit and plist contain none of `BOOT_`, `MCP_TIMEOUT`, `RC_READY_TIMEOUT_S`. (The systemd stagger line is still present in this PR; PR B's version of this test adds the no-sleep assertion.)
- [ ] **Step 2:** `compose_bot_conf` appends `bot_conf_lines(resolve_boot_policy(bot, fleet, load_host_boot()))` in one block with a one-line comment naming the spec. Nothing else in the file moves (the existing `bot.conf` byte-identity tests must stay green).
- [ ] **Step 3:** Verify: `./.venv/bin/pytest tests/test_boot_policy_conformance.py tests/test_composer.py tests/test_freshbox_selfcontained.py -q` green. Commit: `feat(compose): bot.conf carries the boot policy, exactly once`.

### Task 3: the readiness ceiling in wall-clock time, naming why

**Files:** `lib/lib-common.sh`, `lib/start-bot.sh`, `tests/test_bridge_readiness_wait.sh` (new, through `tests/test_sh_suites.py`), `documentation/environment-variables.md`.

- [ ] **Step 1:** Extract the loop body at `lib/start-bot.sh:345-378` into `wait_bridge_ready_state <bot_dir> <timeout_s> <session_pid> <pretoken>` in `lib-common.sh`: polls `bridge_state` (session-scoped exactly as today), stops on wall-clock `timeout_s` measured with `date +%s`, sleeps 0.5 s between polls, prints the LAST state (`up`, `not-ours`, `no_bridge`, `no_token`, `unknown`) and returns 0 only on `up`. `start-bot.sh` calls it and its `TIMEOUT` line becomes `TIMEOUT — <N>s elapsed (wall clock), last bridge_state=<state>, proceeding anyway`; the existing not-ours wording stays as the `not-ours` case.
- [ ] **Step 2 (tests):** `tests/test_bridge_readiness_wait.sh`: with a stubbed `bridge_state` that returns `no_bridge` for 3 s then `up`, the wait returns 0 and prints `up`; with a stub that always returns `no_bridge` and a 2 s timeout, it returns 1, prints `no_bridge`, and the elapsed wall time is within 2 to 4 s even when each stubbed probe sleeps 1 s (the iteration-count defect, pinned).
- [ ] **Step 3:** Precedence (F4): `start-bot.sh` reads `RC_READY_TIMEOUT_S` from the sourced `bot.conf`; when the key is absent (an old `bot.conf`), the environment value, then 90. Document in `documentation/environment-variables.md`: the composed value wins; the env var is a fallback for un-regenerated bots.
- [ ] **Step 4:** Verify: the new suite through `tests/test_sh_suites.py`, plus `bash lib/validate-bot-change.sh` directly (a `lib/` change the harness exercises; expected `=== 244 passed, 7 failed ===` on macOS with the seven names identical to main). Commit: `fix(start-bot): the readiness ceiling is wall-clock time, and the timeout names the last bridge state`.

### Task 4: plugin updates once per host boot

**Files:** `lib/lib-common.sh`, `lib/start-bot.sh`, `tests/test_plugin_update_once.sh` (new).

- [ ] **Step 1:** `plugin_ensure <plugin> <claude_bin> <log>` in `lib-common.sh`: install when `installed_plugins.json` lacks the plugin (unchanged); otherwise update only when `BOOT_PLUGIN_UPDATE_ONCE` is `1` and no stamp `$CLAUDLOBBY_ROOT/state/boot/plugins-updated.<boot_epoch>` exists, writing the stamp under `with_lock "$CLAUDLOBBY_ROOT/state/boot/plugins.lock"`; when `resolve_boot_epoch` fails, update every start and log `PLUGIN update-once unavailable (boot epoch unresolvable)`. `start-bot.sh:268-277` calls it per plugin.
- [ ] **Step 2 (tests):** with a fake `claude` on PATH that records its argv: two consecutive calls in one boot epoch run `plugin update` once; a new epoch runs it again; a missing plugin still installs on every call; an unresolvable epoch (env `CLAUDLOBBY_BOOT_EPOCH=` empty and the door stubbed to fail) updates every call.
- [ ] **Step 3:** Verify + harness directly. Commit: `fix(start-bot): plugin updates run once per host boot, installs still every start`.

### Task 5: `SupervisionSpec` and the round-trip equivalence test

**Files:** `claudlobby/supervision.py` (new), `claudlobby/composer.py`, `tests/test_supervision_roundtrip.py` (new), `tests/test_composer.py`.

- [ ] **Step 1:** `SupervisionSpec` (frozen dataclass): `label`, `bot_dir`, `launcher` (path), `launcher_args` (tuple), `working_dir`, `environment` (mapping; the two shared keys), `launchd_extra_environment` (mapping; `PATH`, `HOME` — the idiom a LaunchAgent needs), `stop_command` (the tmux kill-server + `.tmux-env` removal, as a string), `stdout_log`, `stderr_log`, `restart_on_failure: bool`. `build_supervision_spec(bot, fleet, paths) -> SupervisionSpec`.
- [ ] **Step 2:** `compose_systemd_unit(spec, *, boot_delay_s=0)` and `compose_launchd_plist(spec)`; their callers build the spec. Byte-identical output to today for every existing fixture (the existing unit tests pin the text; they must not change in this task).
- [ ] **Step 3 (tests):** `tests/test_supervision_roundtrip.py`: `parse_systemd_unit(text) -> dict` (F6: a line parser, sections, repeated keys collected) and `parse_launchd_plist(text)` (`plistlib`); `spec_from_systemd(parsed)` and `spec_from_launchd(parsed)`; for every bot in the fixture fleet both equal the built spec on every field except: the stop hook (asserted present in the unit and absent from the plist, per the spec's table) and the `ExecStartPre` stagger line (an explicit one-entry allowance named `RETIRED_IN_PR_B`). A probe test edits the spec in memory (a new env key) and asserts the round trip fails on the renderer that did not learn it.
- [ ] **Step 4:** Verify: `./.venv/bin/pytest tests/test_supervision_roundtrip.py tests/test_composer.py tests/test_macos_supervision.py -q`. Commit: `feat(compose): one SupervisionSpec behind both unit renderers, pinned by a round-trip test`.

### Task 6: the supervisor adapter and the ratchet

**Files:** `lib/supervisor.sh` (new), `lib/lib-common.sh` (sources it), `tests/test_supervisor_adapter.sh` (new), `tests/test_supervisor_ratchet.py` (new), `tests/supervisor_ratchet_allowlist.json` (new).

- [ ] **Step 1:** `lib/supervisor.sh`, sourced by `lib-common.sh` right after `detect_os`:

```bash
svc_unit_name <bot_dir>       # BOT_SERVICE from bot.conf; pre-rename fallback to BOT_NAME while a unit by that name exists
svc_is_registered <bot_dir>   # rc 0/1; Linux: unit file present under ~/.config/systemd/user; Darwin: plist under ~/Library/LaunchAgents
svc_state <bot_dir>           # prints loaded-active | loaded-inactive | not-loaded | unknown
svc_kick <bot_dir>            # Linux: systemctl --user restart; Darwin: launchctl kickstart -k gui/$(id -u)/<label>; else rc 2 (caller falls back to start-bot.sh)
svc_enroll <bot_dir>          # Linux: the body of install-bot-systemd.sh; Darwin: the body of install-bot.sh (the two scripts become thin wrappers in PR B; in PR A the verb CALLS them)
svc_disenroll <bot_dir>       # Linux: systemctl --user disable --now + unit removal; Darwin: launchctl bootout + plist removal + the stop teardown (tmux kill-server, rm .tmux-env) — the property the plist cannot carry
```
Every verb resolves the OS through `detect_os`'s `_OS`; an unknown OS prints `unknown`/returns 2 and invokes nothing.
- [ ] **Step 2 (contract tests):** `tests/test_supervisor_adapter.sh`: a temp PATH with fake `systemctl` and fake `launchctl` that append argv to a log; run every verb under `_OS=Linux` and `_OS=Darwin`; assert the fake received the expected action and unit name, the printed state per canned fake output, and that `svc_kick` returns 2 on `_OS=Other` with no fake invoked.
- [ ] **Step 3 (ratchet):** `tests/test_supervisor_ratchet.py`: for every file under `lib/` (scripts and extensionless executables), count lines matching the binary regex, excluding `lib/supervisor.sh`; compare to `tests/supervisor_ratchet_allowlist.json` (`{"lib/keepalive.sh": 9, ...}` measured at this tip); fail with the file and the delta on any new file or any count that grew; pass on shrink and print the shrink. A second test asserts the allowlist file has no entry with count 0 (a retired file leaves the list).
- [ ] **Step 4:** Verify + harness directly. Commit: `feat(lib): the supervisor adapter — five verbs, two spellings, and a ratchet on every direct call`.

### Task 7: `MCP_TIMEOUT` reaches the session, docs, CHANGELOG

**Files:** `lib/start-bot.sh` (no change expected — `bot.conf` is sourced under `set -a`; verify the export reaches `exec claude`), `CLAUDE.md`, `documentation/environment-variables.md`, `documentation/fleet-yaml-schema.md` or the system.yaml documentation page (wherever `host.jobs` is documented — find it by grep, add `host.boot` beside it), `CHANGELOG.md`.

- [ ] **Step 1:** A test in `tests/test_boot_policy_conformance.py` that the composed `.tmux-env` (or whatever `start-bot.sh` writes for the pane, `lib/start-bot.sh:158`) would carry `MCP_TIMEOUT` — if `.tmux-env` is derived from `bot.conf` at start, assert on the `bot.conf` export form and add a harness check in Task 8 instead.
- [ ] **Step 2:** Docs: `CLAUDE.md` gains a `supervisor.sh` row in the `lib/` table and a `boot.py` / `supervision.py` line in the package structure; `environment-variables.md` documents `MCP_TIMEOUT` (composed, from `host.boot.mcp_timeout_ms`), the `BOOT_*` keys as composed-not-operator, and F4's precedence; the system.yaml page documents `host.boot`. `CHANGELOG.md` `[Unreleased]`: one bullet per task.
- [ ] **Step 3:** Commit: `docs: the boot policy, the supervisor adapter and the readiness ceiling`.

### Task 8: gauntlet and deploy

- [ ] Committed-code mutants (driver of the check-in series): at least one per task — slots derivation off-by-clamp, `ready_timeout_s` uncoupled, a `bot.conf` key dropped, the wall-clock ceiling back to iterations, the stamp ignored, the round-trip parser ignoring a section, a ratchet count that may grow, `svc_kick` naming the wrong unit on Darwin.
- [ ] The two-leg gate (names + counts, collected per file at both refs), the harness directly, CI on Linux, the final whole-branch review folded once.
- [ ] Deploy to the host: pull, `generate` both fleets, verify the six keys in every `bot.conf` once (a read-only grep), restart ONE leaf manager per fleet with the proven sequence, read the new session's real environment for `MCP_TIMEOUT` (`ps eww` on the pane pid, as the #1570 canary did), and read its startup log for the new `TIMEOUT`/`READY` wording. Deploy comment behind `no_names`.

## Test Plan

Unit: `test_boot_policy.py`, `test_boot_policy_conformance.py`, `test_supervision_roundtrip.py`, `test_supervisor_ratchet.py`. Bash suites through `test_sh_suites.py`: `test_bridge_readiness_wait.sh`, `test_plugin_update_once.sh`, `test_supervisor_adapter.sh`. The harness directly after Tasks 3, 4 and 6 (each touches a `lib/` script it exercises). The standing two-leg gate and CI.

## Verification Checklist

- [ ] `grep -c 'BOOT_ADMISSION_SLOTS=' <every composed bot.conf>` prints 1; the same for the other five keys.
- [ ] `grep -E 'BOOT_|MCP_TIMEOUT|RC_READY' <every composed unit and plist>` prints nothing.
- [ ] `tests/test_supervision_roundtrip.py` fails on the in-memory probe (shown, restored) and passes as committed.
- [ ] `tests/test_supervisor_ratchet.py` fails on one added `systemctl` call in a `lib/` script (shown, restored) and passes as committed.
- [ ] `tests/test_bridge_readiness_wait.sh` pins the wall-clock bound with 1 s probe cost.
- [ ] Harness: `=== 244 passed, 7 failed ===` with the seven names identical to main.
- [ ] Live: a restarted manager's session environment carries `MCP_TIMEOUT=180000`; its startup log carries the new `READY`/`TIMEOUT` wording.

## What NOT To Do

- Do not migrate any call site onto the adapter in this PR (PR B does the boot path; the rest follow under the ratchet).
- Do not remove the systemd stagger in this PR; the round-trip test allows it explicitly until PR B.
- Do not touch `library/`, fleet manifests or operator `.env` files.
- Do not change `bridge_state`'s semantics; only the loop around it moves.

## Context

area: supervision / compose · effort: L · risk: medium (runtime scripts change; behaviour change limited to the timeout, the ceiling and the plugin step) · priority: P1

Linear: neither.
