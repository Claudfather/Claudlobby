---
title: "P3 — summaries owned by clauDNA: the session-export job, `session_summary`, the Claudron pin, and two hooks retired (Claudlobby)"
type: plan
status: draft
owner: chrisrogers37
created: 2026-10-04
epic: documentation/plans/2026-10-04-runtime-neutral-observability-plan.md
spec: documentation/plans/2026-08-18-observable-plane-design-v2.md
issue: "#2145"
repos: Claudfather/Claudlobby
---

# P3 — summaries owned by clauDNA: the session-export job, `session_summary`, the Claudron pin, and two hooks retired (Claudlobby)

> **Status:** draft, authored by `/claudna:forge` on 2026-10-04 from epic plan §6 P3 (Claudlobby bullets) and its two
> `#### Spec:` subsections (`the session_summary plane event`, `the clauDNA export contract additions`). Code references
> are to Claudlobby `cd292cb` (the checkout `2dd0aad5` is identical for every line cited). Depends on: the P1 Claudlobby
> PR (`BotConfig.runtime`, composed `CLAUDLOBBY_RUNTIME`, `derive_session_uid(id, runtime)`, the design-v2 §1.1
> amendment, Half B's Python fleet-event row helper, and Half B's conditional `start-bot.sh` C10 scrub); the P3 clauDNA
> release (plan 5: `export --include-skipped`, the item's `segment` object, `session.runtime`,
> `<CLAUDNA_STATE_DIR>/entrypoint.json`, `schemas/export.schema.json`); Claudron `v0.9.0` (`6ca2b94`, tagged) for PR 6a.
> Waits on canaries: none — this PR reads no `CLAUDE_*` variable and composes no Codex bot. Mission: every change here
> pays for a Claude-only fleet; the mixed-runtime framing rests on the Claudlobby mission decision the epic carries
> (D1/F18, epic §16).

## Summary

Claudlobby stops writing session summaries and starts consuming clauDNA's: each bot gets its own clauDNA root
(`$BOT_DIR/data/claudna`, F14), a fleet timer `session-export` reads every bot's export door at the path clauDNA's own
hook records (F16) and turns each sealed segment — summarized or skipped — into one `session_summary` system event
shaped for the reader the four monitor consumers already use (F6), then acks. `transcript-digest.sh` and
`plane-session-start.sh` retire with every touchpoint, making `ids.derive_session_uid` the one derivation (F2). The two
model-spending clauDNA knobs become fleet-level opt-in switches composed to match clauDNA's summary gate, and — in its own
PR, 6a, after the rest — the Claudron install pin moves `v0.6.1 → v0.9.0` behind an operator-run vault migration and a
named `config plan` *warning* (the validator's warn-never-fail precedent for host-side state). Deliberately left out: Codex
composition (F11, companion) and P2's `session.tool_calls` samples (the consumers lose `tool_calls` until that plane leg
lands and grants them the door).

## Evidence (Claudlobby at `cd292cb`)

- **Where clauDNA state lives today.** Nothing in Claudlobby sets `CLAUDNA_STATE_DIR` (repo grep outside plans: only
  `CLAUDNA_VERSION`, `claudlobby/composer.py:1319-1330`) or a per-bot `HOME` (`_runtime_scripts/start-bot.sh:96`
  `export HOME="$HOME"`), so clauDNA's `paths.state_root` (`lib/claudna/session_store/paths.py:51-59`: `$CLAUDNA_STATE_DIR`
  else `~/.claudna`; clauDNA `SETUP_GUIDE.md:317`) puts **every bot of the service user in one `~/.claudna`**. The
  `# Ecosystem` block (`composer.py:1319-1330`) is where the per-bot root belongs; `BOT_DIR=` is emitted at `:1010`
  (`paths.bot_runtime`, `:1007`; `claudlobby/paths.py:686`) and `bot.conf` is sourced under `set -a`
  (`start-bot.sh:227-229`), so a `"$BOT_DIR/…"` value expands at source time — the `TELEGRAM_STATE_DIR="$HOME/…"`
  shape at `:1041`, admitted by `path_audit.is_safe_anchored_path` (`claudlobby/path_audit.py:596-608`).
- **The seal step's entrypoint.** clauDNA's `seal <sid>` and `list --json` take `--root` (`cli.py:327-337,344-350`); the
  rows carry `sid` and `status` (`readers.py:80`). The store entrypoint is recorded only by the P3 clauDNA release
  (`entrypoint.json`, the export-contract spec), into the **new** root; the composer never learns it.
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
  `tests/test_fleet_mission.py:220-232`); `switches.py:529-540` (+ the comment at `:397`); `tests/test_switches.py:99,106,357-358,417,737-755,804,961`;
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
  `composer.py:1214-1218`. The opt-in allowlist is `tests/test_switches.py:98-110`; the three doc tables are pinned at `:957-969`.
- **Claudron.** Pin `pyproject.toml:32`; comment `.github/workflows/conformance.yml:37`; doc headline/claims
  `documentation/integrations/claudron-integration.md:7,44,48` (`tests/test_claudron_compat.py:155-174` pins the headline to
  the pin; `:115-152` pins pin ≥ the highest live `COMPAT_FLOOR` release, today 0.4.0 at `claudron_compat.py:65-74`).
  `hooks.settings_snippet(executable, vault_root)` and `SNIPPET_EVENTS` are unchanged between v0.6.1 and v0.9.0
  (`claudron/hooks.py:235-242`; the diff adds only the ops log), so `tests/test_claudron_loop.py::TestSnippetParity`
  (`:452-464`) should pass unchanged. The bump crosses `m003`/vault format 3 (0.7.0, `CHANGELOG.md:62`; `claudron/vault.py:68`
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

P1 Claudlobby merged (`derive_session_uid(id, runtime)`, `CLAUDLOBBY_RUNTIME`, the Python fleet-event row helper of Half B
that `_system_event` calls, and — if C10 leaked — Half B's conditional `start-bot.sh` scrub); clauDNA P3 released **with
`claudna_version` pinned on the fleet before that release merges** (X21): marketplace auto-update (`start-bot.sh:315-321`
→ `lib-common.sh:4699-4710`, `claude plugin update` on every bot start unless `BOOT_PLUGIN_UPDATE_ONCE=1`) otherwise
delivers 0.28 to every bot at its next restart, ahead of Task 1's composition; Claudron `v0.9.0` installed on the canary
host for PR 6a (`pip install -e '.[dev,vault]'` for the parity leg). **P2 is not a dependency** — P2 ∥ P3 (epic §10);
this plan lands beside plan 4 in parallel. Both edit `switches.py`, `system.yaml`, `plane/registries.py`,
`tests/test_event_type_registry.py` and the three switch tables, so: whichever of P2-a/P2-b merged first, this PR rebases
onto it, regenerates the three switch tables and `system.yaml.example`, re-copies both `AGENTS.md`, and re-runs
`tests/test_instruction_budget.py`, `tests/test_switches.py`, `tests/test_event_type_registry.py` before pushing (X12).
Order within P3: plan 5 (clauDNA) releases before Task 4 here can observe anything real.

### Blocks

The companion Codex epic (per-bot state dir and a runtime-neutral export consumer are prerequisites). P2: no ordering —
plan 4 *informs* this plan's consumer note (`tool_calls` returns as `session.tool_calls` samples when P2's plane leg
lands and grants `fleet-digest` the `plane samples` door), and this plan informs nothing in P2 (X12).

### Steps

Tasks follow as H3 siblings. Line numbers are pre-dependency: the P1 Claudlobby PR (plan 2) edits `config.py`,
`composer.py`, `plane/contracts.py`, `operation_context.py` and `plane/ids.py` before this PR opens — edit bottom-up or
re-grep each anchor at PR-open; `tests/test_switches.py`'s opt-in allowlist is `:99`–~`:130` (X15). Seam: Task 3 (the
Claudron pin) is its own PR, **6a**, after the rest (**6b** = Tasks 1–2, 4–8): the bump is an operator-run vault
migration with a per-host runbook and its own canary, and nothing in 6b calls what v0.9.0 adds — `claudna.harvest` is
documented as requiring 6a on the host before a bot arms it (B3).

### Task 0: worktree, before-leg, evidence

- [ ] Branch `p3/claudlobby-summaries` from `origin/main` **after** the P1 Claudlobby PR merged. Before-leg per
  `documentation/test-suite.md` (`:32`: the macOS suite is not green — compare **names and counts** across two separately
  prepared legs, unsandboxed; root `CLAUDE.md:254,325`): `./.venv/bin/pytest --tb=no -ra > <evidence>/run_before.txt 2>&1;
  echo $?`, then `bash harness/validate-bot-change.sh` and record its pass/fail pair by name. Evidence dir outside the repo.

### Task 1: per-bot `CLAUDNA_STATE_DIR`, and the `claudna:` mapping composed into `bot.conf`

**Files:** `claudlobby/config.py`, `claudlobby/composer.py`, `tests/test_composer.py`, `tests/test_config.py` (or the
file that holds `_coerce_observability`'s tests), `documentation/fleet-yaml-schema.md`, `fleet.yaml.example`,
`documentation/environment-variables.md`.

- [ ] **Step 1 (tests first).** `tests/test_composer.py`: every composed `bot.conf` carries exactly one
  `export CLAUDNA_STATE_DIR="$BOT_DIR/data/claudna"` line, **after** the `BOT_DIR=` line, unconditionally (a bot with no
  `claudna_version` too) — if the epic's §10 one-line `CLAUDNA_STATE_DIR` PR landed first (X21), this assertion verifies
  it and Step 3 adds only the knob lines; every `bot.conf` carries exactly one `export CLAUDNA_SESSION_SUMMARY=` line:
  `claudna: {session_summary: true}` composes `=1`; `false` **and unset** compose `=0` (the gate then answers
  `disabled`, `project.py:276-287` — never `headless`, which is what an unset variable yields for a bot);
  `claudna: {harvest: true}` composes `export CLAUDNA_HARVEST=1` **and** `CLAUDNA_SESSION_SUMMARY=1` (the gate never
  summarizes a bot on harvest alone); `harvest: true` with `session_summary: false` is a `ValueError` at load naming both
  keys; `defaults.claudna.harvest: true` reaches every bot and a bot's own `false` wins (field-wise); a string `"true"` is
  a `ValueError` naming the key (`_strict_bool`); an unknown key under `claudna:` is refused naming the accepted keys.
  `tests/test_env_register.py`'s `config explain` cell gains `bot.claudna.harvest` → (`fleet.defaults`,
  `fleet.defaults.claudna.harvest`) when set under `defaults:`. Existing tests pinning full `bot.conf` text gain the new
  lines. `tests/test_freshbox_selfcontained.py` stays green (the value is `$BOT_DIR`-anchored).
- [ ] **Step 2.** `claudlobby/config.py`, beside `IsolationConfig` (`:658`):

```python
@dataclass(frozen=True)
class ClaudnaConfig:
    """clauDNA's two model-spending knobs, composed per bot (#2145 P3). None = unset: clauDNA's default (off for bots)."""
    session_summary: bool | None = None   # CLAUDNA_SESSION_SUMMARY — one Haiku call per sealed segment
    harvest: bool | None = None           # CLAUDNA_HARVEST — summaries + draft notes into the bot's vault via `claudron capture`

_CLAUDNA_KEYS = ("session_summary", "harvest")
def _parse_claudna(raw: object, where: str) -> ClaudnaConfig: ...   # = _parse_strict_mapping(raw, where, {"session_summary": _strict_bool, "harvest": _strict_bool}) — unknown key → ValueError naming the accepted keys
def _merge_claudna(default: ClaudnaConfig, override: ClaudnaConfig) -> ClaudnaConfig: ...   # = _merge_fieldwise: override wins when not None
def _check_claudna(cfg: ClaudnaConfig, where: str) -> ClaudnaConfig: ...   # harvest is True and session_summary is False → ValueError(f"'{where}': claudna.harvest: true needs session_summary unset or true — clauDNA's gate never summarizes a bot on harvest alone")
# BotConfig (after claudron_session_loop, :776):
    claudna: ClaudnaConfig = field(default_factory=ClaudnaConfig)
# _coerce_bot (beside observability, :1884-1887) — the cross-field check runs on the MERGED value, where it is known:
    claudna=_check_claudna(_merge_claudna(_parse_claudna(defaults.get("claudna"), "defaults"), _parse_claudna(raw.get("claudna"), f"bots.{name}")), f"bots.{name}"),
```

  `_parse_strict_mapping(raw, where, fields)` and `_merge_fieldwise` are the one strict-mapping parser/merger P2 Task 1
  also needs; whichever of P2/P3 lands first carries them, the registry of strict-mapping fields that
  `scalar_config_origin` (`config.py:2271-2297`; `NotImplementedError` for mappings today) reads, and the one-level
  `_config_field` change in `commands/config_explain.py` (its message split into "not a field" vs "a mapping sub-field");
  the second rebases onto it (X13). `claudna` registers `session_summary` and `harvest`, so `config explain
  bots.<b>.claudna.harvest` names its source. Shape, decided and shown once in `fleet-yaml-schema.md`: the **nested**
  `claudna: {session_summary, harvest}` mapping holds what clauDNA reads at session open; `claudna_version` stays the
  flat install pin it is (`:127`; boot-time `plugin_ensure`, a different lifecycle) — no alias; Claudosseum's
  `CLAUDNA_TELEMETRY` stays an operator `env:` knob outside the mapping, and clauDNA's docs that said Claudlobby sets it
  are corrected in the P3 clauDNA plan (its Task 6).

- [ ] **Step 3.** `compose_bot_conf` (`composer.py:1319-1330`): the `# Ecosystem` header becomes unconditional, followed by
  `export CLAUDNA_STATE_DIR="$BOT_DIR/data/claudna"` (why: the per-bot root is a composition invariant, not a knob — a shared
  `~/.claudna` strands sessions, F14), then the existing three optional lines, then — **always** —
  `export CLAUDNA_SESSION_SUMMARY={'1' if (session_summary or harvest) else '0'}` (unset is `0`, so an unarmed bot's
  segments export as `skipped_reason: disabled`), and `export CLAUDNA_HARVEST={'1' if harvest else '0'}` when `harvest`
  is not `None` (the shell-boolean rule, `:1214-1218`). Comment the block with the spec reference, the gate order
  (`project.py:276-287`) and that clauDNA reads these at session open.
- [ ] **Step 4 (docs).** `fleet-yaml-schema.md`: the shape block (`:127-130`) gains `claudna: { session_summary: true|false,
  harvest: true|false }  # OPTIONAL — STRICT bools; see bots.<name>.claudna`; a new `### bots.<name>.claudna /
  fleet.defaults.claudna` section in the paired-heading form of `heavy_slot` (`:547-564`) after `:928`, naming the two env
  vars, that `CLAUDNA_STATE_DIR` and `CLAUDNA_SESSION_SUMMARY` are always composed, which reason an unarmed bot's segments
  export with (`disabled`; `harvest: true` implies summaries), the spend, the "Claudron ≥ 0.8.0 on the host first (PR 6a)"
  line for `harvest`, that `claudna_version` is a separate install pin, and the canary paragraph. `fleet.yaml.example:333-337`
  gains a commented `claudna:` block. `environment-variables.md` Ecosystem table (`:181-185`) gains three rows:
  `CLAUDNA_STATE_DIR` (source: *composed, always* — `$BOT_DIR/data/claudna`), `CLAUDNA_SESSION_SUMMARY` (source: *composed,
  always* — `0` unless `bots.<name>.claudna.session_summary`/`harvest` or the `defaults.claudna.*` twin arms it) and
  `CLAUDNA_HARVEST` (source: `bots.<name>.claudna.harvest` / `defaults.claudna.harvest`).
- [ ] **Step 5.** Verify: `./.venv/bin/pytest tests/test_composer.py tests/test_config.py tests/test_freshbox_selfcontained.py -q`.
  Commit: `feat(compose): per-bot CLAUDNA_STATE_DIR, and the claudna knobs composed from a strict fleet-level mapping (#2145 P3)`.

### Task 2: the two opt-in switch rows and the `session-export` opt-out row

**Files:** `claudlobby/switches.py`, `tests/test_switches.py`, the three generated tables (`documentation/fleet-yaml-schema.md:238-261`,
`system-yaml-schema.md:141-174`, `architecture/observable-plane.md:386-402`).

- [ ] **Step 1 (tests first).** `tests/test_switches.py:98-110` allowlist gains `"claudna-session-summary"` and
  `"claudna-harvest"` with the comment `# model spend — session-digest's class, which these replace`; a new test asserts both
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
           takes_effect_off="no summary from the bot's next session on; sealed segments then export as skipped_reason: disabled"),
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
`documentation/integrations/claudron-integration.md`.

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
  `claudna.harvest` only after this PR is on the host" line (6b documents the same from its side).
- [ ] **Step 5.** Verify: `./.venv/bin/pytest tests/test_claudron_compat.py tests/test_claudron_loop.py tests/test_doctor*.py
  tests/test_validate_warning_discipline.py -q`; with `pip install -e '.[dev,vault]'`: `pytest -q -m "vault and not quarantine"`
  (`TestSnippetParity`, `:452-464`, unchanged snippet shape). Commit (PR 6a): `chore(claudron): pin v0.9.0, a compat row for
  clauDNA harvest, and config plan warns on a session loop over an unmigrated vault`.

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
  one item → that item is re-emitted as `{status: "skipped", skipped_reason: "unexportable"}` (identity and segment fields only),
  the sid is acked, the tick says so; (l) three consecutive failed ticks for one bot → one `export_stalled` system event
  (`critical`) on the bot's actor, the fourth tick emits none (one per stall; a tick that acks re-arms it); (m) `doctor`'s
  `session-export` rung reads the per-bot files and warns on `consecutive_failures >= 3` or `last_ack_at` older than 24 h while
  `pending > 0`, and says nothing for a bot that has never sealed a segment.
- [ ] **Step 2 — `claudlobby/session_export.py`** (stdlib + the plane):

```python
ENTRYPOINT_SCHEMA = "claudna.entrypoint/1"; EXPORT_SCHEMA = "claudna.export/1"; CONSUMER = "claudlobby"
EMITTER = "session-export"; DATA_SCHEMA = "session_summary/1"; EXPORT_TIMEOUT_S = 60.0
DATA_CAP_BYTES = cap_for("system", "data") - 4_096      # registries.py:81 — 16 384 today, so 12 288; the headroom is the envelope around data.data
MIN_PLUGIN_VERSION = (0, 28, 0)                          # the clauDNA release that writes entrypoint.json (X18)
STALL_AFTER = 3                                          # consecutive failed ticks per bot before one export_stalled event
CAPS = {"title": 300, "intent": 300, "outcome": 300, "arc": 2_000, "list_items": 12, "list_item": 300, "skipped_reason": 64}

@dataclass(frozen=True) class Entrypoint: python: str; entrypoint: str; plugin_version: str | None; runtime: str | None
@dataclass(frozen=True) class BotOutcome: bot: str; emitted: int; duplicate: int; spooled: int; unexportable: int; acked: dict[str, int]; skipped: str | None; failed: str | None

def read_entrypoint(state_dir: Path) -> Entrypoint | str: ...          # the str is the skip reason: no_entrypoint | unknown_schema | old_entrypoint | stale_entrypoint
def export_argv(entry, state_dir) -> list[str]: ...                     # [entry.python or sys.executable, "-S", entry.entrypoint, "export", "--root", str(state_dir), "--consumer", CONSUMER, "--include-skipped", "--json"]
def ack_argv(entry, state_dir, sid, through) -> list[str]: ...
def summary_record(item: dict, *, fleet: str, bot: str, now: datetime) -> tuple[dict, datetime]: ...   # (data.data, occurred_at) — the spec's field block; skipped → turns/transcript_bytes/journey/blocks/procedures/producer None
def unexportable_record(item: dict, *, fleet: str, bot: str, now: datetime) -> tuple[dict, datetime]: ...   # identity + segment fields, status "skipped", skipped_reason "unexportable" — the cursor must advance
def bound(record: dict) -> dict: ...                                    # CAPS, then drop arc, then lists, until len(json.dumps(...).encode()) <= DATA_CAP_BYTES
def _system_event("session_summary", *, fleet, bot, sid, seg, occurred_at, observed_at, data) -> dict: ...   # calls the P1 fleet-event row helper; the literal first argument is what tests/test_event_type_registry.py scans; _system_event("export_stalled", …) is the second call site
def export_bot(root, fleet, bot, state_dir, *, run=subprocess.run, now=None) -> BotOutcome: ...   # read → export → per sid: per item emit_batch(root, [event], require_commit=False) → ack next[sid] → next sid; a failure stops this bot and is recorded
```

  `summary_record`: `status` = `"skipped"` when `item["summary"] is None` else `"ok"`; `skipped_reason = item.get("skipped", {}).get("reason")`
  (an unknown reason is still a skip — A-F10, epic §16, may add `no_summarizer`); `runtime = item["session"].get("runtime") or "claude"`;
  `session_uid = derive_session_uid(sid, runtime)`; on an `ok` item `turns = summary["input"]["turns"]`,
  `transcript_bytes = range.end - range.start`; **on a skipped item they are `None`** — `summary` is `null`, so there is nothing to
  read them from (X19; A-F6, epic §16, would have the job compute them from a `segment.transcript` pointer — held, see plan 5
  Task 1); `sealed_at`/`sealed_by`/counts from `item.get("segment")` (absent on an older clauDNA → `None`; `occurred_at` then
  falls back to `session.closed_at`, else `now`); `blocks = {"count": len(blocks), "kinds": Counter(b["home"] for b in
  summary["blocks"])}` — the block item's type key is `home` (`claudna.segment-summary/1`, `segment-summary.schema.json`);
  `procedures = len(...)`; `producer` verbatim. `occurred_at` is re-emitted as an aware ISO instant (`EmitRequest.occurred_at`
  is `AwareDatetime`, `contracts.py:811`); an unparseable `sealed_at` falls back to `now` and stays raw inside `data`.
  `_system_event` calls the one Python fleet-event row helper the P1 Claudlobby plan adds (Half B; `claudlobby/plane/fleet_events.py`
  or the name that plan settles on — never a third hand-rolled `{"event_type": "system", "source_ref": "fleet-events:…",
  "payload": {…, "data": {"source", "legacy_ts", "data"}}}`), with `"fleet": fleet` (alias; ingest resolves the fleet uid) and
  `payload.subject = f"bot:{fleet}/{bot}"` (alias form, `ingest.py:313-322`). Interpreter: the hook's own recorded `python`
  (proven to run the store on this host), else `sys.executable` — never a PATH lookup, because a timer unit's environment is
  closed. **Why in-process (X22):** resident services post to the daemon socket (`daemon.py:1-30` — the socket front of the
  same `emit_batch`); one-shot timer ticks and CLI doors call `emit_batch` in-process, as the task doors do
  (`task_operations.py:351`). **The job cannot wedge (X20):** one `emit_batch` per item — a batch that mixes an already-ingested
  id with a new one is refused as mixed state (`ingest.py:598-605`) and would hold the cursor forever (the alternative, equally
  acceptable: pre-read `ingest_ledger` for the derived `event_id`s and emit only the unseen, still one commit per item); ack
  per `sid` right after that session's items land; a `committed`, `duplicate` or `spooled` outcome (`require_commit=False`,
  daemon down: durably staged) all count as landed and **are acked**; a deterministic refusal of one item (`ContractViolation`,
  or `RuntimeError` from `_verify_duplicates`) is re-emitted as `unexportable_record` so the cursor advances and the item stays
  visible as a `skipped` row; a transient exception (`sqlite3.Error`, `OSError`) stops **this bot** for this tick, acks nothing
  further for it, and increments `consecutive_failures` in `<fleet_state>/session-export/<bot>.json` (`paths.fleet_state`,
  `paths.py:673`: `{"last_tick", "last_ack_at", "pending", "consecutive_failures", "last_skip", "last_failure"}`); at
  `STALL_AFTER` the tick emits one `export_stalled` system event (`critical` — the `reload_failed` precedent, `registries.py:135`)
  on the bot's actor and re-arms only after a tick that acks; a skip reason (`no_entrypoint` … `stale_entrypoint`) is **not** a
  failure (the bot has nothing to export yet) and is recorded as `last_skip`.
- [ ] **Step 3 — the command and the timer.** `claudlobby/commands/session_export.py`: `tick(args)` (the `task_recheck.tick`
  shape, `:202-215`): `SESSION_EXPORT_ENABLED == "0"` → `print("session-export: OFF here (SESSION_EXPORT_ENABLED=0); nothing is
  exported")`, return 0; else `resolve_operation_scope(root=args.root, fleet=args.tick_fleet)` (`operation_context.py:222-228`),
  `state_dir = selected.paths.bot_runtime(bot_id) / "data" / "claudna"` per bot (`paths.py:686`), one `export_bot` per bot, one
  line per bot (`session-export: <bot>: 3 emitted (1 duplicate), acked through seg 4` / `skipped: stale_entrypoint <path>`), JSON
  `{"fleet", "bots": [BotOutcome…]}`, exit 0 on every operating path, 2 only for a malformed call; after each bot the tick
  rewrites `<fleet_state>/session-export/<bot>.json` (the doctor rung's read-only source); `--dry-run` runs the export and
  prints the records without emitting or acking — the operator's hand run, **never** the monitor's door (it spawns every bot's
  export from the caller's session; the monitor reads `host doctor`'s rung and `event list`, Task 5). `commands/_parsers.py`
  beside `:66-68`:
  `sub.add_parser("_session-export-tick", help=argparse.SUPPRESS)` with `tick_fleet` and `--dry-run`, `set_defaults(func=_command("session_export", "tick"))`.
  `claudlobby/system.yaml` after `task-recheck` (`:538`), with a comment in the file's register (why it ships on: spends nothing,
  deletes nothing, sends nothing; the spend is clauDNA's and gated by Task 2's switches): `session-export: { script:
  "$CLAUDLOBBY_CLI --root $CLAUDLOBBY_ROOT _session-export-tick", interval: 900, type: oneshot }`. Regenerate `system.yaml.example`
  with the recipe at `tests/test_fleet_mission.py:228-231`; update the composed-timer-set pins in `tests/test_composer.py`
  (grep `task-recheck` there). The unit carries `Environment=SESSION_EXPORT_ENABLED=…` from `FLEET_JOB_ARMING` by construction.
- [ ] **Step 4 — registry and its gate.** `registries.py`, after `:204`: `# #2145 F6: one per sealed clauDNA segment, recorded by
  the session-export fleet job from the store's export; the monitor's substrate, never an alert. session_digest above stays
  registered so history classifies.` / `"session_summary": "notice",` and, in the critical block beside `reload_failed` (`:135`):
  `# #2145 P3: the session-export job failed STALL_AFTER ticks in a row for one bot — its cursor is not moving; one per stall.` /
  `"export_stalled": "critical",`. `tests/test_event_type_registry.py`: `PY_WRITERS` (`:275-278`) gains
  `"claudlobby/session_export.py": "_system_event"`; `:323` drops `session_digest` (its shell writer is gone) and `:328-330`
  gains `session_summary` and `export_stalled`; `:396` drops `RS + "transcript-digest.sh"`.
- [ ] **Step 5 — the doctor rung.** `doctor.check_session_export(fleet, paths, report)` beside `check_switches` (`doctor.py:1099`):
  for each bot, read `<fleet_state>/session-export/<bot>.json`; absent → nothing (never ran, or nothing sealed); `consecutive_failures
  >= STALL_AFTER` → warn `session-export-stalled` with `last_failure`; `pending > 0` and `last_ack_at` older than 24 h → warn
  `session-export-cursor-age` naming the bot and the age; a `last_skip` → an info line naming the reason (a stale entrypoint is the
  operator's cue to restart the bot). This is the monitor's read-only door for "is the export moving" (Task 5).
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
| `:82-90` | the job ships on: zero rows + no error means no bot sealed a segment in the window, or the fleet set `SESSION_EXPORT_ENABLED=0`, or every bot's entrypoint is stale/old — say which, from the **read-only** door `claudlobby host doctor` (the `session-export` rung names per-bot skips, cursor age and stalls; never `_session-export-tick --dry-run`, a suppressed private command that spawns every bot's export from the monitor's session). **A second stop rule (B5):** every row in the window `skipped` → print `COVERAGE: summaries armed on 0 of N bots (all rows skipped: <reason tally>)` and stop — identity and volume are real, but there is no journey to reason over. Week one reads exactly that way on most fleets: N bots × status rows, journeys only where `claudna.session_summary` is armed |
| `:102,113` | `by_status` keeps working; counts are `ok · skipped` (no `error` class) |
| `:115-116` | "`skipped` means the summarizer did not run: `skipped_reason` names why — `disabled` (summaries off for the bot: the composed default, `CLAUDNA_SESSION_SUMMARY=0`), `headless` (a run with the variable unset — not a composed bot), `trivial`, `no_transcript`, `gave_up`, `retired`, `unexportable` (the job could not record the item); any other value is still a skip (A-F10, epic §16, may add `no_summarizer`). Not a failure." |
| `:120-121,125-133` | rubric → `journey.title/intent/outcome/done/in_progress/next`; volume jq sums `prompts`, `failures` over every row and `turns`, `transcript_bytes` over `ok` rows only (both are `null` on a skipped row — the template says `turns · bytes: ok rows only`); drop `tool_calls` — its successor is not promised here: when P2's plane leg lands, *that* PR grants `fleet-digest` the `plane samples` door (`SKILL.md:5-8` grants `jq`, `python3`, `event list` only) and adds the `session.tool_calls` step (A-F3's sequencing, epic §16); friction jq selects `status=="ok"` rows with `journey.outcome != "completed"` or `failures > 0` or non-empty `journey.next`, keeping `session_id, bot, fleet, ts` |
| `:173-174,187-192` | cut order: `skipped` rows (count only) first, then `ok` rows with an empty `journey`; template `rows: N ok · N skipped`, `VOLUME: sessions · turns · prompts · failures` |
| `fleet-observe/SKILL.md:34-35,39,43-49,52` | `failed`/`would_change` → `journey.outcome` not `completed` + `journey.next`; `worked`/`reusable` → `journey.done` + `blocks.count/kinds`; "rubric left empty" (`:43-45`) → a `skipped_reason: disabled` row on a substantial session is **not an instrument failing** — the instrument is off by composition, which `:47-49` ("a gap you can name is a finding") already covers: name it once as *coverage* ("summaries armed on k of N bots"), never per session; the stop rule mirrors `fleet-digest`'s (all rows skipped → the coverage line, stop); token bloat → `transcript_bytes` and `prompts` over `ok` rows (tool totals come with P2's plane leg, see above) |
| `fleet-monitoring.md:39-41,98,102-115,119-122` | `:39-41` "Nothing watches for sessions ending … no poller" → "A session ending is still an event the session reports — clauDNA's SessionEnd seals the segment inside the session; the plane learns of it when the `session-export` timer reads the store's export door (every 15 min): a poll of a contract door, never of transcripts or liveness"; source row → `session_summary`; the contract block becomes the spec's field table (identity `ts · session_id · session_uid · runtime · bot · fleet`; `status` + `skipped_reason`; volume `turns · transcript_bytes` (`null` on skipped) `· prompts · skills · failures · interrupts`; `journey.*`; `blocks.count/kinds`, `procedures`; `producer.model/duration_ms/cost_usd`; `seg · sealed_at · sealed_by`); the dormancy paragraph → "the job runs by default; a bot with summaries off still yields a `skipped` row, so an **empty** window means no sealed segment, a disabled job, or a stale entrypoint — name which (`host doctor`'s `session-export` rung)"; cite #1456/#1503: the digest's rows never reached this reader |
| `ai-platform-monitor.md:22` | "The plane's `session_summary` events (`claudlobby event list --type session_summary`) \| One per sealed segment — the journey (title, intent, outcome, done, next) when summaries are on for the bot; identity and volume always" |

  File now, independent of P3 (PC): the reader-filter defect — `plane-readers.py:1155-1164`'s `fleet-events:` filter silently
  drops rows other writers stamp differently (#1456/#1503 saw the symptom as an empty window) — one issue, so the next writer
  learns it from the registry gate, not from a monitor that reads nothing.
- [ ] **Step 3.** Verify: `./.venv/bin/pytest tests/test_no_retired_digest_reference.py tests/test_skill_ref_resolution.py -q`.
  Commit: `docs(library): the monitor reads session_summary — fleet-digest, fleet-observe, fleet-monitoring, ai-platform-monitor re-pointed`.

### Task 6: retire `transcript-digest.sh` and `plane-session-start.sh`, every touchpoint

**Files:** deletions `claudlobby/_runtime_scripts/transcript-digest.sh`, `claudlobby/_runtime_scripts/plane-session-start.sh`,
`tests/test_transcript_digest.sh` (auto-collected by `tests/test_sh_suites.py:33` — deleting it is the change),
`tests/test_transcript_digest_isolation.py`, `tests/test_plane_session_hook.py`; edits listed below (including `claudlobby/plane/ids.py:25-29`).

- [ ] **Step 1 (test first).** A new test in `tests/test_composer.py`: no composed `settings.local.json` names either script and
  the package `system.yaml` has no `SessionEnd` entry and no `plane-session-start` `SessionStart` entry; `grep -rn
  'transcript-digest\|plane-session-start\|\.plane-session\|SESSION_DIGEST' claudlobby/ tests/ harness/ library/ documentation/
  CLAUDE.md AGENTS.md system.yaml.example fleet.yaml.example | grep -v documentation/plans/ | grep -v system-map-2026-07-30`
  is the checklist criterion (empty).
- [ ] **Step 2 — code and config.** `claudlobby/system.yaml:402-425` both comment+hook blocks removed; `system.yaml.example`
  regenerated. `switches.py:529-540` row removed; `:397` comment names `spindown-receipt` only. `isolation.py:100-103` docstring:
  "The one copy; the shell fallback retired with `transcript-digest.sh` (#2145 P3)". `harness/validate-bot-change.sh:3576`
  loop → `update-siblings claudna-harvest code-audit-sweep`; `:3580` → grep the arm line `host doctor --switches` prints for
  `claudna-harvest` (`bots.<bot>.claudna.harvest: true …`).
- [ ] **Step 3 — tests.** `tests/test_switches.py:99,106` (drop `session-digest`, fix the comment), `:357-358` (resolve
  `OBSERVABILITY_UNASSIGNED_CHECK=1` / `worker-unassigned` instead), `:417`, `:734-755` (`test_the_four_corrected_carriers`:
  `spindown-receipt` keeps the BOT_CONF assertions; the docstring loses the digest), `:804,961` comments;
  `tests/test_fleet_claude_bin.py:188`; `tests/test_plane_emit_class.py:414`; `tests/test_plane_gauntlet_doors.py:27` and the
  comment at `:148`; `tests/test_heavy_slot_match.py:158` → `-k boot_admission`. `tests/test_event_type_registry.py:323,396` were
  done in Task 4. `tests/test_system_event_retention.py:74` **unchanged**.
- [ ] **Step 4 — docs and indexes.** `environment-variables.md:199` row removed and `:189-195` reworded (the opt-in example is
  now `claudna.*`, carried by composition, not `env:`); `fleet-update-lifecycle.md:422-428` → the digest retired; the
  `SPINDOWN_RECEIPT_ENABLED` half of the anecdote stands; `testing-plane-isolation.md:82` drops `transcript-digest`;
  `system-yaml-schema.md:388-389` rows removed and `:505-510` → "no opt-in self-gate remains among the composed hooks; clauDNA's
  `CLAUDNA_*` gates are composed from `bots.<name>.claudna`", roster `:425-428` gains `session-export | interval: 900 | (absent —
  enrolled); the private tick skips on SESSION_EXPORT_ENABLED=0`; `plane/ids.py:25-29`'s comment ("minted fresh per process at SessionStart, never derived") → "`process_uid` has no minter since `plane-session-start.sh` retired (#2145 P3); the prefix stays registered so historical `proc_` rows classify" — `"process": "proc_"` is **kept** (B6); `observable-plane.md:67-69` → "Session uids are transcript-stable:
  `ids.derive_session_uid(id, runtime)` is the one implementation (#2145 F2); the bash mirror retired with `plane-session-start.sh`",
  `:212` removed, `:214` → ``| `claudlobby _session-export-tick` (fleet timer invokes the selected CLI) | one `session_summary`
  system event per sealed clauDNA segment on the bot's actor — identity, volume, the journey and block tally when summaries are
  on, `skipped_reason` otherwise; `data.session_uid` is the F2 uid | `SESSION_EXPORT_ENABLED=0` in the fleet-tier `.env`;
  `PLANE_EMIT_DISABLED=1` |``; root `CLAUDE.md:124,140` rows deleted (the tick has no launcher, like `task-recheck`);
  `_runtime_scripts/CLAUDE.md:69,116` rows deleted; `cp CLAUDE.md AGENTS.md` at both levels (`tests/test_instruction_budget.py`).
- [ ] **Step 5 — the v2 ruling.** A deliberate duplicate of plan 2 Task 5 Step 1 — a safety net, since the two PRs are authored
  apart; if P1 already wrote it, this step is a no-op (B6). If the P1 PR did not: `2026-08-18-observable-plane-design-v2.md:606` gains one dated sentence:
  "**Amended 2026-10 (#2145 §1.1):** the SessionStart hook and `process_uid` minting are superseded — doors derive the uid from
  the caller's own session id with `ids.derive_session_uid`; `plane-session-start.sh` retired in P3." `:588` (Phase 3) gains the
  LangSmith supersession if absent.
- [ ] **Step 6.** Verify: `./.venv/bin/pytest tests/test_sh_suites.py tests/test_switches.py tests/test_plane_gauntlet_doors.py
  tests/test_plane_emit_class.py tests/test_fleet_claude_bin.py tests/test_instruction_budget.py tests/test_fleet_mission.py -q`;
  `bash harness/validate-bot-change.sh` directly (a harness-exercised script changed). Commit: `refactor(hooks): retire
  transcript-digest.sh and plane-session-start.sh — derive_session_uid is the one derivation (#2145 F2/F6/F15)`.

### Task 7: the F14 cutover runbook, CHANGELOG

> **Held:** this task implements F14 as ratified; amendment A-F14 (epic §16) proposes otherwise. Do not start it before the operator rules.

**Files:** `documentation/runbooks/claudna-state-dir-cutover.md` (new), `CHANGELOG.md`.

- [ ] **Step 1.** The runbook, operator-run, in this order: (1) pin the P3 clauDNA release on the fleet
  (`claudna_version`) and activate Task 1's composition; (2) restart bots through the proven sequence — each writes
  `<BOT_DIR>/data/claudna/entrypoint.json` at its first SessionStart; (3) `E="$(jq -r .entrypoint <any bot>/data/claudna/entrypoint.json)"`
  (fallback, **not a contract**: the plugin cache under the bot's `CLAUDE_CONFIG_DIR`, `composer.py:1046`); (4) for every open session
  in the old root: `python3 -S "$E" list --root ~/.claudna --json` → rows with `"status": "open"` → `python3 -S "$E" seal <sid>
  --root ~/.claudna` (the summary gate runs with the operator's shell env — leave `CLAUDNA_SESSION_SUMMARY` unset: bot sessions
  gate `headless`, no spend; `SETUP_GUIDE.md:320`); (5) **only when** step 6's condition holds — the service user runs no
  interactive clauDNA sessions on that host (they share the default root) — or after that user's interactive shell has been
  given its own root first (`CLAUDNA_STATE_DIR` in clauDNA's `shell/` aux additions, verified with one interactive session):
  `mv ~/.claudna ~/.claudna.retired-<date>` — **rename, never `chmod -R a-w`**: under a read-only root the hook exits 0 and logs
  (`plugin-hooks/session-store.sh:22-31,43-44`; `cli.py:177-213`) and 0.28's `entrypoint.json` write fails the same silent way,
  so a chmod'd root would quietly swallow every interactive session; leave the renamed root until `CLAUDNA_RETAIN_DAYS` (30,
  `retention.py:42`) has passed since the last seal; (6) remove it. Nothing in the composer or the job reads the old root.
  (A-F14, epic §16: if per-bot roots exist only so the job can run per bot, one shared store grouped by `actor.bot_id` would
  avoid this cutover — held.)
- [ ] **Step 2.** `CHANGELOG.md` `[Unreleased]`: one `### Added — …` for the job and event (with the consumer mapping), one
  `### Changed — …` per Task 1/2/3 (the pin entry in the shape of `:323-325`), one `### Removed — …` for the two hooks naming
  F15's waiver and the F2 single implementation. Commit: `docs: the clauDNA state-dir cutover runbook and the P3 changelog`.

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
- [ ] **Drift gate (X17).** `claudlobby/conformance.py`'s clauDNA leg already resolves and clones the pinned clauDNA
  (`resolve_claudna_ref`, `:112-131`; CI pins `CLAUDNA_REF`). It gains one cell: vendor `schemas/export.schema.json` from the
  clone into `claudlobby/contracts/claudna-export.schema.json` (the way clauDNA vendors `contracts/claudron.json`; the cell fails
  when the vendored copy differs from the clone's), build a fixture store with the clone's own `tests/conftest.py` helpers
  (`segment_summary`/`complete_segment`) or a checked-in fixture tree, run
  `python3 -S <clone>/lib/claudna/session_store export --root <fixture> --consumer claudlobby --include-skipped --json`, validate
  the envelope against the vendored schema, and feed every item through `session_export.summary_record` — one gate on the real
  envelope, not on canned fixtures; `tests/test_session_export.py`'s canned envelopes are generated from that fixture once and
  checked in.
- [ ] **Mutants** (committed code): the ack before the emit; a two-item `emit_batch` (one seen, one new — ingest refuses it as
  mixed state and the cursor never moves); an item-level `ContractViolation` with no `unexportable` re-emit (the cursor wedges);
  a flat `data` (reader renders `{}`); `source_ref` without the `fleet-events:` prefix (reader returns nothing);
  `derive_uid("ev_", …)`; the bound dropped (a 50 KB arc → `detail_truncated=1`); a stale entrypoint "guessed" from the plugin
  cache; the migration warning firing on D009 (6a); `CLAUDNA_STATE_DIR` conditional on `claudna_version`;
  `CLAUDNA_SESSION_SUMMARY` absent for an unarmed bot; a `"true"` string arming `claudna.harvest`.
- [ ] **The two-leg gate** (rc + scoped names + count line, against Task 0), CI on Linux, `bash harness/validate-bot-change.sh`
  directly.
- [ ] **Canary root (mandatory runtime validation).** One host, one real Claude bot with `claudna.session_summary: true` (one
  bot of many — the switch's own arm line) and a second with it unset; Claudron v0.9.0 installed; the vault migrated per Task 3's
  step. After a `/clear` or a SessionEnd and one tick (≤ 15 min): `claudlobby --json event list --type session_summary --since 1h`
  renders one row with `data.status == "ok"` and `data.journey.title` populated — **the thing the digest never achieved** — and one
  with `data.status == "skipped"`, `data.skipped_reason == "disabled"` (the composer wrote `CLAUDNA_SESSION_SUMMARY=0` into the
  unarmed bot's `bot.conf`, so the gate answers `disabled`, `project.py:276-287` — were it `headless`, the composition never
  reached the bot); `<BOT_DIR>/data/claudna/sessions/<sid>/consumers.json` shows `claudlobby.through_seg`;
  `<BOT_DIR>/data/.plane-session` is **not** rewritten by the new session; record whether `claude plugin update` left the prior
  `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>` directory on the bot host (X18 — plan 5's live check asks the same
  on a dev machine; the bot host is the one that matters); (6a) `config plan` on the canary fleet is silent with the migrated
  vault and warns `claudron-migration` (with the named fix) against a scratch clone left at format 2. Cite the observation in
  the PR body behind `no_names`.

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
  was chosen (both are `null` on a skipped row unless A-F6 is ratified). That is F6's coverage concern kept (F15), not a defect.
- **`tool_calls` is gone until P2** (`session.tool_calls` samples); `fleet-observe`'s token-bloat lens reads `prompts` meanwhile.
- **`config plan` probes `claudron doctor --json` once per distinct session-loop vault** (PR 6a; only with `claudron` on PATH;
  `CLAUDRON_DOCTOR_TIMEOUT_S`, 60 s, shared with doctor — Claudron #201 class); pending migrations, an old format and a timeout
  each **warn**, nothing here refuses — `config validate --strict` is the operator's refusal.
- **F14's "then remove"** assumes the service user runs only bots; the same default root serves that user's interactive
  sessions, so the runbook renames (never chmods) the old root only under that condition, or after the interactive shell has its
  own `CLAUDNA_STATE_DIR`. A-F14 (epic §16) asks whether the cutover is needed at all; Task 7 is held on it.
- **`config explain bots.<b>.claudna.*`** works through the strict-mapping provenance registry that whichever of P2/P3 lands
  first carries (X13); until then `scalar_config_origin` still raises for mappings (`config.py:2271-2297`) and the second PR
  rebases onto the first's helper — a merge-order fact, not a limitation of the design.

## Test Plan

Unit: `tests/test_session_export.py` (new — the per-item emit, the wedge test, `unexportable`, `export_stalled`, the null rule),
`tests/test_doctor*.py` (the `session-export` rung), `tests/test_composer.py` (the always-composed `CLAUDNA_SESSION_SUMMARY`,
the no-retired-hook test, timer-set pins), `tests/test_config.py` (the `claudna:` mapping and its cross-field check),
`tests/test_env_register.py` (the provenance cell), `tests/test_switches.py`, `tests/test_claudron_compat.py`,
`tests/test_claudron_loop.py` (validator warning cells; `TestSnippetParity` under the vault extra),
`tests/test_validate_warning_discipline.py`, `tests/test_event_type_registry.py`, `tests/test_no_retired_digest_reference.py`,
`tests/test_fleet_mission.py` (example pin), `tests/test_instruction_budget.py`; the conformance leg's export cell (Task 8).
Removed suites: `test_transcript_digest.sh`, `test_transcript_digest_isolation.py`, `test_plane_session_hook.py`. Bash: the harness
scenario through `harness/validate-bot-change.sh` directly. The two-leg gate against Task 0's before-leg; CI on Linux.

## Verification Checklist

- [ ] `grep -c 'export CLAUDNA_STATE_DIR="$BOT_DIR/data/claudna"' <every composed bot.conf>` prints 1; `grep -c 'export CLAUDNA_SESSION_SUMMARY='`
  prints 1, and the value is `0` on every bot with neither `claudna.session_summary` nor `claudna.harvest` armed.
- [ ] `tests/test_session_export.py` wedge cell: seg 1 emitted unacked, seg 2 sealed → seg 2 `committed`, seg 1 `duplicate`, both
  acked; the two-item-batch mutant holds the cursor (shown, restored).
- [ ] `grep -rn 'transcript-digest\|plane-session-start\|\.plane-session\|SESSION_DIGEST\|session_digest' claudlobby/ tests/ harness/
  library/ documentation/ CLAUDE.md AGENTS.md *.example | grep -v -e documentation/plans/ -e system-map-2026-07-30 -e CHANGELOG`
  returns only `registries.py:201-204`, `test_system_event_retention.py:74` and `test_no_retired_digest_reference.py`.
- [ ] `claudlobby host doctor --switches` names `claudna-session-summary`, `claudna-harvest` (off) and `session-export` (on); the
  three doc tables equal the render (`tests/test_switches.py:957-969`).
- [ ] `tests/test_claudron_compat.py::test_vault_pin_satisfies_compat_floor` is red at `@v0.6.1` (shown, restored) and green at `@v0.9.0`.
- [ ] `tests/test_session_export.py` read-side cell: `fleet_events(... event_type="session_summary")[0]["data"]["journey"]["title"]`
  is populated; the flat-`data` mutant renders `{}` (shown, restored).
- [ ] Harness: `session_summary` rows via `val_events`, acks after emits, no row on the second run, the stale-entrypoint skip line.
- [ ] Live, canary root: `claudlobby --json event list --type session_summary --since 1h` shows one `ok` row with
  `data.journey.title` and one `skipped` row with `data.skipped_reason == "disabled"`; `consumers.json` carries `claudlobby`;
  no fresh `.plane-session`; (6a) `config plan` warns `claudron-migration` with the `claudron doctor --fix` line against the
  format-2 scratch clone, and is silent after the migration.
- [ ] Conformance: the clauDNA leg's export cell runs the pinned clone's real door through `summary_record`, and the vendored
  `claudlobby/contracts/claudna-export.schema.json` equals the clone's `schemas/export.schema.json`.
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
- Do not point the monitor at `_session-export-tick --dry-run`; its read-only door is `host doctor`'s `session-export` rung.
- Do not `chmod -R a-w` the old `~/.claudna`: rename it, and only under the runbook's condition (F14 step 5).
- Do not read a worker's session uid from `.plane-session`; do not keep a bash derivation (epic §11).
- Do not refuse a plan on any doctor finding — a pending migration is a named warning (warn-never-fail; `--strict` is the
  operator's refusal) and D007–D010/structure findings are nothing; never run `claudron doctor --fix` from Claudlobby (it
  commits to a shared vault).
- Do not arm `claudna.*` through `bots.<name>.env` (`defaults.env` is not merged; the mapping is the one way).
- Do not touch `architecture/system-map-2026-07-30.md` or `CHANGELOG.md` history; do not remove `session_digest` or `tool_call`
  from the registry.
- Do not compose or validate a Codex bot here (F11).

## Context

area: compose / plane / library · effort: **M** per the epic index — 6b (Tasks 1–2, 4–8) is M, 6a (Task 3) is S plus the
operator-run migration · risk: medium (a fleet timer that spawns a subprocess per bot; a `config plan` probe of an external CLI
that warns; a library re-point that changes what the monitor reads) · priority: P3 · related: #2145 (epic), #1503 (the digest's
plane cutover), #1456 (the rows the reader dropped), #1961 (the waived comparison, F15), #785 (the monitor), Claudron #190/#201
(doctor), clauDNA plan 5 (`2026-10-04-runtime-neutral-observability-p3-claudna-export-contract.md`).

Spec spellings corrected here, decisions unchanged: `derive_uid("ev", …)` not `"ev_"` (`ids.py:57-60`); `source_ref` is the
fleet-event helper's (`fleet-events:` prefix; no `session-summary:<sid>/<seg>` sub-grammar — `event_id` is the dedup key);
`EmitRequest.occurred_at` is `contracts.py:811`; the test that binds the protocol rewrite to the registry entry is
`test_no_retired_digest_reference.py` plus `test_event_type_registry.py:323`, not gate (d) (`fleet-monitoring.md` is outside
`DOCS`, `:404-408`); the epic's P3 bullet says composition "refuses" a session loop on an unmigrated vault — this plan warns
(Task 3, B3), for the reasons given there.

Answered here, carried to the epic: *why the record rides into the plane rather than a thin row fetched through the export
door* — the four consumers are manager-bot skills whose only door is `claudlobby event list` (`fleet-digest/SKILL.md:5-8`),
so a record they cannot reach is no record; *why `session-export` ships ON* — it spends, deletes, mutates and sends nothing
(the Defaults rule); the spend is clauDNA's, behind Task 2's opt-in switches. Whether a 12-field record is the right first
cut is E11's question for the operator (epic §14).
