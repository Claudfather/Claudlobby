---
title: "P3 — summaries owned by clauDNA: the session-export job, `session_summary`, the Claudron pin, and two hooks retired (Claudlobby)"
type: plan
status: draft
owner: chrisrogers37
created: 2026-10-04
updated: 2026-10-05
epic: documentation/plans/2026-10-04-runtime-neutral-observability-plan.md
spec: documentation/plans/2026-08-18-observable-plane-design-v2.md
issue: "#2145"
repos: Claudfather/Claudlobby
---

# P3 — summaries owned by clauDNA: the session-export job, `session_summary`, the Claudron pin, and two hooks retired (Claudlobby)

> **Status:** draft, authored by `/claudna:forge` on 2026-10-04 from epic plan §6 P3 (Claudlobby bullets) and its two
> `#### Spec:` subsections (`the session_summary plane event`, `the clauDNA export contract additions`). Code references
> are to Claudlobby `cd292cb` (the checkout `2dd0aad5` is identical for every line cited). Depends on: P1 Claudlobby
> **Half A** (release N: `BotConfig.agent_cli`, composed `CLAUDLOBBY_RUNTIME`, `derive_session_uid(id, runtime)`, the
> design-v2 §1.1 amendment, Task 9b's `fleet_event_request`, and — if C10 leaked — Task 7b's conditional `start-bot.sh`
> scrub; nothing from Half B); the P3 clauDNA
> release (plan 5: `export --include-skipped`, the item's `segment` object, `session.runtime`,
> `<CLAUDNA_STATE_DIR>/entrypoint.json`, `lib/claudna/session_store/schemas/export.schema.json`); Claudron `v0.9.0`
> (`6ca2b94`, tagged) for PR 6a. Waits on canaries: none — this PR reads no `CLAUDE_*` variable and composes no Codex bot.
> Mission decisions: **D1** (F18 (a) — Claudlobby composes agent CLIs: Claude Code today, Codex through #2149) and
> **D2** (clauDNA's mission amendment), both ratified 2026-10-05
> ([F18 lock](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-6000050806),
> [D2](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-6000052137)); Half A and clauDNA 0.27 carry
> their mission texts, and nothing here waits on a ruling.

## Summary

Claudlobby stops writing session summaries and starts consuming clauDNA's: each bot gets its own clauDNA root
(`$BOT_DIR/data/claudna`, F14 — first, as §10 order 3's one-line PR, whose activation seals the old root's bot sessions
per F14(a)), a fleet timer `session-export` reads every bot's export door at the path clauDNA's own
hook records (F16) and turns each sealed segment — summarized or skipped — into one `session_summary` system event
shaped for the reader the four monitor consumers already use (F6), then acks. `transcript-digest.sh` and
`plane-session-start.sh` retire with every touchpoint, making `ids.derive_session_uid` the one derivation (F2). The two
model-spending clauDNA knobs become fleet-level opt-in switches composed to match clauDNA's summary gate, and — in its own
PR, 6a, after the rest — the Claudron install pin moves `v0.6.1 → v0.9.0` behind an operator-run vault migration and a
named `config plan` *warning* (the validator's warn-never-fail precedent for host-side state). Deliberately left out: Codex
composition (F11, companion) and P2's `session.tool_calls` samples (the consumers lose `tool_calls` until a PR grants
them the `plane samples` door; P2-a2, the plane leg, serves `usage_read`/`brief_read` first — A-F3).

## Evidence (Claudlobby at `cd292cb`)

- **Where clauDNA state lives today.** Nothing in Claudlobby sets `CLAUDNA_STATE_DIR` (repo grep outside plans: only
  `CLAUDNA_VERSION`, `claudlobby/composer.py:1319-1330`) or a per-bot `HOME` (`_runtime_scripts/start-bot.sh:96`
  `export HOME="$HOME"`), so clauDNA's `paths.state_root` (`lib/claudna/session_store/paths.py:51-59`: `$CLAUDNA_STATE_DIR`
  else `~/.claudna`; clauDNA `SETUP_GUIDE.md:317`) puts **every bot of the service user in one `~/.claudna`**. The
  `# Ecosystem` block (`composer.py:1319-1330`) is where the per-bot root belongs; `BOT_DIR=` is emitted at `:1010`
  (`paths.bot_runtime`, `:1007`; `claudlobby/paths.py:686`) and `bot.conf` is sourced under `set -a`
  (`start-bot.sh:223-229`), so a `"$BOT_DIR/…"` value expands at source time — the `TELEGRAM_STATE_DIR="$HOME/…"`
  shape at `:1041`, admitted by `path_audit.is_safe_anchored_path` (`claudlobby/path_audit.py:596-608`).
- **The seal step (clauDNA 0.26, `d1f70d4`).** `list [--bot <name|id>] [--json] --root R` (`cli.py:344-350`) prints rows,
  newest first, carrying `sid`, `status`, `kind` and `bot` — no fleet (`readers.py:78-89`) — **50 by default** (`--limit`)
  and private sessions only with `--include-private`; `--bot` matches the actor's `bot_name` or `bot_id` (`readers.py:71-72`),
  which a bot's session records with `kind: "bot"` from the `BOT_ID`/`BOT_NAME` its `bot.conf` exports (`boundaries.py:52-74`;
  `composer.py:1023-1024`), so it never returns an interactive session. `seal <sid> --root R` closes a session as abandoned
  with **no owner pid** — a live one too (`cli.py:506-512` → `boundaries.abandon_session` → `close_abandoned(owner_pid=None)`,
  `store.py:322-338`) — and hands the caller's environment to the summary gate (`boundaries.py:213-229`). `sweep [--dry-run]
  --root R` (`cli.py:341-343,415-450`) closes only sessions whose `claude` pid is gone and that sat idle past
  `CLAUDNA_UNCLOSED_AFTER_H` hours (floor 1 h, `unclosed.py:48-61`), at most `SWEEP_LIMIT = 5` per run, longest idle first
  (`:51`); it strips `CLAUDNA_SESSION_SUMMARY` before summarizing (`cli.py:422-424`), runs retention under the same lock
  (`:442`) and prints `{"closed": […], …}`. The store entrypoint is recorded only by the P3 clauDNA release
  (`entrypoint.json`, the export-contract spec), into the **new** root; the composer never learns it, so before 0.28 the
  installed plugin's cache directory is the only way to run the store (the F14 runbook's fallback, Task 7).
- **Fleet job, not host job.** `defaults.jobs` (`claudlobby/system.yaml:466-558`) composes per-fleet timers through
  `compose_fleet_timers` (`composer.py:4823-4871`) with the fleet name appended on `ExecStart` (`_write_timer_units`,
  `:4206-4207`); `task-recheck` (`system.yaml:535-538`) is a Python tick with no launcher —
  `$CLAUDLOBBY_CLI --root $CLAUDLOBBY_ROOT _task-recheck-tick`, registered at `commands/_parsers.py:66-68`, implemented
  by `commands/task_recheck.py:202-215` (`tick`: the `TASK_RECHECK_ENABLED=0` loud no-op, then `dispatch`). Host jobs
  pass **no** fleet (`system.yaml:55-57`; `_write_timer_units(fleet_name=None)`), and `FLEET_JOB_ARMING`
  (`composer.py:4790-4793,4961-4964`) stamps a fleet job's switch env onto its unit from `switches.jobs_with_env`
  (`switches.py:722-743`).
- **The event's reader.** `claudlobby event list --type X` → `plane-readers.py` `fleet_events` (`:1238-1279`) filters
  `e.source_ref LIKE 'fleet-events:%'` (`:1155-1164,1251`) and renders through `legacy_event_row` (`:1208-1229`), which
  takes `data` from the nested `detail.data` (`:1228`) and `bot` from `subject_alias` minus `bot:<fleet>/` (`:1219-1225`);
  `commands/events.py:182-186` is the item. `transcript-digest.sh` stamps `source_ref = session-digest:<sid>` with a flat
  `data` (`:337-342`), so today's rows never render (the F6 forge note).
- **Write spine and ids.** `emit_batch(root, raw_requests, *, conn_factory=None, require_commit=False, precondition=None)`
  (`claudlobby/plane/emit_api.py:149-153`); `derive_uid(prefix, material)` renders `f"{prefix}_{hex}"` (`plane/ids.py:57-60`),
  `ID_PATTERNS["event"] = ^ev_[0-9a-f]{32}$` (`:35`) — so the spec's `derive_uid("ev_", …)` must be spelled
  `derive_uid("ev", …)`; `SystemEvent.data` is DIAGNOSTIC, cap 16 384 (`plane/registries.py:72`); `session_digest` is
  registered at `:201-204` beside `tool_call`/`session_event` (`:196-197`); the registry rule is `:100-104`.
- **Registry gate.** `tests/test_event_type_registry.py` scans Python writers listed in `PY_WRITERS` (`:275-278`) by helper
  name and literal first argument (`:307-317,363-366`), trips on any module naming `"event_type": "system"` that no scan
  covers (`:392-399`, with `RS + "transcript-digest.sh"` as a marker control at `:396`), and uses `session_digest` as a
  shell-writer positive control (`:323`). Gate (d)'s `DOCS` (`:404-408`) are `fleet-observability.md`, `fleet-pulse/SKILL.md`,
  `guides/observability.md`, and only tables whose first header is in `TYPE_HEADERS` (`:409`) — **`fleet-monitoring.md`'s
  "Digest row contract" (header `Field`) is not bound by gate (d)**; the spec's sentence names the wrong test. What binds
  the protocol rewrite to the PR is `tests/test_no_retired_digest_reference.py` (`RETIRED`, `:41-45`) once
  `session_digest` joins its tokens, and `:323` once the shell writer is gone.
- **Retire touchpoints (grep-derived, excluding plans and the retiring files themselves):** `claudlobby/system.yaml:402-425`
  (digest `:402-417`, session-start `:418-425`); `system.yaml.example:430-453` (regenerated, recipe at
  `tests/test_fleet_mission.py:228-231`); `switches.py:529-540` (+ the comment at `:397`); `tests/test_switches.py:99,106,357-358,417,734-755,804,961`;
  `harness/validate-bot-change.sh:3576-3581`; `claudlobby/isolation.py:100-103` (docstring only — digest-only, yes);
  `tests/test_fleet_claude_bin.py:188`; `tests/test_plane_emit_class.py:414`; `tests/test_plane_gauntlet_doors.py:27` (+ the
  dangling comment `:148` — no test body reads `.plane-session`); `tests/test_event_type_registry.py:323,396`;
  `tests/test_heavy_slot_match.py:158` (an example string); `documentation/environment-variables.md:199` (+ `:189-195`);
  `fleet-update-lifecycle.md:422-428`; `testing-plane-isolation.md:82`; `system-yaml-schema.md:388-389,505-510`;
  `architecture/observable-plane.md:67-69,212,214`; root `CLAUDE.md:124,140`; `_runtime_scripts/CLAUDE.md:69,116`; both
  `AGENTS.md` are byte-identical copies today (`cmp` clean). `.plane-session`'s only reader is `transcript-digest.sh:256`.
  `tests/test_system_event_retention.py:74` keeps `session_digest` (a registered type nobody writes is its point).
  `architecture/system-map-2026-07-30.md` is a dated snapshot and stays (the rule at `test_no_retired_digest_reference.py:17-19`).
- **Switch mechanics.** `_carrier_lines` (`switches.py:206-241`): a `COMPOSE_BOT` row renders "`bots.<bot>.<config>: true`
  … ONE armed bot first … widen to `defaults.<config>`" from `config` (`:219-226`); `_bot_config_value` walks the dotted
  path and tests `is True` (`:853-860`); `_enroll_state` reports per-bot state for `COMPOSE_BOT` (`:872-884`). `defaults.env`
  is **not** merged (`config.py:1889`; `validator.py:1299-1301` relies on it), so a `BOT_CONF`/env row like `session-digest`
  can only be armed per bot. Mergeable mappings: `_coerce_observability`/`_merge_observability` (`config.py:1402-1424,1522-1552`),
  unknown-key refusal `_parse_isolation` (`:1682-1718`), arming knobs `_strict_bool` (`:1652-1663`), shell-boolean rule
  `composer.py:1201-1207`. The opt-in allowlist is `tests/test_switches.py:99-178`; the three doc tables are pinned at `:957-969`.
- **Claudron.** Pin `pyproject.toml:32`; comment `.github/workflows/conformance.yml:37`; doc headline/claims
  `documentation/integrations/claudron-integration.md:7,44,48` (`tests/test_claudron_compat.py:155-174` pins the headline to
  the pin; `:115-152` pins pin ≥ the highest live `COMPAT_FLOOR` release, today 0.4.0 at `claudron_compat.py:65-74`).
  `hooks.settings_snippet(executable, vault_root)` and `SNIPPET_EVENTS` are unchanged between v0.6.1 and v0.9.0
  (`claudron/hooks.py:235-242`; the diff adds only the ops log), so `tests/test_claudron_loop.py::TestSnippetParity`
  (`:452-500`) should pass unchanged. The bump crosses `m003`/vault format 3 (0.7.0, `CHANGELOG.md:62`; `claudron/vault.py:68`
  `VAULT_FORMAT = 3`) and 0.8.0's "run `claudron doctor --fix` if you haven't since 0.7.0" (`:14`); `doctor --fix` commits
  to the vault (`:67-68`). `claudron doctor --json` reports `data.pending`, `data.vault_format`, `data.engine_format`, and
  D001 for both a pending migration and an unrecorded format (`claudron/doctor.py:315-326`); D007/D008 are identity/ignore
  rules, D009/D010 hook checks (`:302,379-380`), and `check_structure` findings follow (`:328`). Claudlobby's consumer is
  `doctor._claudron_probe`/`_claudron_doctor` (`claudlobby/doctor.py:768-870`, timeout `CLAUDRON_DOCTOR_TIMEOUT_S = 60`,
  `:746`); `validator._validate_bots` probes host state once (`claudron_on_path`, `validator.py:642`) and already memoizes per
  vault (`vault_resolutions`, `:1346-1348`); `doctor` imports `validator` (`doctor.py:1373`), so the validator cannot import
  `doctor` — a shared prober must live below both.
- **clauDNA shapes the event maps from** (v0.26.0 + the P3 release): `claudna.segment-summary/1` has `input{transcript_path,
  range, sha256, turns}`, `producer{model, prompt_version, duration_ms, cost_usd}`, `journey{title, intent, outcome, arc, done,
  in_progress, next}`, `blocks[]`, `procedures[]`; `claudna.segment/2` has `sealed_at`, `sealed_by`, `counts{prompts, skills,
  failures, interrupts}`; the export item is `{sid, seg, session: SESSION_FIELDS subset, summary}` (`export.py:124-128`), plus
  P3's `segment{sealed_at, sealed_by, counts}` and `skipped{reason}` status items, P1's `session.runtime`. The cursor never
  moves back (`export.py:135-149`); retention caps at `CLAUDNA_RETAIN_DAYS` = 30 (`retention.py:41-42`).
- **clauDNA's summary gate** (`lib/claudna/session_store/project.py:276-287`, `summary_gate`): `CLAUDNA_SESSION_SUMMARY=0` →
  `disabled`; `=1` → summarize; else an actor of kind `headless`/`bot` → `headless`; else no harvest → `disabled`. So
  `CLAUDNA_HARVEST=1` alone never summarizes a bot's session, and a bot with the variable unset reads `headless`, not
  `disabled` — the composition in Task 1 is written against this order.
- **Warn, never fail, on host-side state** (`validator.py:1138-1152` git identity, `:1319-1331` claudron off PATH,
  `:2094-2103` `job-inert`): every host probe the validator makes is a named `shared.add`/`report.warn` category
  (`tests/test_validate_warning_discipline.py`). Claudron's engine does not guard either: `pending_migrations` is called
  only by `claudron/doctor.py:315-326`, and `VAULT_FORMAT` (`claudron/vault.py:68`) is read only to write the identity
  file — D001 is advisory, and `recall`/`capture`/hooks run against an unmigrated vault.
- **Ingest refuses a mixed batch.** `_verify_duplicates` (`plane/ingest.py:598-605`) raises `RuntimeError("…mixed state")`
  when a colliding batch holds an id with no ledger row; `emit_batch` re-raises `ContractViolation` and every non-retryable
  error (`plane/emit_api.py:273-280`). A multi-item batch with one new segment and one re-emitted one is exactly that
  collision — the job in Task 4 never builds one.

## Implementation Plan

### Dependencies

P1 Claudlobby **Half A** merged (release N: `derive_session_uid(id, runtime)`, `CLAUDLOBBY_RUNTIME`, Task 9b's
`fleet_event_request` that `_system_event` calls, and — if C10 leaked — Task 7b's conditional `start-bot.sh` scrub; nothing
here waits on Half B; Half A carries the D1 mission text and clauDNA 0.27 the D2 one, both ratified 2026-10-05 —
[F18 lock](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-6000050806),
[D2](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-6000052137)); clauDNA P3 released (0.28.0, this plan's floor,
`MIN_PLUGIN_VERSION`) **after §10 order 3's per-bot root is active on every bot** (Task 1 Steps 1–2; X21): `claudna_version`
exports `CLAUDNA_VERSION` and nothing pins the installed plugin today (`plugin_ensure` runs `claude plugin update` on every
bot start unless `BOOT_PLUGIN_UPDATE_ONCE=1`, `start-bot.sh:315-321` → `lib-common.sh:4693-4710`), so 0.28 reaches every
bot at its next restart, ahead of 6b — the guard is the order-3 per-bot root: an early 0.28 writes `entrypoint.json` into
the bot's own root and, with summaries unarmed, the bot's summary gate returns `headless` (no spend); Claudron `v0.9.0`
installed on the canary host for PR 6a (`pip install -e '.[dev,vault]'` for the parity leg). **P2 is not a dependency** —
P2 ∥ P3 (epic §10); this plan lands beside plan 4 in parallel. Both edit `switches.py`, `system.yaml`, `plane/registries.py`,
`tests/test_event_type_registry.py` and the three switch tables, so: whichever of P2-b, P2-a1 and P2-a2 merged first, this PR rebases
onto it, regenerates the three switch tables and `system.yaml.example`, re-copies both `AGENTS.md`, and re-runs
`tests/test_instruction_budget.py`, `tests/test_switches.py`, `tests/test_event_type_registry.py` before pushing (X12).
Order within P3: plan 5 (clauDNA) releases before Task 4 here can observe anything real.

### Blocks

The companion Codex epic (per-bot state dir and a runtime-neutral export consumer are prerequisites). P2: no ordering —
plan 4 *informs* this plan's consumer note (P2-a2, the plane leg, writes `session.tool_calls` samples; they reach the
monitor only through a later `plane samples` grant to `fleet-digest`), and this plan informs nothing in P2 (X12).

### Steps

Tasks follow as H3 siblings. Line numbers are pre-dependency: P1 Claudlobby Half A (plan 2) edits `config.py`,
`composer.py`, `plane/contracts.py`, `plane/ids.py` and `tests/test_event_type_registry.py`, and adds
`plane/fleet_events.py`, before this PR opens — edit bottom-up or re-grep each anchor at PR-open;
`tests/test_switches.py`'s opt-in allowlist is `:99-178` (X15). Seams: Task 1's Steps 1–2 are §10 order 3's own one-line
PR and its activation, ahead of everything else here (it needs nothing from P1). Task 3 (the Claudron pin) is its own PR,
**6a**, after the rest (**6b** = Task 1 Steps 3–11, Tasks 2, 4–7, 7c and 8): the bump is an operator-run vault migration
with a per-host runbook and its own canary and gate (Task 3 Step 6), and nothing in 6b calls what v0.9.0 adds —
`claudna.harvest` is documented, and warned (`claudna-harvest-pin`, Task 1 Step 9), as requiring 6a on the host before a
bot arms it (B3). Task 7 is F14(a)'s runbook document — the old root's rename and removal; the seal itself runs at order 3,
Task 1 Step 2 — and 6b carries it; the operator runs it after 6b, on the schedule Task 7 gives.

### Task 0: worktree, before-leg, evidence

- [ ] Branch `p3/claudlobby-summaries` from `origin/main` **after** P1 Claudlobby Half A merged. Before-leg per
  `documentation/test-suite.md` (`:32`: the macOS suite is not green — compare **names and counts** across two separately
  prepared legs, unsandboxed; root `CLAUDE.md:254,325`): `./.venv/bin/pytest --tb=no -ra > <evidence>/run_before.txt 2>&1;
  echo $?`, then `bash harness/validate-bot-change.sh` and record its pass/fail pair by name. Evidence dir outside the repo.
- [ ] **The other two PRs get their own.** §10 order 3 (Task 1 Steps 1–2): its own branch from `origin/main` — it needs
  nothing from P1 — and the same before-leg. **6a** (Task 3): branch `p3/claudron-pin` from `origin/main` **after** 6b
  merged, with its own before-leg (the same commands, its own evidence files); Task 3 Step 6's gate compares against it,
  never against 6b's.

### Task 1: per-bot `CLAUDNA_STATE_DIR`, and the `claudna:` mapping composed into `bot.conf`

The per-bot root ships now — clauDNA's spec §1.1 rule 3 (`2026-09-28-session-store-design.md:34`) already says per bot.
A-F14 lapsed 2026-10-05 ([lock comment](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-6000052137));
F14(a) stands: order 3's activation seals the old root's bot sessions (Step 2), and Task 7's runbook renames, then
removes, the old root.

**Two PRs, one task.** The `CLAUDNA_STATE_DIR` line is §10 order 3's own one-line PR, before clauDNA 0.27 merges (epic
§9; §10.1: it needs no plan file, and this task is its spec): Step 1 is that PR and Step 2 its activation — F14(a)'s seal
of the old root, carried out at the switch, as the fork decides. 6b carries Steps 3–11, and Step 3's first assertion then
verifies the line; if order 3 did not land first, 6b carries Step 1 and runs Step 2 at its own activation. Steps 4–6 are
the **canonical carrier** of P2's strict-mapping helpers (plan 4 Task 3 cites them), written for 6b landing first: the
registry's literal is then P3-first and P2-a1 registers `telemetry` in one line. P2-a1 and 6b run in parallel (epic §10);
if P2-a1 lands first instead, it carries Steps 4–6 verbatim, with its own mapping in the cells and the literal (X13).

**Files:** order 3 — `claudlobby/composer.py`, `tests/test_composer.py`, `CHANGELOG.md`. 6b — `claudlobby/config.py`,
`claudlobby/composer.py`, `claudlobby/commands/config_explain.py`, `claudlobby/validator.py`, `tests/test_composer.py`,
`tests/test_config.py` (or the file that holds `_coerce_observability`'s tests), `tests/test_env_register.py` (`config
explain`'s cells live there; there is no `tests/test_config_explain*.py`), `tests/test_validator.py`,
`tests/test_validate_warning_discipline.py`, `documentation/fleet-yaml-schema.md`, `fleet.yaml.example`,
`documentation/environment-variables.md`.

- [ ] **Step 1 — the order-3 PR: the line.** `compose_bot_conf` (`composer.py:1319-1330`): the `# Ecosystem` header
  becomes unconditional, followed by `export CLAUDNA_STATE_DIR="$BOT_DIR/data/claudna"` (why: the per-bot root is a
  composition invariant, not a knob — a shared `~/.claudna` strands sessions, F14), then the existing three optional lines.
  Test first, in `tests/test_composer.py`: every composed `bot.conf` carries exactly one such line, **after** the
  `BOT_DIR=` line (`:1010`), unconditionally (a bot with no `claudna_version` too); existing tests pinning full `bot.conf`
  text gain it; `tests/test_freshbox_selfcontained.py` stays green (the value is `$BOT_DIR`-anchored). One CHANGELOG line,
  naming the activation below and that a `CLAUDNA_STATE_DIR` set in a `.env` tier is now overridden by the composed line
  (`start-bot.sh:219-229` sources `bot.conf` after the tiers). Verify: `./.venv/bin/pytest tests/test_composer.py
  tests/test_freshbox_selfcontained.py -q`. Commit: `feat(compose): each bot gets its own clauDNA root,
  CLAUDNA_STATE_DIR="$BOT_DIR/data/claudna" (#2145 F14)`.
- [ ] **Step 2 — the order-3 PR's validation and activation: the canary-root check and F14(a)'s seal
  (operator-run).** `E` is the installed plugin's store entrypoint. No `entrypoint.json` exists before 0.28, so `E` is the
  plugin-cache path the F14 runbook names as its fallback (Task 7):
  `~/.claude/plugins/cache/Claudfather/claudna/<version>/lib/claudna/session_store` (under the bot's `CLAUDE_CONFIG_DIR`
  when it has one, `composer.py:1046`), 0.26 or later (`list --bot`, `seal` and `sweep` with `--root` are 0.26's —
  Evidence). Run with `CLAUDNA_SESSION_SUMMARY` **unset** in the operator's shell: `seal` hands that environment to the
  summary gate, and unset, a bot session gates `headless` — the seal costs no model spend (`project.py:276-287`).
  (i) **Before merge, the canary root** (mandatory runtime validation, `CLAUDE.md:262`;
  `documentation/validating-bot-changes.md:35-50`): after the canary bot restarts under the new composition, show
  `$BOT_DIR/data/claudna/sessions/<sid>/` written by its new session and nothing new for that bot under `~/.claudna`
  (`python3 -S "$E" list --bot <b> --json --since 1h --root ~/.claudna` lists no session opened after the restart); cite
  both in the PR body. After merge, the fleet's activation runs the same check per bot as `host activate` restarts it.
  (ii) **Seal the old root's open bot sessions,** per bot `<b>` once it has restarted onto its own root. A clean
  restart closes the old session itself (its SessionEnd writes `session.closed`, `close_reason: other`), so this usually
  finds nothing; it is for sessions a crash, kill or hard reboot left open. Run it **after** the restart, never before:
  sealing a session whose `claude` is still running labels a clean close `abandoned` (observed on the canary, #2172):
  `python3 -S "$E" list --bot <b> --json --limit 100000 --include-private --root ~/.claudna | jq -r '.[] |
  select(.status == "open") | .sid'`, then `python3 -S "$E" seal <sid> --root ~/.claudna` for each. **Never** `seal` for
  "every open session": `seal` passes no owner pid, so it would close the service user's live interactive sessions in the
  same root (`cli.py:506-512`, `store.py:322-338`). `--limit` lifts `list`'s default of 50, which would hide the oldest
  strays; `--include-private` keeps a private bot session from stranding. Where two fleets on the host name a bot alike,
  seal it only after both have switched — `list --bot` matches a name or an id, never a fleet.
  (iii) **Sweep the stragglers** (a session with no bot actor, a bot since removed, a `claude -p` run): at least an hour
  after the last bot restart, `CLAUDNA_UNCLOSED_AFTER_H=1 python3 -S "$E" sweep --root ~/.claudna`, repeated until its
  `closed` list is empty — it closes at most 5 per run, and only sessions whose `claude` is gone and that sat idle past the
  hour, so a live session of either kind is never touched (Evidence).
  F14(a)'s rest — leave the old root until its retention window passes, then remove it — is Task 7's runbook, which 6b
  carries and the operator runs after 6b.
- [ ] **Step 3 (tests first) — 6b.** `tests/test_composer.py`: Step 1's line is in place (this assertion verifies it,
  X21), and every `bot.conf` carries exactly one `export CLAUDNA_SESSION_SUMMARY=` line:
  `claudna: {session_summary: true}` composes `=1`; `false` **and unset** compose `=0` (the gate then answers
  `disabled`, `project.py:276-287` — never `headless`, which is what an unset variable yields for a bot);
  `claudna: {harvest: true}` composes `export CLAUDNA_HARVEST=1` **and** `CLAUDNA_SESSION_SUMMARY=1` (the gate never
  summarizes a bot on harvest alone); `harvest: true` with `session_summary: false` is a `ValueError` at load naming both
  keys; `defaults.claudna.harvest: true` reaches every bot and a bot's own `false` wins (field-wise); a string `"true"` is
  a `ValueError` naming the key (`_strict_bool`); an unknown key under `claudna:` is refused naming the accepted keys.
  Existing tests pinning full `bot.conf` text gain the new lines. `tests/test_validator.py`, Step 9's warnings: a fleet- or
  bot-tier `.env` assigning `CLAUDNA_SESSION_SUMMARY=1` → one `claudna-env` warning naming the tier and `claudna:`;
  `bots.<b>.env: {CLAUDNA_HARVEST: "1"}` → the same, naming the bot; the same key in the operator's own `os.environ` →
  nothing; a bot arming `claudna.harvest` → one `claudna-harvest-pin` warning; neither is ever an error.
  `tests/test_validate_warning_discipline.py`: both categories are named at their raise sites and fold per cause.
- [ ] **Step 4 (tests first) — the strict-mapping helper's own cells (canonical here).** Plan 4's cells (j) and (k),
  carried here: in `tests/test_config.py`, loading fleets through `load_fleet` the way `tests/test_host_override.py` builds
  roots, `scalar_config_origin(..., "claudna.harvest", bot=...)` returns `("bot", "fleet.bots.<b>.claudna.harvest")`,
  `("fleet.defaults", "fleet.defaults.claudna.harvest")` and `("built_in", None)` for the three sources, and `"claudna"`
  whole still raises `NotImplementedError`; `_parse_strict_mapping` parametrized over `_STRICT_MAPPING_FIELDS` — every
  registered mapping refuses an unknown key by name (P2-a1's `telemetry` joins the cell when it registers). In
  `tests/test_env_register.py`, beside the `config explain` cells (`:135-179`): `config explain bot.claudna.harvest --bot B`
  → (`fleet.defaults`, `fleet.defaults.claudna.harvest`) when set under `defaults:`; `config explain bot.claudna --bot B` →
  `claudna is a mapping — name a sub-field: claudna.<key>`; an unknown head (`bot.nonesuch.x`) → `configuration key is not
  a declared model field`.
- [ ] **Step 5 — the helpers (plan 4 Task 3 Step 2's bodies, carried here).** `claudlobby/config.py`, beside
  `IsolationConfig` (`:658`) — **one** strict-mapping parser and **one** field-wise merger for every strict sub-mapping on
  `BotConfig`; the second mapping to land registers in one line, never a second copy, and there are no
  `_parse_claudna`/`_merge_claudna` wrappers:

```python
def _parse_strict_mapping(raw: Any, where: str, fields: dict[str, Callable[[str, Any], Any]]) -> dict:
    """One tier's strict mapping → the fields it SETS (absent keys stay absent so the merge inherits
    them; `null` unsets, so `harvest: null` inherits). Unknown keys are refused by name."""
    if raw is None:
        return {}
    raw = _shaped(f"'{where}'", raw, dict, "{" + next(iter(fields)) + ": …}")
    unknown = sorted(str(k) for k in raw if k not in fields)
    if unknown:
        raise ValueError(f"'{where}': unknown key(s) {', '.join(unknown)} — accepted: {', '.join(fields)}")
    return {k: parse(f"{where}.{k}", raw[k]) for k, parse in fields.items() if raw.get(k) is not None}

def _merge_fieldwise(cls, defaults_raw: Any, bot_raw: Any, name: str, key: str, fields: dict):
    """Field-wise: bot over defaults over built-in (observability's precedence, isolation's strictness)."""
    return cls(**{**_parse_strict_mapping(defaults_raw, f"defaults.{key}", fields),
                  **_parse_strict_mapping(bot_raw, f"bots.{name}.{key}", fields)})

#: BotConfig field → its strict sub-mapping's parsers. The ONE registry `scalar_config_origin` and
#: `config_explain._config_field` read (one provenance rule, one error text). The literal is the first
#: lander's — P3's here, placed after `_CLAUDNA_FIELDS` (Step 7); P2-a1 adds `"telemetry": _TELEMETRY_FIELDS`.
_STRICT_MAPPING_FIELDS: dict[str, dict] = {"claudna": _CLAUDNA_FIELDS}
```

- [ ] **Step 6 — provenance (plan 4 Task 3 Step 3, carried here).** `scalar_config_origin` (`config.py:2271-2296`;
  `NotImplementedError` for mappings today): before the final `raise`, when `field` is `<head>.<sub>` with `<head>` in
  `_STRICT_MAPPING_FIELDS` and `<sub>` among its keys, answer from the raw tiers — `("bot", f"fleet.bots.{bot}.{field}")`
  if the bot's mapping sets `<sub>`, else `("fleet.defaults", f"fleet.defaults.{field}")` if `raw_fleet["defaults"][head]`
  does, else `("built_in", None)`. `config_explain._config_field` (`commands/config_explain.py:10-35`) accepts one dotted
  level when the head is a `BotConfig` field (`field.split(".", 1)[0] in model.__dataclass_fields__`), splits `:32`'s one
  message in two — `configuration key is not a declared model field` for an unknown head, `<head> is a mapping — name a
  sub-field: <head>.<key>` when a registered strict mapping is named whole — and the `effective` read at `:70` walks the
  path with `functools.reduce(getattr, field.split("."), obj)`.
- [ ] **Step 7 — `ClaudnaConfig`.** `claudlobby/config.py`, beside the helpers:

```python
@dataclass(frozen=True)
class ClaudnaConfig:
    """clauDNA's two model-spending knobs, composed per bot (#2145 P3). None = unset: clauDNA's default (off for bots)."""
    session_summary: bool | None = None   # CLAUDNA_SESSION_SUMMARY — one Haiku call per sealed segment
    harvest: bool | None = None           # CLAUDNA_HARVEST — summaries + draft notes into the bot's vault via `claudron capture`

_CLAUDNA_FIELDS = {
    "session_summary": lambda where, v: _strict_bool(f"'{where}'", v),
    "harvest": lambda where, v: _strict_bool(f"'{where}'", v),
}
# Step 5's registry literal sits here, after the parsers it names: {"claudna": _CLAUDNA_FIELDS} (P2-a1 adds its one line).
def _check_claudna(cfg: ClaudnaConfig, where: str) -> ClaudnaConfig: ...   # the post-merge cross-field rule: harvest is True and session_summary is False → ValueError(f"'{where}': claudna.harvest: true needs session_summary unset or true — clauDNA's gate never summarizes a bot on harvest alone")
# BotConfig (after claudron_session_loop, :776):
    claudna: ClaudnaConfig = field(default_factory=ClaudnaConfig)
# _coerce_bot (beside observability, :1884-1887) — Step 5's field-wise merge (unknown keys refused by name, bot over
# defaults over built-in), then the cross-field check on the MERGED value, where it is known:
    claudna=_check_claudna(_merge_fieldwise(ClaudnaConfig, defaults.get("claudna"), raw.get("claudna"), name, "claudna", _CLAUDNA_FIELDS), f"bots.{name}"),
```

  `claudna` registers `session_summary` and `harvest` (Step 5's literal), so `config explain bot.claudna.harvest --bot B`
  names its source; `_check_claudna` stays this plan's own — the cross-field rule a generic merger cannot express, applied
  to the merged `ClaudnaConfig`. Shape, decided and shown once in `fleet-yaml-schema.md`: the **nested** `claudna:
  {session_summary, harvest}` mapping holds what clauDNA reads at session open; `claudna_version` stays the flat field it
  is (`:127`) — it exports `CLAUDNA_VERSION`, and nothing pins the installed plugin today (`plugin_ensure` runs `claude
  plugin update`, `lib-common.sh:4693-4710`) — no alias; Claudosseum's `CLAUDNA_TELEMETRY` stays an operator `env:` knob
  outside the mapping, and clauDNA's docs that said Claudlobby sets it are corrected in the P3 clauDNA plan (its Task 6).

- [ ] **Step 8 — compose.** `compose_bot_conf`, after Step 1's line and the existing three optional lines — **always** —
  `export CLAUDNA_SESSION_SUMMARY={'1' if (session_summary or harvest) else '0'}` (unset is `0`, so an unarmed bot's
  segments export as `skipped: {reason: disabled}`), and `export CLAUDNA_HARVEST={'1' if harvest else '0'}` when `harvest`
  is not `None` (the shell-boolean rule, `:1201-1207`). Comment the block with the spec reference, the gate order
  (`project.py:276-287`) and that clauDNA reads these at session open.
- [ ] **Step 9 — two validator warnings (warn-never-fail).** `validator._validate_bots`, per bot, each category registered
  in `WARNING_CATEGORIES` (`validator.py:152`) and named at its raise site. **`claudna-env`:** a `.env` tier assigns
  `CLAUDNA_STATE_DIR`, `CLAUDNA_SESSION_SUMMARY` or `CLAUDNA_HARVEST` — read through `env_tiers.resolve`
  (`env_tiers.py:239-243`, the `env-tiers.sh` door `start-bot.sh:213-214` consumes; never `os.environ`, the operator's own
  shell) — and the composed line, sourced after the tiers (`start-bot.sh:219-229`), silently overrides it; or
  `bots.<b>.env` assigns one, which is emitted after the composed lines (`composer.py:1366-1370`) and silently beats the
  mapping, so the switch tables misreport the bot. Either way the finding names `claudna:` as the one knob — `report.warn`
  for the bot's own tier or `env:`, `shared.add` for a fleet or host tier, as the required-env check folds (`:880-901`).
  **`claudna-harvest-pin`:** a bot arms `claudna.harvest` while the install pin is Claudron `v0.6.1` (6b ships before 6a;
  harvest calls `capture --stdin`/`amend`, which need ≥ 0.8.0 — Task 3's compat row): "arm it once PR 6a is on the host".
  Task 3 deletes this warning with the bump.
- [ ] **Step 10 (docs).** `fleet-yaml-schema.md`: the shape block (`:127-130`) gains `claudna: { session_summary: true|false,
  harvest: true|false }  # OPTIONAL — STRICT bools; see bots.<name>.claudna`; a new `### bots.<name>.claudna /
  fleet.defaults.claudna` section in the paired-heading form of `heavy_slot` (`:547-564`) after `:928`, naming the two env
  vars, that `CLAUDNA_STATE_DIR` and `CLAUDNA_SESSION_SUMMARY` are always composed, which reason an unarmed bot's segments
  export with (`disabled`; `harvest: true` implies summaries), the spend, the "Claudron ≥ 0.8.0 on the host first (PR 6a)"
  line for `harvest`, that a `.env` tier or `env:` assigning the three keys warns `claudna-env` (Step 9), that
  `claudna_version` is a separate field (it exports `CLAUDNA_VERSION` and pins nothing), and the canary paragraph.
  `fleet.yaml.example:333-337` gains a commented `claudna:` block. `environment-variables.md` Ecosystem table (`:181-185`) gains three rows:
  `CLAUDNA_STATE_DIR` (source: *composed, always* — `$BOT_DIR/data/claudna`), `CLAUDNA_SESSION_SUMMARY` (source: *composed,
  always* — `0` unless `bots.<name>.claudna.session_summary`/`harvest` or the `defaults.claudna.*` twin arms it) and
  `CLAUDNA_HARVEST` (source: `bots.<name>.claudna.harvest` / `defaults.claudna.harvest`).
- [ ] **Step 11.** Verify: `./.venv/bin/pytest tests/test_composer.py tests/test_config.py tests/test_env_register.py
  tests/test_validator.py tests/test_validate_warning_discipline.py tests/test_freshbox_selfcontained.py -q`.
  Commit: `feat(compose): the claudna knobs composed from a strict fleet-level mapping — one strict-mapping helper with
  provenance, and env-override warnings (#2145 P3)`.

### Task 2: the two opt-in switch rows and the `session-export` opt-out row

**Files:** `claudlobby/switches.py`, `tests/test_switches.py`, the three generated tables (`documentation/fleet-yaml-schema.md:238-261`,
`system-yaml-schema.md:141-174`, `architecture/observable-plane.md:386-402`).

- [ ] **Step 1 (tests first).** `tests/test_switches.py:99-178` allowlist gains `"claudna-session-summary"` and
  `"claudna-harvest"` with the comment `# model spend — the retired digest hook's class, which these replace`; a new test asserts both
  are `DOOR`/`OPT_IN`/`COMPOSE_BOT` with `config` under `claudna.`, and that `resolve()` reports `fleet.yaml claudna.harvest —
  1 of 2 bot(s)` for a fleet with one armed bot (`_enroll_state`, `:872-884`); `session-export` is `FLEET_JOB`/`OPT_OUT`/
  `ENV_FLEET` with `env="SESSION_EXPORT_ENABLED"` and `job="session-export"` (the `task-recheck` shape, `:249-260`), so
  `jobs_with_env(FLEET_JOB)` carries it (`:722-743`).
- [ ] **Step 2.** Rows, in the opt-in block (`:432+`) and the fleet-job block:

```python
    Switch(key="claudna-session-summary", scope=DOOR, polarity=OPT_IN, carrier=COMPOSE_BOT, config="claudna.session_summary",
           why_opt_in="model spend (one Haiku call per sealed segment, in the background of the bot's session)",
           what="let clauDNA summarize each sealed segment of this bot's sessions; the session-export job then carries the "
                "journey into the plane's session_summary events (#2145 F6)",
           compose_steps="config plan, config diff PLAN_ID, host activate PLAN_ID",
           takes_effect="recorded when the bot's next session opens: bot.conf is read at session start",
           takes_effect_off="no summary from the bot's next session on; sealed segments then export as skipped: {reason: disabled}"),
    Switch(key="claudna-harvest", scope=DOOR, polarity=OPT_IN, carrier=COMPOSE_BOT, config="claudna.harvest",
           why_opt_in="model spend (summaries) and writes draft notes into the bot's Claudron vault through claudron capture",
           what="opt this bot's sessions into clauDNA harvest: summaries on (the composer writes CLAUDNA_SESSION_SUMMARY=1 "
                "beside CLAUDNA_HARVEST=1 — clauDNA's gate never summarizes a bot on harvest alone), typed blocks filed as "
                "(unverified) drafts in the vault CLAUDRON_VAULT_PATH names; needs Claudron >= 0.8.0 on the host (PR 6a)",
           compose_steps=…, takes_effect=…, takes_effect_off=…),
    Switch(key="session-export", scope=FLEET_JOB, polarity=OPT_OUT, carrier=ENV_FLEET, env="SESSION_EXPORT_ENABLED",
           job="session-export", plane=True,
           what="every 15 min, read each bot's clauDNA export door and record one session_summary plane event per sealed "
                "segment (summarized or skipped), then ack — the monitor's substrate (#2145 F6)"),
```

- [ ] **Step 3.** Regenerate the three tables: `claudlobby host doctor --switches --markdown` (pinned by `:957-969`).
  Verify: `./.venv/bin/pytest tests/test_switches.py -q`. Commit: `feat(switches): claudna-session-summary and claudna-harvest
  opt in per bot; session-export ships on with a loud off`.

### Task 3 (PR 6a, after 6b): the Claudron pin `v0.6.1 → v0.9.0`, the compat row that forces it, and the migration warning

**Files:** `pyproject.toml`, `.github/workflows/conformance.yml`, `claudlobby/claudron_compat.py`, `claudlobby/doctor.py`,
`claudlobby/validator.py`, `tests/test_claudron_compat.py`, `tests/test_claudron_loop.py`, `tests/test_validator*.py`,
`documentation/integrations/claudron-integration.md`, `CHANGELOG.md` (6a's own entry).

- [ ] **Step 1 (tests first).** `tests/test_claudron_compat.py`: with the new floor row, `test_vault_pin_satisfies_compat_floor`
  (`:115-152`) **fails at v0.6.1** (floor 0.8.0) and passes at v0.9.0; `test_integration_doc_version_claim_matches_pin`
  (`:155-174`) passes only once `:7` says `v0.9.0`; the doc-sync test sees the new table row. `tests/test_claudron_loop.py::TestValidator`
  (`:412-443`) gains, with a fake `claudron` on PATH that answers `status --json` (capabilities incl. `doctor`) and
  `doctor --json`: pending `m003` → one `claudron-migration` **warning** (never an error) naming the vault and
  `claudron doctor --fix --json --vault <vault>`; `vault_format 2 < engine_format 3` with nothing pending → the same warning;
  only D007/D008/D009/D010 and structure findings → **nothing**; `doctor` not in capabilities, exit 3, or the timeout → a
  `claudron-doctor` warning naming why; `claudron` off PATH → no probe at all (`claudron-path` already warns, `:1319-1331`);
  two bots on one vault → one probe per `validate()` call; a monkeypatched probe is honoured on the next call (no module-level
  cache). `tests/test_validate_warning_discipline.py`: both categories are named at their raise site and fold per cause.
- [ ] **Step 2 — the row and the shared prober.** `claudron_compat.py` gains a fifth `COMPAT_FLOOR` row:
  `ClaudronCapability(feature="clauDNA harvest from a composed bot (CLAUDNA_HARVEST=1 → claudron capture --stdin / amend)",
  requires="capture --stdin + amend --stdin with expect_trust (subject-filing), memory homes", default_order_release="0.8.0",
  probe=PROBE_VERB_PREFIX + "amend")` — why: Task 1 lets the composer arm harvest, so the install-time pin must provide what
  harvest calls (clauDNA's door is `claudron capture/amend`, epic §6 P3). Move `CLAUDRON_DOCTOR_TIMEOUT_S`, `_run`,
  `_claudron_probe` and the envelope parsing of `_claudron_doctor` (`doctor.py:746-866`) into one
  `claudron_compat.vault_migration_state(vault: str) -> VaultMigrationState(status: "ok"|"pending"|"old_format"|"unknown",
  pending: tuple[tuple[str, str], ...], vault_format, engine_format, detail: str)`, memoized **per `validate()` call** in the
  `vault_resolutions` shape (`validator.py:1346-1348`; a module-level `lru_cache` would outlive a test's monkeypatch), gated
  on `claudron_on_path` (`:642`), budgeted at `CLAUDRON_DOCTOR_TIMEOUT_S` (60 s, shared with doctor — one probe per distinct
  vault, so a fleet pays vaults × ≤ 60 s only when the engine hangs); `doctor._claudron_doctor` renders its rows from it
  (same text as today). One prober, two readers.
- [ ] **Step 3 — the warning (warn-never-fail, B3).** `validator._validate_bots`, after the vault block (`:1358`), for each bot
  with `composer._session_loop_enabled(bot)` and a resolving vault, with `claudron` on PATH: `state = vault_migration_state(vault)`;
  `pending`/`old_format` → `shared.add("claudron-migration", f"claudron_vault_path '{…}' has {n} pending migration(s) ({ids}) /
  records vault format {v} but the engine is at {e} — the session loop would run hooks on an unmigrated vault. Once per vault,
  from one clone, a human runs `claudron doctor --fix --json --vault <vault>` (it commits), then pulls on every clone
  (vault-sync), then plans again.", bot_name)`; `unknown` → `shared.add("claudron-doctor", …)` naming why. Why a warning and
  not the epic bullet's "refuses": every host-side probe the validator makes warns by stated intent (`:1138-1152`,
  `:1319-1331`, `job-inert` `:2094-2103`; #2001 made `doctor` surface migrations and "never --fix"), and Claudron's engine
  itself does not refuse on pending migrations (Evidence: D001 is advisory), so a plan-time error would be the stack's only
  hard stop on a state the engine tolerates — the `doctor` rung (`check_claudron`, `doctor.py:968`) and
  `config validate --warn-baseline` are where it bites. Nothing for D007–D010 or structure codes (the engine's and the human's).
  The same `validate()` backs `config plan` (`config_staging.py:174-176`), `config validate` and `doctor` — intended; and
  `config validate --strict` turns the warning into the refusal an operator who wants one asks for.
- [ ] **Step 4 — the bump.** `pyproject.toml:32` → `@v0.9.0`; `conformance.yml:37` comment; `claudron-integration.md:7`
  headline `(at v0.9.0)`, `:11` gains "`claudlobby config plan` warns (`claudron-migration`) on a session-loop bot whose vault
  has a pending migration or an unrecorded format (D001) and names the fix; every other finding stays doctor's", `:44` "the
  pinned **v0.9.0** ships the write-lock", `:48` `@v0.9.0 today`, the floor table (`:56-61`) gains the row; a new **per-host
  runbook** paragraph under "Version pin and bump policy": the 0.6.1→0.9.0 jump crosses `m003`/vault format 3 and the 0.8.0
  index rebuild; in order — (1) upgrade the install on the host (`pip install -e .` picks up the pin); (2) from **one** clone,
  `claudron doctor --fix --json --vault <vault>` (it commits); (3) `git pull` on every other clone (vault-sync does it on its
  next run); (4) `claudlobby config plan` — the `claudron-migration` warning is gone; canary one host first. Also the "arm
  `claudna.harvest` only after this PR is on the host" line (6b documents the same from its side), and the bump deletes 6b's
  `claudna-harvest-pin` warning (Task 1 Step 9) with its `WARNING_CATEGORIES` entry and test — the pin now provides what
  harvest calls.
- [ ] **Step 5.** Verify: `./.venv/bin/pytest tests/test_claudron_compat.py tests/test_claudron_loop.py tests/test_doctor*.py
  tests/test_validate_warning_discipline.py -q`; with `pip install -e '.[dev,vault]'`: `pytest -q -m "vault and not quarantine"`
  (`TestSnippetParity`, `:452-500`, unchanged snippet shape). `CHANGELOG.md` `[Unreleased]`: one `### Changed — …` for the
  pin, in the shape of the v0.6.1 pin entry (`CHANGELOG.md:314-316`; 6b's entries are Task 7c's). Commit (PR 6a):
  `chore(claudron): pin v0.9.0, a compat row for clauDNA harvest, and config plan warns on a session loop over an unmigrated
  vault`.
- [ ] **Step 6 — 6a's canary and gate** (6a is its own PR, so Task 8's gate is not its gate). **Canary root** (mandatory
  runtime validation, `CLAUDE.md:262`): one host with Claudron v0.9.0 installed and the vault migrated per Step 4's
  runbook; `config plan` on the canary fleet is silent with the migrated vault and warns `claudron-migration` (with the
  named fix) against a scratch clone left at format 2; a canary bot with `claudna.harvest: true` composes with no
  `claudna-harvest-pin` warning and, after its next sealed segment, has a draft filed through `claudron capture`. Cite the
  observation in 6a's PR body behind `no_names`. **Mutant** (committed code): the migration warning firing on D009.
  **The two-leg gate** against 6a's own before-leg (Task 0), CI on Linux, and `pytest -q -m "vault and not quarantine"`
  under the vault extra (Step 5).

### Task 4: the `session-export` fleet job and the `session_summary` event

**Files:** `claudlobby/session_export.py` (new), `claudlobby/commands/session_export.py` (new), `claudlobby/commands/_parsers.py`,
`claudlobby/plane/registries.py`, `claudlobby/doctor.py` (the `session-export` rung), `claudlobby/system.yaml`, `system.yaml.example`,
`tests/test_session_export.py` (new), `tests/test_event_type_registry.py`, `tests/test_doctor*.py`, `tests/test_composer.py`
(timer-set pins), `documentation/architecture/module-map.md:11`.

- [ ] **Step 1 (tests first).** `tests/test_session_export.py`, with a fake `run` (the injectable `subprocess.run`) returning
  canned envelopes and recording argv, a scratch plane (`_fleet_root` + `initialize_plane`, the `tests/test_plane_registry.py:694-702`
  shape): (a) an `ok` item and a `skipped` item become two `system` rows whose `payload.event == "session_summary"`,
  `source_ref.startswith("fleet-events:")` (the helper's; the reader selects on the prefix and `event_id` carries the dedup key —
  no `session-summary:<sid>/<seg>` sub-grammar, X2), `event_id == derive_uid("ev", f"session_summary:{fleet}:{sid}:{seg}")`,
  `occurred_at == segment.sealed_at`, `observed_at` the run instant, subject `actor`/`bot:<fleet>/<bot>`, `data ==
  {"source": "session-export", "legacy_ts": …, "data": {…}}` with `data.data.session_uid == derive_session_uid(sid, runtime)`
  and `runtime` absent → `"claude"`; on the skipped row `turns`, `transcript_bytes`, `journey`, `blocks`, `procedures`,
  `producer` are all `None` while `sealed_at`/`sealed_by`/the four counts are populated (the null rule, X19); (b) **the read-side
  pin the digest never had:** `load_lib_module("plane-readers.py").fleet_events(conn, fleet, event_type="session_summary")`
  renders `data["status"]`, `data["journey"]["title"]`, `data["session_id"]` populated; (c) emit then ack: argv shows
  `export --root <state> --consumer claudlobby --include-skipped --json`, then **one `emit_batch` call per item** (the fake
  `emit` records call sizes: all 1), then one `--ack --sid <sid> --through <seg>` per `next` entry, each ack after *that
  session's* items landed and before the next session's first emit; (d) run twice → second run's outcomes are all `duplicate`,
  no new rows, acks repeat (the cursor never moves back); (e) `emit_batch` raising `sqlite3.OperationalError` on session B →
  session A acked, **B not acked**, B's `consecutive_failures` is 1 in `<fleet_state>/session-export/<bot>.json`; (f) missing
  `entrypoint.json` → skip `no_entrypoint`; `schema != "claudna.entrypoint/1"` → `unknown_schema`; `plugin_version` `None` or
  `< 0.28.0` → `old_entrypoint`; `entrypoint` path absent on disk → `stale_entrypoint` (F16), each named in the tick's output
  and none emitting; (g) a 60 s `TimeoutExpired` → `export_timeout`; a non-zero exit or non-JSON stdout → `export_failed` with
  its own counter (X18), both counting as a failure; (h) the bound: a `journey.arc` of 50 000 chars and 200-item lists
  serialize under `DATA_CAP_BYTES` with `arc` cut first, lists second, and the JSON still parses; (i) `SESSION_EXPORT_ENABLED=0`
  → the loud line, exit 0, nothing run; (j) **the wedge test (X20):** emit seg 1 without acking (the fake `run` drops the ack),
  then the envelope gains seg 2 → the next run commits seg 2, seg 1 is `duplicate`, **both** acked; (k) a `ContractViolation` on
  one item → that item is re-emitted as `{status: "skipped", skipped_reason: "unexportable"}` (identity and segment fields only)
  under **its own** `event_id`, `derive_uid("ev", f"session_summary_unexportable:{fleet}:{sid}:{seg}")`, the sid is acked, the
  tick says so; the item's derived id pre-seeded in `ingest_ledger` with no family row (the ledger/family divergence,
  `ingest.py:633-639`) → the same fallback lands and the sid is acked; the fallback's id pre-seeded the same way too → the
  refusal is recorded as `last_unexportable` in the per-bot file and the sid is **still acked** (the terminal rule); (l) three
  consecutive failed ticks for one bot → one `export_stalled` row at `notice` on the bot's actor **and** one alert-door call
  (a fake `notify` records `notify_fleet(level="alert", event="export_stalled", …)`); the fourth tick emits neither (one per
  stall; a tick that acks re-arms it); the stall's record raising on the third tick → it is not marked recorded and the
  fourth tick retries it (under the same derived id), while a page already submitted is not sent again — the #900 rule;
  a page that fails is retried the same way; (m) `doctor`'s `session-export` rung reads the per-bot files through
  `source_state.probe_source`: absent → nothing (a bot that has never sealed a segment), unreadable or unparseable → `warn`;
  it warns on `consecutive_failures >= 3` or `last_ack_at` older than 24 h while `pending > 0`, and a `last_skip` renders
  `pass` with the reason in its detail.
- [ ] **Step 2 — `claudlobby/session_export.py`** (stdlib + the plane):

```python
ENTRYPOINT_SCHEMA = "claudna.entrypoint/1"; EXPORT_SCHEMA = "claudna.export/1"; CONSUMER = "claudlobby"
EMITTER = "session-export"; DATA_SCHEMA = "session_summary/1"; EXPORT_TIMEOUT_S = 60.0
DATA_CAP_BYTES = cap_for("system", "data") - 4_096      # registries.py:81 — 16 384 today, so 12 288; the headroom is the envelope around data.data
MIN_PLUGIN_VERSION = (0, 28, 0)                          # the clauDNA release that writes entrypoint.json (X18)
STALL_AFTER = 3                                          # consecutive failed ticks per bot before one export_stalled record + one page
CAPS = {"title": 300, "intent": 300, "outcome": 300, "arc": 2_000, "list_items": 12, "list_item": 300, "skipped_reason": 64}

@dataclass(frozen=True) class Entrypoint: python: str; entrypoint: str; plugin_version: str | None; runtime: str | None
@dataclass(frozen=True) class BotOutcome: bot: str; emitted: int; duplicate: int; spooled: int; unexportable: int; acked: dict[str, int]; skipped: str | None; failed: str | None

def read_entrypoint(state_dir: Path) -> Entrypoint | str: ...          # the str is the skip reason: no_entrypoint | unknown_schema | old_entrypoint | stale_entrypoint
def export_argv(entry, state_dir) -> list[str]: ...                     # [entry.python or sys.executable, "-S", entry.entrypoint, "export", "--root", str(state_dir), "--consumer", CONSUMER, "--include-skipped", "--json"]
def ack_argv(entry, state_dir, sid, through) -> list[str]: ...
def summary_record(item: dict, *, fleet: str, bot: str, now: datetime) -> tuple[dict, datetime]: ...   # (data.data, occurred_at) — the spec's field block; skipped → turns/transcript_bytes/journey/blocks/procedures/producer None
def unexportable_record(item: dict, *, fleet: str, bot: str, now: datetime) -> tuple[dict, datetime]: ...   # identity + segment fields, status "skipped", skipped_reason "unexportable" — the cursor must advance; emitted under its OWN id, derive_uid("ev", f"session_summary_unexportable:{fleet}:{sid}:{seg}")
def bound(record: dict) -> dict: ...                                    # CAPS, then drop arc, then lists, until len(json.dumps(...).encode()) <= DATA_CAP_BYTES
def _system_event("session_summary", *, fleet, bot, sid, seg, occurred_at, observed_at, data) -> dict: ...   # returns fleet_event_request(event_type, fleet=fleet, subject_kind="actor", subject=f"bot:{fleet}/{bot}", bot=bot, source=EMITTER, data=data, occurred_at=…, observed_at=observed_at, event_id=…) — P1 Task 9b; the literal first argument is what tests/test_event_type_registry.py scans; _system_event("export_stalled", …) is the second call site
def export_bot(root, fleet, bot, state_dir, *, run=subprocess.run, now=None) -> BotOutcome: ...   # read → export → per sid: per item emit_batch(root, [event], require_commit=False) → ack next[sid] → next sid; a failure stops this bot and is recorded
def report_stall(root, fleet, bot, state: dict, *, emit=emit_batch, notify=notify_fleet) -> dict: ...   # at STALL_AFTER: the export_stalled record (notice; event_id derived from fleet, bot and stall.since) + the alert-door page; marks each part only once it landed (#900)
```

  `summary_record`: `status` = `"skipped"` when `item["summary"] is None` else `"ok"`; `skipped_reason = item.get("skipped", {}).get("reason")`
  (an unknown reason is still a skip, as plan 5's spec §8 text says); `runtime = item["session"].get("runtime") or "claude"`;
  `session_uid = derive_session_uid(sid, runtime)`; on an `ok` item `turns = summary["input"]["turns"]`,
  `transcript_bytes = range.end - range.start`; **on a skipped item they are `None`** — `summary` is `null`, so there is nothing to
  read them from, and the item carries no transcript pointer to compute them from (X19; F6(b)); `sealed_at`/`sealed_by`/counts from
  `item.get("segment")` (absent on an older clauDNA → `None`; `occurred_at` then falls back to `session.closed_at`, else
  `now`); `blocks = {"count": len(blocks), "kinds": Counter(b["home"] for b in
  summary["blocks"])}` — the block item's type key is `home` (`claudna.segment-summary/1`, `segment-summary.schema.json`);
  `procedures = len(...)`; `producer` verbatim. `occurred_at` is re-emitted as an aware ISO instant (`EmitRequest.occurred_at`
  is `AwareDatetime`, `contracts.py:811`); an unparseable `sealed_at` falls back to `now` and stays raw inside `data`.
  `_system_event` calls `fleet_event_request` (`claudlobby/plane/fleet_events.py`, P1 Claudlobby Task 9b, Half A — never a
  third hand-rolled `{"event_type": "system", "source_ref": "fleet-events:…", "payload": {…, "data": {"source", "legacy_ts",
  "data"}}}`) with `fleet=fleet` (alias; ingest resolves the fleet uid), `subject_kind="actor"`, `subject=f"bot:{fleet}/{bot}"`
  (alias form, `ingest.py:313-322`), `source=EMITTER`, `occurred_at` (the seal), `event_id` (the derived id above) and
  `observed_at` (the run instant — the helper's `observed_at` slot). Interpreter: the hook's own recorded `python`
  (proven to run the store on this host), else `sys.executable` — never a PATH lookup, because a timer unit's environment is
  closed. **Why in-process (X22):** the epic's transport rule, stated once in its §6 P2 intake spec, puts one-shot Python ticks
  (`task-recheck`, `session-export`) and Python CLI doors on `emit_batch` in-process, as the task doors do
  (`task_operations.py:351`); this job is such a tick. **The job cannot wedge (X20):** one `emit_batch` per item — a batch
  that mixes an already-ingested id with a new one is refused as mixed state (`ingest.py:598-605`) and would hold the cursor
  forever (the alternative, equally
  acceptable: pre-read `ingest_ledger` for the derived `event_id`s and emit only the unseen, still one commit per item); ack
  per `sid` right after that session's items land; a `committed`, `duplicate` or `spooled` outcome (`require_commit=False`,
  daemon down: durably staged) all count as landed and **are acked**; a deterministic refusal of one item (`ContractViolation`,
  or `RuntimeError` from `_verify_duplicates`, `ingest.py:605-642`) is re-emitted as an `unexportable` status item (built by
  `unexportable_record`) under **its own** id, `derive_uid("ev", f"session_summary_unexportable:{fleet}:{sid}:{seg}")` — the
  refusals keyed on the item's existing ledger row (the idempotency conflict, `:608-613`; the ledger/family and `ingest_seq`
  divergences, `:633-645`) would refuse a re-emission under the same id the same way — so the cursor advances and the item stays
  visible as a `skipped` row. **Terminal rule:** if the fallback is refused too, the job records `last_unexportable` (`sid`,
  `seg`, both errors) in the per-bot file and acks anyway — a deterministic refusal never holds the cursor. A transient
  exception (`sqlite3.Error`, `OSError`) stops **this bot** for this tick, acks nothing further for it, and increments
  `consecutive_failures` in `<fleet_state>/session-export/<bot>.json` (`paths.fleet_state`, `paths.py:673`: `{"last_tick",
  "last_ack_at", "pending", "consecutive_failures", "last_skip", "last_failure", "last_unexportable", "stall"}`). That file
  is written as `automation_state._write` writes its state — temp file in the same directory, `fsync`, `os.replace`,
  directory `fsync` (`automation_state.py:118-139`) — under a `<file>.lock` held the way `_state_lock` holds one (`:32-49`,
  used at `:182`): the most complete of the repo's private atomic writers, named here rather than re-derived (no shared helper
  exists). **The stall:** at `STALL_AFTER` the tick reports once (`report_stall`) — an `export_stalled` record at **`notice`**
  on the bot's actor (`_system_event`, `event_id` derived from `(fleet, bot, stall.since)`, the stall's first failing tick) and
  a page through the existing alert door, `fleet_notification.notify_fleet(level="alert", event="export_stalled", message=…)`
  (`fleet_notification.py:54`), which records a fleet-anchored `fleet_alert` — built by Task 9b's helper — and pushes it to the
  manager and Telegram even when the plane is what is failing (it records best-effort, `:99-104`). `stall` in the per-bot file
  marks each part reported **only once it landed** — the record `committed`/`duplicate`/`spooled`, the page's
  `notification == "submitted"` — `debounce_notify`'s #900 rule (`lib-common.sh:3732-3742`: the marker is written only when
  the notify succeeded), so a later failing tick retries whichever part did not land, and a tick that acks clears `stall` and
  re-arms it. A skip reason (`no_entrypoint` … `stale_entrypoint`) is **not** a failure (the bot has nothing to export yet) and
  is recorded as `last_skip`.
- [ ] **Step 3 — the command and the timer.** `claudlobby/commands/session_export.py`: `tick(args)` (the `task_recheck.tick`
  shape, `:202-215`): `SESSION_EXPORT_ENABLED == "0"` → `print("session-export: OFF here (SESSION_EXPORT_ENABLED=0); nothing is
  exported")`, return 0; else `resolve_operation_scope(root=args.root, fleet=args.tick_fleet)` (`operation_context.py:222-228`),
  `state_dir = selected.paths.bot_runtime(bot_id) / "data" / "claudna"` per bot (`paths.py:686`; the per-bot roots of
  F14(a)), one `export_bot` per bot, one
  line per bot (`session-export: <bot>: 3 emitted (1 duplicate), acked through seg 4` / `skipped: stale_entrypoint <path>`), JSON
  `{"fleet", "bots": [BotOutcome…]}`, exit 0 on every operating path, 2 only for a malformed call; after each bot the tick
  rewrites `<fleet_state>/session-export/<bot>.json` (the doctor rung's and the monitor's read-only source); `--dry-run` runs
  the export and prints the records without emitting or acking — the operator's hand run, **never** the monitor's door (it
  spawns every bot's export from the caller's session; the monitor reads the per-bot state files with its granted `jq`, and
  `event list`, Task 5). `commands/_parsers.py` beside `:66-68`:
  `sub.add_parser("_session-export-tick", help=argparse.SUPPRESS)` with `tick_fleet` and `--dry-run`, `set_defaults(func=_command("session_export", "tick"))`.
  `claudlobby/system.yaml` after `task-recheck` (`:538`), with a comment in the file's register (why it ships on: spends nothing,
  deletes nothing, sends nothing; the spend is clauDNA's and gated by Task 2's switches): `session-export: { script:
  "$CLAUDLOBBY_CLI --root $CLAUDLOBBY_ROOT _session-export-tick", interval: 900, type: oneshot }`. Regenerate `system.yaml.example`
  with the recipe at `tests/test_fleet_mission.py:228-231`; update the composed-timer-set pins in `tests/test_composer.py`
  (grep `task-recheck` there). The unit carries `Environment=SESSION_EXPORT_ENABLED=…` from `FLEET_JOB_ARMING` by construction.
- [ ] **Step 4 — registry and its gate.** `plane/registries.py`, after `:204`: `# #2145 F6: one per sealed clauDNA segment, recorded by
  the session-export fleet job from the store's export; the monitor's substrate, never an alert. session_digest above stays
  registered so history classifies.` / `"session_summary": "notice",` and after it: `# #2145 P3: the session-export job
  failed STALL_AFTER ticks in a row for one bot — its cursor is not moving; one per stall. notice: the record, read by event
  list and the session-export doctor rung; the alert door pages (notify_fleet, level=alert — its fleet_alert is the critical
  row).` / `"export_stalled": "notice",` — notice by the registry's own rule (`:100-104`: a type a writer records directly is
  notice, and a FLEET ALERT is critical — here the alert door raises it), so the `reload_failed` analogy goes. It is not in a
  bot's brief, whose ALERTS read that bot's own critical events only (`brief.py:566-608`); fleet-pulse pages a fixed list
  (`_CRITICAL_ESCALATION_TYPES`, `fleet-pulse.sh:769` at `cd292cb`), which the alert door's own push makes unnecessary.
  `tests/test_event_type_registry.py`: `PY_WRITERS` (`:275-278`) gains
  `"claudlobby/session_export.py": "_system_event"`; `:323` drops `session_digest` (its shell writer is gone) and `:328-330`
  gains `session_summary` and `export_stalled`; `:396` drops `RS + "transcript-digest.sh"`.
- [ ] **Step 5 — the doctor rung.** `doctor.check_session_export(fleet, paths, report)` beside `check_switches` (`doctor.py:1099`):
  for each bot, probe `<fleet_state>/session-export/<bot>.json` with `source_state.probe_source` (`source_state.py:136`;
  `check_workstream_residual` is the precedent for a file in the same directory, `doctor.py:1023-1048`): absent → nothing (never
  ran, or nothing sealed); unreadable, or unparseable JSON → `warn` naming the file (an unreadable state file is not the same
  fact as no state file); `consecutive_failures >= STALL_AFTER` → warn `session-export-stalled` with `last_failure`;
  `pending > 0` and `last_ack_at` older than 24 h → warn `session-export-cursor-age` naming the bot and the age; a `last_skip` →
  `pass` with the reason in its detail (a stale entrypoint is the operator's cue to restart the bot) — `Check.status` has no
  info level (`doctor.py:34-37`; `format_report` prints anything but pass/warn/skip as FAIL, `:1497-1510`), and `skip` means a
  rung an operator turned off (#1745). This is the operator's view of the files the monitor reads with `jq` (Task 5).
- [ ] **Step 6.** `documentation/architecture/module-map.md:11` names `session_export.py` beside `task_recheck`. Verify:
  `./.venv/bin/pytest tests/test_session_export.py tests/test_event_type_registry.py tests/test_fleet_mission.py tests/test_composer.py
  tests/test_doctor*.py -q`.
  Commit: `feat(plane): the session-export fleet job records one session_summary per sealed clauDNA segment, acks per session, never wedges (#2145 F6/F16)`.

### Task 5: re-point the four consumers, field by field

**Files:** `library/skills/fleet-digest/SKILL.md`, `library/skills/fleet-observe/SKILL.md`, `library/protocols/fleet-monitoring.md`,
`library/expertise/ai-platform-monitor.md`, `tests/test_no_retired_digest_reference.py`.

- [ ] **Step 1 (test first).** `tests/test_no_retired_digest_reference.py` `RETIRED` (`:41-45`) gains `r"\bsession_digest\b"`,
  `r"transcript-digest"`, `r"SESSION_DIGEST_"`; the docstring (`:1-29`) is rewritten: the writer retired in #2145 P3, the live
  surface is `session_summary` via `claudlobby event list --type session_summary`. It is now red on every line below.
- [ ] **Step 2.** The spec's mapping table, applied:

| Where | Change |
|---|---|
| `fleet-digest/SKILL.md:3,26-27,41,58,60` | `session_digest` → `session_summary`; "no `transcript-digest` file" → "recorded by the `session-export` fleet job from clauDNA's export" |
| `:64` jq | unchanged shape — `.data` **is** the summary record (`legacy_event_row` lifts `detail.data`) |
| `:82-90` | the job ships on: zero rows + no error means no bot sealed a segment in the window, or the fleet set `SESSION_EXPORT_ENABLED=0`, or every bot's entrypoint is stale/old — say which, from the job's per-bot state files, `<fleet_state>/session-export/<bot>.json` (`last_skip`, `last_ack_at`, `pending`, `consecutive_failures`, `stall`; `paths.fleet_state`, `paths.py:671-679`: the fleet's `runtime/`, i.e. `"$BOT_DIR/../.."` for a manager in an overlay fleet), read with the skill's granted `jq` (`SKILL.md:5-8`) — not `claudlobby host doctor`, which neither monitor skill is granted and which runs the whole `validate()` and, after 6a, a per-vault `claudron doctor` probe of up to 60 s; never `_session-export-tick --dry-run`, a suppressed private command that spawns every bot's export from the monitor's session. **A second stop rule (B5):** every row in the window `skipped` → print `COVERAGE: summaries armed on 0 of N bots (all rows skipped: <reason tally>)` and stop — identity and volume are real, but there is no journey to reason over. Week one reads exactly that way on most fleets: N bots × status rows, journeys only where `claudna.session_summary` is armed |
| `:102,113` | `by_status` keeps working; counts are `ok · skipped` (no `error` class) |
| `:115-116` | "`skipped` means the summarizer did not run: `skipped_reason` names why — `disabled` (summaries off for the bot: the composed default, `CLAUDNA_SESSION_SUMMARY=0`), `headless` (a run with the variable unset — not a composed bot), `trivial`, `no_transcript`, `gave_up`, `retired`, `unexportable` (the job could not record the item); any other value is still a skip. Not a failure." |
| `:120-121,125-133` | rubric → `journey.title/intent/outcome/done/in_progress/next`; volume jq sums `prompts`, `failures` over every row and `turns`, `transcript_bytes` over `ok` rows only (both are `null` on a skipped row — the template says `turns · bytes: ok rows only`); drop `tool_calls` — its successor is not promised here: P2-a2, the plane leg, writes `session.tool_calls` samples but serves `usage_read`/`brief_read` first (A-F3, ratified 2026-10-05), so the `session.tool_calls` step waits for a PR that grants `fleet-digest` the `plane samples` door (`SKILL.md:5-8` grants `jq`, `python3`, `event list` only); friction jq selects `status=="ok"` rows with `journey.outcome != "completed"` or `failures > 0` or non-empty `journey.next`, keeping `session_id, bot, fleet, ts` |
| `:173-174,187-192` | cut order: `skipped` rows (count only) first, then `ok` rows with an empty `journey`; template `rows: N ok · N skipped`, `VOLUME: sessions · turns · prompts · failures` |
| `fleet-observe/SKILL.md:34-35,39,43-49,52` | `failed`/`would_change` → `journey.outcome` not `completed` + `journey.next`; `worked`/`reusable` → `journey.done` + `blocks.count/kinds`; "rubric left empty" (`:43-45`) → a `skipped_reason: disabled` row on a substantial session is **not an instrument failing** — the instrument is off by composition, which `:47-49` ("a gap you can name is a finding") already covers: name it once as *coverage* ("summaries armed on k of N bots"), never per session; the stop rule mirrors `fleet-digest`'s (all rows skipped → the coverage line, stop); token bloat → `transcript_bytes` and `prompts` over `ok` rows (tool totals wait for the `plane samples` grant, see above) |
| `fleet-monitoring.md:39-41,98,102-115,119-122` | `:39-41` "Nothing watches for sessions ending … no poller" → "A session ending is still an event the session reports — clauDNA's SessionEnd seals the segment inside the session; the plane learns of it when the `session-export` timer reads the store's export door (every 15 min): a poll of a contract door, never of transcripts or liveness"; source row → `session_summary`; the contract block becomes the spec's field table (identity `ts · session_id · session_uid · runtime · bot · fleet`; `status` + `skipped_reason`; volume `turns · transcript_bytes` (`null` on skipped) `· prompts · skills · failures · interrupts`; `journey.*`; `blocks.count/kinds`, `procedures`; `producer.model/duration_ms/cost_usd`; `seg · sealed_at · sealed_by`); the dormancy paragraph → "the job runs by default; a bot with summaries off still yields a `skipped` row, so an **empty** window means no sealed segment, a disabled job, or a stale entrypoint — name which (the job's per-bot state files, `<fleet_state>/session-export/<bot>.json`, read with `jq`; `host doctor`'s `session-export` rung is the operator's view of them)"; cite #1456/#1503: the digest's rows never reached this reader |
| `ai-platform-monitor.md:22` | "The plane's `session_summary` events (`claudlobby event list --type session_summary`) \| One per sealed segment — the journey (title, intent, outcome, done, next) when summaries are on for the bot; identity and volume always" |

  File now, independent of P3 (PC): the reader-filter defect — `plane-readers.py:1155-1164`'s `fleet-events:` filter silently
  drops rows other writers stamp differently (#1456/#1503 saw the symptom as an empty window) — one issue, so the next writer
  learns it from the registry gate, not from a monitor that reads nothing.
- [ ] **Step 3.** Verify: `./.venv/bin/pytest tests/test_no_retired_digest_reference.py tests/test_skill_ref_resolution.py -q`.
  Commit: `docs(library): the monitor reads session_summary — fleet-digest, fleet-observe, fleet-monitoring, ai-platform-monitor re-pointed`.

### Task 6: retire `transcript-digest.sh` and `plane-session-start.sh`, every touchpoint

**Files:** deletions `claudlobby/_runtime_scripts/transcript-digest.sh`, `claudlobby/_runtime_scripts/plane-session-start.sh`,
`tests/test_transcript_digest.sh` (auto-collected by `tests/test_sh_suites.py:33` — deleting it is the change),
`tests/test_transcript_digest_isolation.py`, `tests/test_plane_session_hook.py`; edits listed below (including `claudlobby/plane/ids.py:25-28`).

- [ ] **Step 1 (test first).** A new test in `tests/test_composer.py`, `test_no_retired_hook_is_composed`: no composed
  `settings.local.json` names either script and the package `system.yaml` has no `SessionEnd` entry and no
  `plane-session-start` `SessionStart` entry. **The one "no retired name left" criterion** (the Verification Checklist cites
  it; there is no second pattern): `grep -rn 'transcript-digest\|transcript_digest\|plane-session-start\|\.plane-session\|SESSION_DIGEST\|session_digest\|session-digest'
  claudlobby/ tests/ harness/ library/ documentation/ CLAUDE.md AGENTS.md *.example | grep -v -e documentation/plans/ -e
  system-map-2026-07-30 -e CHANGELOG` returns **only** this allowlist of deliberate history — `claudlobby/plane/registries.py`
  (the `session_digest` entry and its comment, `:201-204`, kept so history classifies, and Task 4's `session_summary` comment
  that points at it); `tests/test_system_event_retention.py:74`; `tests/test_no_retired_digest_reference.py` (its docstring
  and `RETIRED` tokens); and this test. Every other mention is removed or reworded so it names no retired file: the
  `isolation.py` docstring, the `plane/ids.py:25-28` comment and `observable-plane.md:67-69` (Step 4) say "the digest hook" or
  "the SessionStart hook".
- [ ] **Step 2 — code and config.** `claudlobby/system.yaml:402-425` both comment+hook blocks removed; `system.yaml.example`
  regenerated. `switches.py:529-540` row removed and the three switch tables regenerated (Task 2 Step 3's command); `:397`
  comment names `spindown-receipt` only. `isolation.py:100-103` docstring: "The one copy; the shell fallback retired with the
  digest hook (#2145 P3)". `harness/validate-bot-change.sh:3576`
  loop → `update-siblings claudna-harvest code-audit-sweep`; `:3580` → grep the arm line `host doctor --switches` prints for
  `claudna-harvest` (`bots.<bot>.claudna.harvest: true …`).
- [ ] **Step 3 — tests.** `tests/test_switches.py:99,106` (drop `session-digest`, fix the comment), `:357-358` (resolve
  `OBSERVABILITY_UNASSIGNED_CHECK=1` / `worker-unassigned` instead), `:417`, `:734-755` (`test_the_four_corrected_carriers`:
  `spindown-receipt` keeps the BOT_CONF assertions; the docstring loses the digest), `:804,961` comments;
  `tests/test_fleet_claude_bin.py:188`; `tests/test_plane_emit_class.py:414`; `tests/test_plane_gauntlet_doors.py:27` and the
  comment at `:148`; `tests/test_heavy_slot_match.py:158` → `-k boot_admission`. `tests/test_event_type_registry.py:323,396` were
  done in Task 4. `tests/test_system_event_retention.py:74` **unchanged**.
- [ ] **Step 4 — docs and indexes.** `environment-variables.md:199` row removed and `:189-195` reworded (the opt-in example is
  now `claudna.*`, carried by composition, not `env:`); `fleet-update-lifecycle.md:385,422-428` → the digest retired; the
  `SPINDOWN_RECEIPT_ENABLED` half of the anecdote stands; `architecture/module-map.md:44`'s list of the switches that stay
  off says "`claudna-session-summary` and `claudna-harvest` spend" where it said "`session-digest` spends" (its count
  follows); `testing-plane-isolation.md:82` drops `transcript-digest`;
  `system-yaml-schema.md:388-389` rows removed and `:505-510` → "no opt-in self-gate remains among the composed hooks; clauDNA's
  `CLAUDNA_*` gates are composed from `bots.<name>.claudna`", roster `:425-428` gains `session-export | interval: 900 | (absent —
  enrolled); the private tick skips on SESSION_EXPORT_ENABLED=0`; `plane/ids.py:25-28`'s comment ("minted fresh per process at SessionStart, never derived") → "`process_uid` has no minter since the SessionStart hook retired (#2145 P3); the prefix stays registered so historical `proc_` rows classify" — `"process": "proc_"` is **kept** (B6);
  `observable-plane.md:67-69` keeps P1's sentence (Half A, plan 2 Task 5 Step 1: "… other runtimes derive in Python only,
  through `ids.session_alias` (#2145 F2)") and changes only its bash-copy clause — "the bash derivation in
  `claudlobby/_runtime_scripts/plane-session-start.sh` is pinned byte-identical to `ids.derive_session_uid(id)` for runtime
  `claude`" → "the bash copy retired with the SessionStart hook (#2145 P3)" — so register row 10's citation of this paragraph
  as the join key's prose stays true; `:212` removed, `:214` → ``| `claudlobby _session-export-tick` (fleet timer invokes the selected CLI) | one `session_summary`
  system event per sealed clauDNA segment on the bot's actor — identity, volume, the journey and block tally when summaries are
  on, `skipped_reason` otherwise; `data.session_uid` is the F2 uid | `SESSION_EXPORT_ENABLED=0` in the fleet-tier `.env`;
  `PLANE_EMIT_DISABLED=1` |``; root `CLAUDE.md:124,140` rows deleted (the tick has no launcher, like `task-recheck`);
  `_runtime_scripts/CLAUDE.md:69,116` rows deleted; `cp CLAUDE.md AGENTS.md` at both levels (`tests/test_instruction_budget.py`).
- [ ] **Step 5 — the v2 ruling, checked (one line).** Half A (a dependency) wrote it in plan 2 Task 5 Step 1: confirm
  `2026-08-18-observable-plane-design-v2.md` carries P1's §19 item 9 and its dated note at `:606` (the SessionStart hook
  superseded) — this PR writes neither, so the ruling keeps one author.
- [ ] **Step 6.** Verify: `./.venv/bin/pytest tests/test_sh_suites.py tests/test_switches.py tests/test_plane_gauntlet_doors.py
  tests/test_plane_emit_class.py tests/test_fleet_claude_bin.py tests/test_instruction_budget.py tests/test_fleet_mission.py -q`;
  `bash harness/validate-bot-change.sh` directly (a harness-exercised script changed). Commit: `refactor(hooks): retire
  transcript-digest.sh and plane-session-start.sh — derive_session_uid is the one derivation (#2145 F2/F6/F15)`.

### Task 7: the F14 cutover runbook

**Files:** `documentation/runbooks/claudna-state-dir-cutover.md` (new). 6b carries the document (its CHANGELOG line is
Task 7c's) and the operator runs it after 6b, per F14(a): steps 1–4 first; step 5, the rename, under its interactive-user
condition, once every bot has restarted onto its per-bot root (§10 order 3's activation) and steps 1–4 have confirmed the
seal and closed what bots left open; step 6, the removal, once `CLAUDNA_RETAIN_DAYS` has passed since the last seal.
The seal itself is not this document's: it ran at order 3's activation (Task 1 Step 2).

- [ ] **Step 1.** The runbook, operator-run, in this order: (1) Task 1's composition is already active (§10 order 3) and its
  activation sealed the old root's bot sessions (Task 1 Step 2) — confirm the seal: Task 1 Step 2 (i)'s check shows no bot
  still writes to `~/.claudna`, and the order-3 Verification Checklist item holds (no `open` row for a switched bot under
  `list --bot … --root ~/.claudna`; the last `sweep` printed an empty `closed` list); (2) once 0.28 is on the bots, each has written `<BOT_DIR>/data/claudna/entrypoint.json` at an
  opening SessionStart; (3) `E="$(jq -r .entrypoint <any bot>/data/claudna/entrypoint.json)"` (fallback, **not a contract**: the
  plugin cache under the bot's `CLAUDE_CONFIG_DIR`, `composer.py:1046` — the path Task 1 Step 2 uses); (4) re-run Task 1
  Step 2's (ii)–(iii) so nothing a bot opened is still open before the rename: `python3 -S "$E" list --bot <b> --json --limit
  100000 --include-private --root ~/.claudna` → each `"status": "open"` row → `python3 -S "$E" seal <sid> --root ~/.claudna`,
  per bot, then `CLAUDNA_UNCLOSED_AFTER_H=1 python3 -S "$E" sweep --root ~/.claudna` until its `closed` list is empty — never
  `seal` for "every open session", which would close the service user's live interactive sessions (`cli.py:506-512`); leave
  `CLAUDNA_SESSION_SUMMARY` unset (bot sessions gate `headless`, no spend; `SETUP_GUIDE.md:320`); (5) **only when** its
  condition holds — the service user runs no interactive clauDNA sessions on that host (they share the default root) — or after that user's interactive shell has been
  given its own root first (`CLAUDNA_STATE_DIR` in clauDNA's `shell/` aux additions, verified with one interactive session):
  `mv ~/.claudna ~/.claudna.retired-<date>` — **rename, never `chmod -R a-w`**: under a read-only root the hook exits 0 and logs
  (`plugin-hooks/session-store.sh:22-31,43-44`; `cli.py:177-213`) and 0.28's `entrypoint.json` write fails the same silent way,
  so a chmod'd root would quietly swallow every interactive session; leave the renamed root until `CLAUDNA_RETAIN_DAYS` (30,
  `retention.py:42`) has passed since the last seal; (6) remove it. Nothing in the composer or the job reads the old root.
  Commit: `docs: the clauDNA state-dir cutover runbook (#2145 F14)`.

### Task 7c: CHANGELOG (6b)

**Files:** `CHANGELOG.md`.

- [ ] `CHANGELOG.md` `[Unreleased]`: one `### Added — …` for the job and event (with the consumer mapping), one
  `### Changed — …` per Task 1/2 (Task 3's pin entry rides PR 6a; the `CLAUDNA_STATE_DIR` line has its own, Task 1 Step 1),
  one `### Removed — …` for the two hooks naming F15's waiver and the F2 single implementation. Task 1's entry carries the
  heads-up for operators who armed summaries through env: a `CLAUDNA_SESSION_SUMMARY` or `CLAUDNA_HARVEST` set in a `.env`
  tier is now overridden by the composed line, and one set in `env:` overrides the `claudna:` mapping — move it to
  `claudna:` (`config validate` warns `claudna-env`), and points operators at Task 7's cutover runbook, run after 6b.
  Commit: `docs: the P3 changelog (#2145)`.

### Task 8: gate, harness scenario, canary-root observation

- [ ] **Harness scenario** in `harness/validate-bot-change.sh` (the `val_scenario`/`harness_check` shape): compose a scratch
  fleet; assert `bot.conf` carries `export CLAUDNA_STATE_DIR="$BOT_DIR/data/claudna"`, `export CLAUDNA_SESSION_SUMMARY=0` on an
  unarmed bot, and no `SESSION_DIGEST`; the composed
  `settings.local.json` names neither retired script; seed `runtime/bots/<b>/data/claudna/entrypoint.json` pointing at a stub
  `session_store/__main__.py` that prints a canned `claudna.export/1` envelope (one `ok` item, one `skipped` item, both with
  `segment{}` and `session.runtime`) and appends `--ack` argv to a file; `"$VAL_CLI" --root "$ROOT" _session-export-tick "$FLEET"`;
  `val_events "$ROOT" "$FLEET" "$BOT" session_summary` shows two rows with `"status":"ok"` / `"status":"skipped"` and a
  `session_uid`; the ack file has exactly the `next` entries; a second run adds no row; a stale `entrypoint` → the skip line and
  zero rows; `SESSION_EXPORT_ENABLED=0` stamped on the unit → the OFF line. The scenario runs with
  `CLAUDLOBBY_HOST_SYSTEM_YAML=/nonexistent/…` (as `tests/conftest.py:150` does — the harness never pinned it, B9) and a
  `claudron` stub in `$STUB_BIN` answering `status --json`/`doctor --json` with nothing pending, so an operator's real host
  override and vault state never reach the run. Record the harness's new pass/fail pair **by name** before the two-leg comparison.
- [ ] **Drift gate (X17), wired to what CI actually runs.** Today `conformance.yml:61-73` checks clauDNA out at its **default
  branch** (no `ref:`) and runs only `python -m claudlobby.conformance rename-map`; `:12` names only `vault-tests` as a
  required check; and no job sets `CLAUDNA_REF`, which `conformance.py:116` calls "the knob CI pins its checkout to". This PR
  adds: (1) `claudlobby/contracts/claudna.ref` (new, checked in) — the clauDNA release the export contract is read at
  (`v0.28.0`), moved by the PR that moves the fleet to a newer clauDNA release: the `.ref` half of clauDNA's own
  `contracts/claudron.ref` precedent; (2) `claudlobby/contracts/claudna-export.schema.json` (new) — the vendored copy of
  clauDNA's `lib/claudna/session_store/schemas/export.schema.json` at that ref (the path is repo-relative: there is no
  `schemas/` at the clone root); (3) a subcommand, `python -m claudlobby.conformance export-contract <clone root>`, handed the
  checkout's root: it fails when the vendored copy differs from the clone's file; copies a checked-in fixture store
  (`tests/fixtures/claudna_export/store/`, captured once from a real 0.28 store with identifiers scrubbed — never clauDNA's
  `tests/conftest.py`) to a temp dir; runs `python3 -S <clone>/lib/claudna/session_store export --root <the copy> --consumer
  claudlobby --include-skipped --json`; validates the envelope with the clone's own validator (`claudna.session_store.schema`)
  against the vendored schema; and feeds every item through `session_export.summary_record` — one gate on the real envelope,
  not on canned fixtures. An absent checkout prints `SKIP` and exits 0 (the module's local-first rule, `conformance.py:18-23`);
  (4) in `conformance.yml`, an `export-contract-gate` job in the `rename-map-gate` form whose clauDNA checkout reads
  `claudna.ref` into `ref:` (a prior step echoes it to `$GITHUB_OUTPUT`) and keeps `continue-on-error: true`, so a failed
  checkout skips with notice; it is **not** a required check (`:12` stays `vault-tests` only) — a clauDNA outage or a moved tag
  must never block a Claudlobby merge — and its header comment says so. The workflow edit is pushed with the `workflow` scope
  (`5694413b`); (5) the default lane's half (`test.yml`'s `pytest` job; Claudlobby has no `make check`): an offline test in
  `tests/test_session_export.py` checks the checked-in canned envelopes (`tests/fixtures/claudna_export/envelopes.json`,
  generated from the fixture store once and regenerated when `claudna.ref` moves) against the vendored schema — every
  `required` key present, with the JSON `type` its `properties` name (the keyword subset clauDNA's own validator implements,
  `schema.py:25-29`; no new dependency). The PR also corrects `conformance.py:116`'s docstring and the epic's §14 Q8, which
  repeat the "pinned checkout" premise. A producer-side leg (#223's form) is declined: clauDNA's own suite validates every
  envelope against `export.schema.json` (plan 5 Task 2 Step 4), so the envelope cannot change without its schema changing, and
  a changed schema fails (3) at the next `claudna.ref` move — a clauDNA-side job would add a cross-repo CI coupling for no new
  signal.
- [ ] **Mutants** (committed code): the ack before the emit; a two-item `emit_batch` (one seen, one new — ingest refuses it as
  mixed state and the cursor never moves); an item-level `ContractViolation` with no `unexportable` re-emit (the cursor wedges);
  the fallback re-emitted under the item's own id (the ledger-keyed refusal repeats and the cursor holds); `stall` marked
  reported before its record landed (a failed emit is never retried);
  a flat `data` (reader renders `{}`); `source_ref` without the `fleet-events:` prefix (reader returns nothing);
  `derive_uid("ev_", …)`; the bound dropped (a 50 KB arc → `detail_truncated=1`); a stale entrypoint "guessed" from the plugin
  cache; `CLAUDNA_STATE_DIR` conditional on `claudna_version`;
  `CLAUDNA_SESSION_SUMMARY` absent for an unarmed bot; a `"true"` string arming `claudna.harvest`.
- [ ] **The two-leg gate** (rc + scoped names + count line, against Task 0), CI on Linux, `bash harness/validate-bot-change.sh`
  directly.
- [ ] **Canary root (mandatory runtime validation).** One host, one real Claude bot with `claudna.session_summary: true` (one
  bot of many — the switch's own arm line) and a second with it unset; nothing here needs Claudron v0.9.0 (6a's preconditions
  and its `claudron-migration` check are Task 3 Step 6's). After a `/clear` or a SessionEnd and one tick (≤ 15 min):
  `claudlobby --json event list --type session_summary --since 1h` renders one row with `data.status == "ok"` and `data.journey.title` populated — **the thing the digest never achieved** — and one
  with `data.status == "skipped"`, `data.skipped_reason == "disabled"` (the composer wrote `CLAUDNA_SESSION_SUMMARY=0` into the
  unarmed bot's `bot.conf`, so the gate answers `disabled`, `project.py:276-287` — were it `headless`, the composition never
  reached the bot); `<BOT_DIR>/data/claudna/sessions/<sid>/consumers.json` shows `claudlobby.through_seg`;
  `<BOT_DIR>/data/.plane-session` is **not** rewritten by the new session; record whether `claude plugin update` left the prior
  `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>` directory on the bot host (X18 — plan 5's live check asks the same
  on a dev machine; the bot host is the one that matters). Cite the observation in the PR body behind `no_names`.

## Door consumer tables

| Door | Consumers at `cd292cb` | After this PR |
|---|---|---|
| `event list --type session_digest` | `fleet-digest/SKILL.md:58,60` (→ `fleet-observe` reads its output), `fleet-monitoring.md:98`, `ai-platform-monitor.md:22` (prose) | all four read `--type session_summary`; `test_no_retired_digest_reference.py` fails on any `library/` line that still names the digest |
| `$BOT_DIR/data/.plane-session` | `transcript-digest.sh:256` only (`test_plane_gauntlet_doors.py:148` is a dangling comment) | no reader, no writer |
| `derive_session_uid` | Python `plane/ids.py:79-89`; bash mirror `plane-session-start.sh:59-70`, parity-pinned by `tests/test_plane_session_hook.py:31-46` | Python only (`(id, runtime)` from P1); the parity suite retires with the mirror |
| `claudron doctor --json` | `doctor._claudron_doctor` (`doctor.py:795-870`) | `claudron_compat.vault_migration_state` — read by `doctor` (rows) and `validator` (the `claudron-migration` warning, PR 6a) |

## Stated limitations

- **`skipped_reason: disabled` is the common row.** The composer writes `CLAUDNA_SESSION_SUMMARY=0` for every bot a fleet has
  not armed, so clauDNA's gate (`project.py:276-287`) answers `disabled` — never `headless`, which it reserves for a bot or
  `claude -p` run with the variable unset — and the monitor can tell "off by composition" from "composition never reached the
  bot". It sees identity and the segment counts for every segment, `turns`/`transcript_bytes` and a journey only where the spend
  was chosen (both are `null` on a skipped row). That is F6's coverage concern kept (F15), not a defect.
- **`tool_calls` is gone** until a PR grants `fleet-digest` the `plane samples` door (P2-a2 writes the `session.tool_calls`
  samples; under A-F3 their first reader is `usage_read`/`brief_read`); `fleet-observe`'s token-bloat lens reads `prompts` meanwhile.
- **`config plan` probes `claudron doctor --json` once per distinct session-loop vault** (PR 6a; only with `claudron` on PATH;
  `CLAUDRON_DOCTOR_TIMEOUT_S`, 60 s, shared with doctor — Claudron #201 class); pending migrations, an old format and a timeout
  each **warn**, nothing here refuses — `config validate --strict` is the operator's refusal.
- **F14's "then remove"** assumes the service user runs only bots; the same default root serves that user's interactive
  sessions, so the runbook renames (never chmods) the old root only under that condition, or after the interactive shell has its
  own `CLAUDNA_STATE_DIR`. The seal touches bot sessions only (`list --bot`, then a `sweep` that closes only dead, idle
  sessions; Task 1 Step 2).
- **`config explain bot.claudna.<key> --bot B`** works through the strict-mapping provenance registry Task 1 carries (Steps 4–6,
  the canonical copy); if P2-a1 lands first instead, it carries those steps and 6b registers `claudna` in one line (X13) — a
  merge-order fact, not a limitation of the design.

## Test Plan

Unit: `tests/test_session_export.py` (new — the per-item emit, the wedge test, `unexportable`, `export_stalled`, the null rule),
`tests/test_doctor*.py` (the `session-export` rung), `tests/test_composer.py` (the always-composed `CLAUDNA_SESSION_SUMMARY`,
the no-retired-hook test, timer-set pins, the order-3 line), `tests/test_config.py` (the `claudna:` mapping, its cross-field
check, and the strict-mapping helper's cells), `tests/test_env_register.py` (the `config explain` provenance cells),
`tests/test_validator.py` (Task 1's `claudna-env` and `claudna-harvest-pin` warnings), `tests/test_switches.py`,
`tests/test_claudron_compat.py`, `tests/test_claudron_loop.py` (6a's validator warning cells; `TestSnippetParity` under the
vault extra), `tests/test_validate_warning_discipline.py`, `tests/test_event_type_registry.py`,
`tests/test_no_retired_digest_reference.py`, `tests/test_fleet_mission.py` (example pin), `tests/test_instruction_budget.py`;
the offline canned-envelope check in `tests/test_session_export.py` and the `export-contract` conformance leg (Task 8).
Removed suites: `test_transcript_digest.sh`, `test_transcript_digest_isolation.py`, `test_plane_session_hook.py`. Bash: the harness
scenario through `harness/validate-bot-change.sh` directly. The two-leg gate against Task 0's before-leg; CI on Linux.

## Verification Checklist

- [ ] `grep -c 'export CLAUDNA_STATE_DIR="$BOT_DIR/data/claudna"' <every composed bot.conf>` prints 1; `grep -c 'export CLAUDNA_SESSION_SUMMARY='`
  prints 1, and the value is `0` on every bot with neither `claudna.session_summary` nor `claudna.harvest` armed.
- [ ] (Order 3) The PR body cites Task 1 Step 2 (i)'s canary-root check; after the fleet's activation,
  `list --bot <b> --json --limit 100000 --include-private --root ~/.claudna` shows no `open` row for any switched bot, the last
  `sweep` printed an empty `closed` list, and no `seal` was run on a session `list --bot` did not return.
- [ ] `tests/test_session_export.py` wedge cell: seg 1 emitted unacked, seg 2 sealed → seg 2 `committed`, seg 1 `duplicate`, both
  acked; the two-item-batch mutant holds the cursor (shown, restored).
- [ ] Task 6 Step 1's one "no retired name left" grep returns only its allowlist (`plane/registries.py`'s `session_digest`
  entry and the comments on it, `test_system_event_retention.py:74`, `test_no_retired_digest_reference.py`,
  `tests/test_composer.py::test_no_retired_hook_is_composed`) — no other line, and no second pattern anywhere in this plan.
- [ ] `claudlobby host doctor --switches` names `claudna-session-summary`, `claudna-harvest` (off) and `session-export` (on); the
  three doc tables equal the render (`tests/test_switches.py:957-969`).
- [ ] (6a) `tests/test_claudron_compat.py::test_vault_pin_satisfies_compat_floor` is red at `@v0.6.1` (shown, restored) and green at `@v0.9.0`.
- [ ] `tests/test_session_export.py` read-side cell: `fleet_events(... event_type="session_summary")[0]["data"]["journey"]["title"]`
  is populated; the flat-`data` mutant renders `{}` (shown, restored).
- [ ] Harness: `session_summary` rows via `val_events`, acks after emits, no row on the second run, the stale-entrypoint skip line.
- [ ] Live, canary root: `claudlobby --json event list --type session_summary --since 1h` shows one `ok` row with
  `data.journey.title` and one `skipped` row with `data.skipped_reason == "disabled"`; `consumers.json` carries `claudlobby`;
  no fresh `.plane-session`.
- [ ] (6a, Task 3 Step 6) Live, canary root: `config plan` warns `claudron-migration` with the `claudron doctor --fix` line
  against the format-2 scratch clone, and is silent after the migration; no `claudna-harvest-pin` warning remains.
- [ ] Conformance: `python -m claudlobby.conformance export-contract <clone root>`, against a clauDNA checkout at
  `claudlobby/contracts/claudna.ref`, runs the real door through `summary_record`; the vendored
  `claudlobby/contracts/claudna-export.schema.json` equals the clone's `lib/claudna/session_store/schemas/export.schema.json`;
  CI's `export-contract-gate` job ran it (not a required check); the offline canned-envelope check passes in the default lane.
- [ ] The two-leg diff introduces no new failure names; the harness pair matches the Test Plan by name.

## What NOT To Do

- Do not write `session_summary` with a flat `data` or a non-`fleet-events:` `source_ref`: the reader (`plane-readers.py:1155-1164,1228`)
  would drop it exactly as it drops the digest today. Do not add a second reader.
- Do not rely on ingest truncation; bound `data` in the emitter (epic §11).
- Do not guess an entrypoint from the plugin cache (`installed_plugins.json`) when `entrypoint.json` is stale or absent — skip the
  bot and say so (F16).
- Do not ack before that session's items have landed; do not ack a session after an exception on it; do not put two items in one
  `emit_batch` call unless the ledger was pre-read for their ids — a batch mixing a seen and an unseen id is refused as mixed
  state (`ingest.py:598-605`) and holds the cursor forever (X20).
- Do not compose `CLAUDNA_HARVEST=1` without `CLAUDNA_SESSION_SUMMARY=1`: the gate never summarizes a bot on harvest alone
  (`project.py:276-287`).
- Do not point the monitor at `_session-export-tick --dry-run` or `claudlobby host doctor` (neither is granted to it; `host
  doctor` runs `validate()` and, after 6a, a 60 s vault probe): its read-only source is the job's per-bot state files, read with
  `jq` (Task 5).
- Do not `chmod -R a-w` the old `~/.claudna`: rename it, and only under the runbook's condition (F14 step 5).
- Do not `seal` every open session in the old root: `seal` passes no owner pid and would close the service user's live
  interactive sessions — seal the bot sessions `list --bot` returns, and let `sweep` (dead `claude`, idle past the hour) take
  the rest (Task 1 Step 2).
- Do not read a worker's session uid from `.plane-session`; do not keep a bash derivation (epic §11).
- Do not refuse a plan on any doctor finding — a pending migration is a named warning (warn-never-fail; `--strict` is the
  operator's refusal) and D007–D010/structure findings are nothing; never run `claudron doctor --fix` from Claudlobby (it
  commits to a shared vault).
- Do not arm `claudna.*` through `bots.<name>.env` (`defaults.env` is not merged; the mapping is the one way).
- Do not touch `architecture/system-map-2026-07-30.md` or `CHANGELOG.md` history; do not remove `session_digest` or `tool_call`
  from the registry.
- Do not compose or validate a Codex bot here (F11).

## Context

area: compose / plane / library · effort: 6b **M → L** (Task 1 Steps 3–11, Tasks 2, 4–7, 7c and 8; Task 7 is the runbook
document, which the operator runs after 6b): the cycle-1 wedge-proofing made Task 4 alone per-item emit, per-sid ack,
`unexportable`, `export_stalled`, the doctor rung, six skip reasons and 13 test legs, on top of two hook retirements and four
consumer re-points, and cycle 2 adds the canonical strict-mapping helpers, two validator warnings and the drift gate's CI
job · §10 order 3's one-line PR (Task 1 Steps 1–2) is S plus its operator-run activation · 6a (Task 3) is S plus the
operator-run migration · risk: medium (a fleet timer that spawns a
subprocess per bot; a `config plan` probe of an external CLI that warns; a library re-point that changes what the monitor reads) · priority: P3 · mission: D1 (F18 (a)) and D2 ratified 2026-10-05
([F18 lock](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-6000050806),
[D2](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-6000052137)) · related: #2145 (epic), #1503 (the digest's
plane cutover), #1456 (the rows the reader dropped), #1961 (the waived comparison, F15), #785 (the monitor), Claudron #190/#201
(doctor), clauDNA plan 5 (`2026-10-04-runtime-neutral-observability-p3-claudna-export-contract.md`).

Spec spellings corrected here, decisions unchanged: `derive_uid("ev", …)` not `"ev_"` (`ids.py:57-60`); `source_ref` is the
fleet-event helper's (`fleet-events:` prefix; no `session-summary:<sid>/<seg>` sub-grammar — `event_id` is the dedup key);
`EmitRequest.occurred_at` is `contracts.py:811`; the test that binds the protocol rewrite to the registry entry is
`test_no_retired_digest_reference.py` plus `test_event_type_registry.py:323`, not gate (d) (`fleet-monitoring.md` is outside
`DOCS`, `:404-408`); the epic's P3 bullet, which said composition "refuses" a session loop on an unmigrated vault, now reads
~~refuses~~ **warns** (epic `:1378`), as this plan does (Task 3, B3), for the reasons given there.

Answered here, carried to the epic: *why the record rides into the plane rather than a thin row fetched through the export
door* — the four consumers are manager-bot skills whose only door is `claudlobby event list` (`fleet-digest/SKILL.md:5-8`),
so a record they cannot reach is no record; *why `session-export` ships ON* — it spends, deletes, mutates and sends nothing
(the Defaults rule); the spend is clauDNA's, behind Task 2's opt-in switches. Whether a 12-field record is the right first
cut is E11's question for the operator (epic §14).
