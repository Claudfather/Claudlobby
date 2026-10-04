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
> amendment); the P3 clauDNA release (plan 5: `export --include-skipped`, the item's `segment` object, `session.runtime`,
> `<CLAUDNA_STATE_DIR>/entrypoint.json`); Claudron `v0.9.0` (`6ca2b94`, tagged). Waits on canaries: none — this PR reads
> no `CLAUDE_*` variable and composes no Codex bot.

## Summary

Claudlobby stops writing session summaries and starts consuming clauDNA's: each bot gets its own clauDNA root
(`$BOT_DIR/data/claudna`, F14), a fleet timer `session-export` reads every bot's export door at the path clauDNA's own
hook records (F16) and turns each sealed segment — summarized or skipped — into one `session_summary` system event
shaped for the reader the four monitor consumers already use (F6), then acks. `transcript-digest.sh` and
`plane-session-start.sh` retire with every touchpoint, making `ids.derive_session_uid` the one derivation (F2). The two
model-spending clauDNA knobs become fleet-level opt-in switches, and the Claudron install pin moves `v0.6.1 → v0.9.0`
behind an operator-run vault migration and a `config plan` refusal scoped to exactly that. Deliberately left out: Codex
composition (F11, companion), P2's `session.tool_calls` samples (the consumers lose `tool_calls` until then),
`config explain` provenance for the new `claudna:` mapping (P2's telemetry mapping teaches `scalar_config_origin` first).

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

## Implementation Plan

### Dependencies

P1 Claudlobby merged (`derive_session_uid(id, runtime)`, `CLAUDLOBBY_RUNTIME`); clauDNA P3 released and pinned on the
canary fleet's `claudna_version`; Claudron `v0.9.0` installed on the canary host (`pip install -e '.[dev,vault]'` for the
parity leg). Order within P3: plan 5 (clauDNA) releases before Task 4 here can observe anything real.

### Blocks

The companion Codex epic (per-bot state dir and a runtime-neutral export consumer are prerequisites); P2's consumer note
(`tool_calls` returns as `session.tool_calls` samples).

### Steps

Seam: Tasks 1–3 (composition, switches, pin) are independent of Tasks 4–8 (job, event, consumers, retirement) and may ship
as PR 6a / 6b if review wants the pin bump on a host before the job lands.

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
  `claudna_version` too); `claudna: {session_summary: true}` on a bot composes `export CLAUDNA_SESSION_SUMMARY=1`; an
  explicit `false` composes `=0`; unset composes nothing (clauDNA's own default — off for bots — stands); `defaults.claudna.harvest:
  true` reaches every bot and a bot's own `false` wins (field-wise); a string `"true"` is a `ValueError` naming the key
  (`_strict_bool`); an unknown key under `claudna:` is refused naming the accepted keys. Existing tests pinning full
  `bot.conf` text gain the new line. `tests/test_freshbox_selfcontained.py` stays green (the value is `$BOT_DIR`-anchored).
- [ ] **Step 2.** `claudlobby/config.py`, beside `IsolationConfig` (`:658`):

```python
@dataclass(frozen=True)
class ClaudnaConfig:
    """clauDNA's two model-spending knobs, composed per bot (#2145 P3). None = unset: clauDNA's default (off for bots)."""
    session_summary: bool | None = None   # CLAUDNA_SESSION_SUMMARY — one Haiku call per sealed segment
    harvest: bool | None = None           # CLAUDNA_HARVEST — summaries + draft notes into the bot's vault via `claudron capture`

_CLAUDNA_KEYS = ("session_summary", "harvest")
def _parse_claudna(raw: object, where: str) -> ClaudnaConfig: ...   # unknown key → ValueError(f"'{where}': claudna: unknown key(s) … — accepted: …"); each value through _strict_bool
def _merge_claudna(default: ClaudnaConfig, override: ClaudnaConfig) -> ClaudnaConfig: ...   # field-wise, override wins when not None
# BotConfig (after claudron_session_loop, :776):
    claudna: ClaudnaConfig = field(default_factory=ClaudnaConfig)
# _coerce_bot (beside observability, :1884-1887):
    claudna=_merge_claudna(_parse_claudna(defaults.get("claudna"), "defaults"), _parse_claudna(raw.get("claudna"), f"bots.{name}")),
```

- [ ] **Step 3.** `compose_bot_conf` (`composer.py:1319-1330`): the `# Ecosystem` header becomes unconditional, followed by
  `export CLAUDNA_STATE_DIR="$BOT_DIR/data/claudna"` (why: the per-bot root is a composition invariant, not a knob — a shared
  `~/.claudna` strands sessions, F14), then the existing three optional lines, then
  `export CLAUDNA_SESSION_SUMMARY={'1' if … else '0'}` / `export CLAUDNA_HARVEST=…` when not `None` (the shell-boolean rule,
  `:1214-1218`). Comment the block with the spec reference and that clauDNA reads these at session open.
- [ ] **Step 4 (docs).** `fleet-yaml-schema.md`: the shape block (`:127-130`) gains `claudna: { session_summary: true|false,
  harvest: true|false }  # OPTIONAL — STRICT bools; see bots.<name>.claudna`; a new `### bots.<name>.claudna /
  fleet.defaults.claudna` section in the paired-heading form of `heavy_slot` (`:547-564`) after `:928`, naming the two env
  vars, that `CLAUDNA_STATE_DIR` is always composed, the spend, and the canary paragraph. `fleet.yaml.example:333-337` gains a
  commented `claudna:` block. `environment-variables.md` Ecosystem table (`:181-185`) gains three rows: `CLAUDNA_STATE_DIR`
  (source: *composed, always* — `$BOT_DIR/data/claudna`), `CLAUDNA_SESSION_SUMMARY` and `CLAUDNA_HARVEST` (source:
  `bots.<name>.claudna.*` / `defaults.claudna.*`).
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
           takes_effect_off="no summary from the bot's next session on; sealed segments then export as skipped"),
    Switch(key="claudna-harvest", scope=DOOR, polarity=OPT_IN, carrier=COMPOSE_BOT, config="claudna.harvest",
           why_opt_in="model spend (summaries) and writes draft notes into the bot's Claudron vault through claudron capture",
           what="opt this bot's sessions into clauDNA harvest: summaries on, typed blocks filed as (unverified) drafts "
                "in the vault CLAUDRON_VAULT_PATH names", compose_steps=…, takes_effect=…, takes_effect_off=…),
    Switch(key="session-export", scope=FLEET_JOB, polarity=OPT_OUT, carrier=ENV_FLEET, env="SESSION_EXPORT_ENABLED",
           job="session-export", plane=True,
           what="every 15 min, read each bot's clauDNA export door and record one session_summary plane event per sealed "
                "segment (summarized or skipped), then ack — the monitor's substrate (#2145 F6)"),
```

- [ ] **Step 3.** Regenerate the three tables: `claudlobby host doctor --switches --markdown` (pinned by `:957-969`).
  Verify: `./.venv/bin/pytest tests/test_switches.py -q`. Commit: `feat(switches): claudna-session-summary and claudna-harvest
  opt in per bot; session-export ships on with a loud off`.

### Task 3: the Claudron pin `v0.6.1 → v0.9.0`, the compat row that forces it, and the migration refusal

**Files:** `pyproject.toml`, `.github/workflows/conformance.yml`, `claudlobby/claudron_compat.py`, `claudlobby/doctor.py`,
`claudlobby/validator.py`, `tests/test_claudron_compat.py`, `tests/test_claudron_loop.py`, `tests/test_validator*.py`,
`documentation/integrations/claudron-integration.md`.

- [ ] **Step 1 (tests first).** `tests/test_claudron_compat.py`: with the new floor row, `test_vault_pin_satisfies_compat_floor`
  (`:115-152`) **fails at v0.6.1** (floor 0.8.0) and passes at v0.9.0; `test_integration_doc_version_claim_matches_pin`
  (`:155-174`) passes only once `:7` says `v0.9.0`; the doc-sync test sees the new table row. `tests/test_claudron_loop.py::TestValidator`
  (`:412-443`) gains, with a fake `claudron` on PATH that answers `status --json` (capabilities incl. `doctor`) and
  `doctor --json`: pending `m003` → one error naming the vault and `claudron doctor --fix --json --vault <vault>`; `vault_format
  2 < engine_format 3` with nothing pending → the same error; only D007/D008/D009/D010 and structure findings → **no error**;
  `doctor` not in capabilities, exit 3, or the 60 s timeout → a warning, no error; two bots on one vault → one probe.
- [ ] **Step 2 — the row and the shared prober.** `claudron_compat.py` gains a fifth `COMPAT_FLOOR` row:
  `ClaudronCapability(feature="clauDNA harvest from a composed bot (CLAUDNA_HARVEST=1 → claudron capture --stdin / amend)",
  requires="capture --stdin + amend --stdin with expect_trust (subject-filing), memory homes", default_order_release="0.8.0",
  probe=PROBE_VERB_PREFIX + "amend")` — why: Task 1 lets the composer arm harvest, so the install-time pin must provide what
  harvest calls (clauDNA's door is `claudron capture/amend`, epic §6 P3). Move `CLAUDRON_DOCTOR_TIMEOUT_S`, `_run`,
  `_claudron_probe` and the envelope parsing of `_claudron_doctor` (`doctor.py:746-866`) into one
  `claudron_compat.vault_migration_state(vault: str) -> VaultMigrationState(status: "ok"|"pending"|"old_format"|"unknown",
  pending: tuple[tuple[str, str], ...], vault_format, engine_format, detail: str)`, `functools.lru_cache`d on the resolved
  path; `doctor._claudron_doctor` renders its rows from it (same text as today). One prober, two readers.
- [ ] **Step 3 — the refusal.** `validator._validate_bots`, after the vault block (`:1358`), for each bot with
  `composer._session_loop_enabled(bot)` and a resolving vault: `state = vault_migration_state(vault)`; `pending`/`old_format` →
  `report.errors.append(f"bot '{bot_name}': claudron_vault_path '{…}' has {n} pending migration(s) ({ids}) / records vault format
  {v} but the engine is at {e} — composing its session loop against this vault would run hooks on an unmigrated vault. Once per
  vault, from one clone, a human runs `claudron doctor --fix --json --vault <vault>` (it commits), then pulls on every clone
  (vault-sync), then plans again.")`; `unknown` → `shared.add("claudron-doctor", …)` warning naming why. Never an error for
  D007–D010 or structure codes (they are the engine's and the human's). The same `validate()` backs `config plan`
  (`config_staging.py:174-176`), `config validate` and `doctor` — intended.
- [ ] **Step 4 — the bump.** `pyproject.toml:32` → `@v0.9.0`; `conformance.yml:37` comment; `claudron-integration.md:7`
  headline `(at v0.9.0)`, `:11` gains "`claudlobby config plan` refuses a session-loop bot whose vault has a pending migration or
  an unrecorded format (D001) and names the fix; every other finding stays doctor's", `:44` "the pinned **v0.9.0** ships the
  write-lock", `:48` `@v0.9.0 today`, the floor table (`:56-61`) gains the row; a new paragraph under "Version pin and bump policy":
  the 0.6.1→0.9.0 jump crosses `m003`/vault format 3 and the 0.8.0 index rebuild, the operator step, canary one host first.
- [ ] **Step 5.** Verify: `./.venv/bin/pytest tests/test_claudron_compat.py tests/test_claudron_loop.py tests/test_doctor*.py -q`;
  with `pip install -e '.[dev,vault]'`: `pytest -q -m "vault and not quarantine"` (`TestSnippetParity`, `:452-464`, unchanged
  snippet shape). Commit: `chore(claudron): pin v0.9.0, a compat row for clauDNA harvest, and config plan refuses a session loop
  on an unmigrated vault`.

### Task 4: the `session-export` fleet job and the `session_summary` event

**Files:** `claudlobby/session_export.py` (new), `claudlobby/commands/session_export.py` (new), `claudlobby/commands/_parsers.py`,
`claudlobby/plane/registries.py`, `claudlobby/system.yaml`, `system.yaml.example`, `tests/test_session_export.py` (new),
`tests/test_event_type_registry.py`, `tests/test_composer.py` (timer-set pins), `documentation/architecture/module-map.md:11`.

- [ ] **Step 1 (tests first).** `tests/test_session_export.py`, with a fake `run` (the injectable `subprocess.run`) returning
  canned envelopes and recording argv, a scratch plane (`_fleet_root` + `initialize_plane`, the `tests/test_plane_registry.py:694-702`
  shape): (a) an `ok` item and a `skipped` item become two `system` rows whose `payload.event == "session_summary"`,
  `source_ref == fleet-events:session-summary:<sid>/<seg>`, `event_id == derive_uid("ev", f"session_summary:{fleet}:{sid}:{seg}")`,
  `occurred_at == segment.sealed_at`, `observed_at` the run instant, subject `actor`/`bot:<fleet>/<bot>`, `data ==
  {"source": "session-export", "legacy_ts": …, "data": {…}}` with `data.data.session_uid == derive_session_uid(sid, runtime)`
  and `runtime` absent → `"claude"`; (b) **the read-side pin the digest never had:** `load_lib_module("plane-readers.py").fleet_events(conn,
  fleet, event_type="session_summary")` renders `data["status"]`, `data["journey"]["title"]`, `data["session_id"]` populated;
  (c) emit then ack: argv shows `export --root <state> --consumer claudlobby --include-skipped --json` then one `--ack --sid <sid>
  --through <seg>` per `next` entry, ack **after** emit; (d) run twice → second run's outcomes are all `duplicate`, no new rows,
  acks repeat (the cursor never moves back); (e) `emit_batch` raising → **no ack**; (f) missing `entrypoint.json` → skip
  `no_entrypoint`; `schema != "claudna.entrypoint/1"` → skip `unknown_schema`; `entrypoint` path absent on disk → skip
  `stale_entrypoint` (F16), each named in the tick's output and none emitting; (g) a 60 s `TimeoutExpired` → skip `export_timeout`;
  (h) the bound: a `journey.arc` of 50 000 chars and 200-item lists serialize under 12 288 bytes with `arc` cut first, lists second,
  and the JSON still parses; (i) `SESSION_EXPORT_ENABLED=0` → the loud line, exit 0, nothing run.
- [ ] **Step 2 — `claudlobby/session_export.py`** (stdlib + the plane):

```python
ENTRYPOINT_SCHEMA = "claudna.entrypoint/1"; EXPORT_SCHEMA = "claudna.export/1"; CONSUMER = "claudlobby"
EMITTER = "session-export"; DATA_SCHEMA = "session_summary/1"; DATA_CAP_BYTES = 12_288; EXPORT_TIMEOUT_S = 60.0
CAPS = {"title": 300, "intent": 300, "outcome": 300, "arc": 2_000, "list_items": 12, "list_item": 300, "skipped_reason": 64}

@dataclass(frozen=True) class Entrypoint: python: str; entrypoint: str; plugin_version: str | None; host: str | None
@dataclass(frozen=True) class BotOutcome: bot: str; emitted: int; duplicate: int; spooled: int; acked: dict[str, int]; skipped: str | None

def read_entrypoint(state_dir: Path) -> Entrypoint | str: ...          # the str is the skip reason: no_entrypoint | unknown_schema | stale_entrypoint
def export_argv(entry, state_dir) -> list[str]: ...                     # [entry.python or sys.executable, "-S", entry.entrypoint, "export", "--root", str(state_dir), "--consumer", CONSUMER, "--include-skipped", "--json"]
def ack_argv(entry, state_dir, sid, through) -> list[str]: ...
def summary_record(item: dict, *, fleet: str, bot: str, now: datetime) -> tuple[dict, datetime]: ...   # (data.data, occurred_at) — the spec's field block; skipped → journey/blocks/procedures/producer None
def bound(record: dict) -> dict: ...                                    # CAPS, then drop arc, then lists, until len(json.dumps(...).encode()) <= DATA_CAP_BYTES
def _system_event("session_summary", *, fleet, bot, sid, seg, occurred_at, observed_at, data) -> dict: ...   # the literal first argument is what tests/test_event_type_registry.py scans
def export_bot(root, fleet, bot, state_dir, *, run=subprocess.run, now=None) -> BotOutcome: ...   # read → export → events → emit_batch(root, events, require_commit=False) → ack each next[sid]
```

  `summary_record`: `status` = `"skipped"` when `item["summary"] is None` else `"ok"`; `skipped_reason = item.get("skipped", {}).get("reason")`;
  `runtime = item["session"].get("runtime") or "claude"`; `session_uid = derive_session_uid(sid, runtime)`;
  `turns = summary["input"]["turns"]`, `transcript_bytes = range.end - range.start`; `sealed_at`/`sealed_by`/counts from
  `item.get("segment")` (absent on an older clauDNA → `None`; `occurred_at` then falls back to `session.closed_at`, else `now`);
  `blocks = {"count": len(blocks), "kinds": Counter(<block type field>)}` (confirm the block item's type key against the released
  `segment-summary.schema.json` — one line); `procedures = len(...)`; `producer` verbatim. `occurred_at` is re-emitted as an
  aware ISO instant (`EmitRequest.occurred_at` is `AwareDatetime`, `contracts.py:808`); an unparseable `sealed_at` falls back
  to `now` and stays raw inside `data`. `_system_event` returns the spec's envelope with `"fleet": fleet` (alias; ingest resolves
  the fleet uid) and `payload.subject = f"bot:{fleet}/{bot}"` (alias form, `ingest.py:313-322`). Interpreter: the hook's own
  recorded `python` (proven to run the store on this host), else `sys.executable` — never a PATH lookup, because a timer unit's
  environment is closed. A spooled outcome (`require_commit=False`, daemon down) is durably staged and **is acked**; an exception
  from `emit_batch` acks nothing (the cursor holds; the deterministic `event_id` makes the retry a `duplicate`).
- [ ] **Step 3 — the command and the timer.** `claudlobby/commands/session_export.py`: `tick(args)` (the `task_recheck.tick`
  shape, `:202-215`): `SESSION_EXPORT_ENABLED == "0"` → `print("session-export: OFF here (SESSION_EXPORT_ENABLED=0); nothing is
  exported")`, return 0; else `resolve_operation_scope(root=args.root, fleet=args.tick_fleet)` (`operation_context.py:222-228`),
  `state_dir = selected.paths.bot_runtime(bot_id) / "data" / "claudna"` per bot (`paths.py:686`), one `export_bot` per bot, one
  line per bot (`session-export: <bot>: 3 emitted (1 duplicate), acked through seg 4` / `skipped: stale_entrypoint <path>`), JSON
  `{"fleet", "bots": [BotOutcome…]}`, exit 0 on every operating path, 2 only for a malformed call; `--dry-run` runs the export
  and prints the records without emitting or acking (the canary's hand run). `commands/_parsers.py` beside `:66-68`:
  `sub.add_parser("_session-export-tick", help=argparse.SUPPRESS)` with `tick_fleet` and `--dry-run`, `set_defaults(func=_command("session_export", "tick"))`.
  `claudlobby/system.yaml` after `task-recheck` (`:538`), with a comment in the file's register (why it ships on: spends nothing,
  deletes nothing, sends nothing; the spend is clauDNA's and gated by Task 2's switches): `session-export: { script:
  "$CLAUDLOBBY_CLI --root $CLAUDLOBBY_ROOT _session-export-tick", interval: 900, type: oneshot }`. Regenerate `system.yaml.example`
  with the recipe at `tests/test_fleet_mission.py:228-231`; update the composed-timer-set pins in `tests/test_composer.py`
  (grep `task-recheck` there). The unit carries `Environment=SESSION_EXPORT_ENABLED=…` from `FLEET_JOB_ARMING` by construction.
- [ ] **Step 4 — registry and its gate.** `registries.py`, after `:204`: `# #2145 F6: one per sealed clauDNA segment, recorded by
  the session-export fleet job from the store's export; the monitor's substrate, never an alert. session_digest above stays
  registered so history classifies.` / `"session_summary": "notice",`. `tests/test_event_type_registry.py`: `PY_WRITERS`
  (`:275-278`) gains `"claudlobby/session_export.py": "_system_event"`; `:323` drops `session_digest` (its shell writer is gone)
  and `:328-330` gains `session_summary`; `:396` drops `RS + "transcript-digest.sh"`.
- [ ] **Step 5.** `documentation/architecture/module-map.md:11` names `session_export.py` beside `task_recheck`. Verify:
  `./.venv/bin/pytest tests/test_session_export.py tests/test_event_type_registry.py tests/test_fleet_mission.py tests/test_composer.py -q`.
  Commit: `feat(plane): the session-export fleet job records one session_summary per sealed clauDNA segment, then acks (#2145 F6/F16)`.

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
| `:82-90` | the job ships on: zero rows + no error means no bot sealed a segment in the window, or the fleet set `SESSION_EXPORT_ENABLED=0`, or every bot's entrypoint is stale — say which (`claudlobby --json … _session-export-tick --dry-run` names skips) |
| `:102,113` | `by_status` keeps working; counts are `ok · skipped` (no `error` class) |
| `:115-116` | "`skipped` means the summarizer did not run: `skipped_reason` names why — `disabled` (summaries off for the bot: the default), `headless`, `trivial`, `no_transcript`, `gave_up`, `retired`. Not a failure." |
| `:120-121,125-133` | rubric → `journey.title/intent/outcome/done/in_progress/next`; volume jq sums `turns`, `transcript_bytes`, `prompts`, `failures` (drop `tool_calls`); friction jq selects `status=="ok"` rows with `journey.outcome != "completed"` or `failures > 0` or non-empty `journey.next`, keeping `session_id, bot, fleet, ts` |
| `:173-174,187-192` | cut order: `skipped` rows (count only) first, then `ok` rows with an empty `journey`; template `rows: N ok · N skipped`, `VOLUME: sessions · turns · prompts · failures` |
| `fleet-observe/SKILL.md:34-35,39,43-44,52` | `failed`/`would_change` → `journey.outcome` not `completed` + `journey.next`; `worked`/`reusable` → `journey.done` + `blocks.count/kinds`; "rubric left empty" → a `skipped_reason: disabled` row on a substantial session is an **instrument gap** (summaries off for that bot), not a finding; token bloat → `transcript_bytes` and `prompts` (tool totals return as P2's `session.tool_calls` samples) |
| `fleet-monitoring.md:98,102-115,119-122` | source row → `session_summary`; the contract block becomes the spec's field table (identity `ts · session_id · session_uid · runtime · bot · fleet`; `status` + `skipped_reason`; volume `turns · transcript_bytes · prompts · skills · failures · interrupts`; `journey.*`; `blocks.count/kinds`, `procedures`; `producer.model/duration_ms/cost_usd`; `seg · sealed_at · sealed_by`); the dormancy paragraph → "the job runs by default; a bot with summaries off still yields a `skipped` row, so an **empty** window means no sealed segment, a disabled job, or a stale entrypoint — name which" |
| `ai-platform-monitor.md:22` | "The plane's `session_summary` events (`claudlobby event list --type session_summary`) \| One per sealed segment — the journey (title, intent, outcome, done, next) when summaries are on for the bot; identity and volume always" |

- [ ] **Step 3.** Verify: `./.venv/bin/pytest tests/test_no_retired_digest_reference.py tests/test_skill_ref_resolution.py -q`.
  Commit: `docs(library): the monitor reads session_summary — fleet-digest, fleet-observe, fleet-monitoring, ai-platform-monitor re-pointed`.

### Task 6: retire `transcript-digest.sh` and `plane-session-start.sh`, every touchpoint

**Files:** deletions `claudlobby/_runtime_scripts/transcript-digest.sh`, `claudlobby/_runtime_scripts/plane-session-start.sh`,
`tests/test_transcript_digest.sh` (auto-collected by `tests/test_sh_suites.py:33` — deleting it is the change),
`tests/test_transcript_digest_isolation.py`, `tests/test_plane_session_hook.py`; edits listed below.

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
  enrolled); the private tick skips on SESSION_EXPORT_ENABLED=0`; `observable-plane.md:67-69` → "Session uids are transcript-stable:
  `ids.derive_session_uid(id, runtime)` is the one implementation (#2145 F2); the bash mirror retired with `plane-session-start.sh`",
  `:212` removed, `:214` → ``| `claudlobby _session-export-tick` (fleet timer invokes the selected CLI) | one `session_summary`
  system event per sealed clauDNA segment on the bot's actor — identity, volume, the journey and block tally when summaries are
  on, `skipped_reason` otherwise; `data.session_uid` is the F2 uid | `SESSION_EXPORT_ENABLED=0` in the fleet-tier `.env`;
  `PLANE_EMIT_DISABLED=1` |``; root `CLAUDE.md:124,140` rows deleted (the tick has no launcher, like `task-recheck`);
  `_runtime_scripts/CLAUDE.md:69,116` rows deleted; `cp CLAUDE.md AGENTS.md` at both levels (`tests/test_instruction_budget.py`).
- [ ] **Step 5 — the v2 ruling.** If the P1 PR did not: `2026-08-18-observable-plane-design-v2.md:606` gains one dated sentence:
  "**Amended 2026-10 (#2145 §1.1):** the SessionStart hook and `process_uid` minting are superseded — doors derive the uid from
  the caller's own session id with `ids.derive_session_uid`; `plane-session-start.sh` retired in P3." `:588` (Phase 3) gains the
  LangSmith supersession if absent.
- [ ] **Step 6.** Verify: `./.venv/bin/pytest tests/test_sh_suites.py tests/test_switches.py tests/test_plane_gauntlet_doors.py
  tests/test_plane_emit_class.py tests/test_fleet_claude_bin.py tests/test_instruction_budget.py tests/test_fleet_mission.py -q`;
  `bash harness/validate-bot-change.sh` directly (a harness-exercised script changed). Commit: `refactor(hooks): retire
  transcript-digest.sh and plane-session-start.sh — derive_session_uid is the one derivation (#2145 F2/F6/F15)`.

### Task 7: the F14 cutover runbook, CHANGELOG

**Files:** `documentation/runbooks/claudna-state-dir-cutover.md` (new), `CHANGELOG.md`.

- [ ] **Step 1.** The runbook, operator-run, in this order: (1) pin the P3 clauDNA release on the fleet
  (`claudna_version`) and activate Task 1's composition; (2) restart bots through the proven sequence — each writes
  `<BOT_DIR>/data/claudna/entrypoint.json` at its first SessionStart; (3) `E="$(jq -r .entrypoint <any bot>/data/claudna/entrypoint.json)"`
  (fallback, **not a contract**: the plugin cache under the bot's `CLAUDE_CONFIG_DIR`, `composer.py:1046`); (4) for every open session
  in the old root: `python3 -S "$E" list --root ~/.claudna --json` → rows with `"status": "open"` → `python3 -S "$E" seal <sid>
  --root ~/.claudna` (the summary gate runs with the operator's shell env — leave `CLAUDNA_SESSION_SUMMARY` unset: bot sessions
  gate `headless`, no spend; `SETUP_GUIDE.md:320`); (5) `chmod -R a-w ~/.claudna` and leave it until `CLAUDNA_RETAIN_DAYS` (30,
  `retention.py:42`) has passed since the last seal; (6) remove it — **only if** the service user runs no interactive Claude
  sessions on that host (they share the same default root). Nothing in the composer or the job reads the old root.
- [ ] **Step 2.** `CHANGELOG.md` `[Unreleased]`: one `### Added — …` for the job and event (with the consumer mapping), one
  `### Changed — …` per Task 1/2/3 (the pin entry in the shape of `:323-325`), one `### Removed — …` for the two hooks naming
  F15's waiver and the F2 single implementation. Commit: `docs: the clauDNA state-dir cutover runbook and the P3 changelog`.

### Task 8: gate, harness scenario, canary-root observation

- [ ] **Harness scenario** in `harness/validate-bot-change.sh` (the `val_scenario`/`harness_check` shape): compose a scratch
  fleet; assert `bot.conf` carries `export CLAUDNA_STATE_DIR="$BOT_DIR/data/claudna"` and no `SESSION_DIGEST`; the composed
  `settings.local.json` names neither retired script; seed `runtime/bots/<b>/data/claudna/entrypoint.json` pointing at a stub
  `session_store/__main__.py` that prints a canned `claudna.export/1` envelope (one `ok` item, one `skipped` item, both with
  `segment{}` and `session.runtime`) and appends `--ack` argv to a file; `"$VAL_CLI" --root "$ROOT" _session-export-tick "$FLEET"`;
  `val_events "$ROOT" "$FLEET" "$BOT" session_summary` shows two rows with `"status":"ok"` / `"status":"skipped"` and a
  `session_uid`; the ack file has exactly the `next` entries; a second run adds no row; a stale `entrypoint` → the skip line and
  zero rows; `SESSION_EXPORT_ENABLED=0` stamped on the unit → the OFF line. Record the harness's new pass/fail pair **by name**
  before the two-leg comparison.
- [ ] **Mutants** (committed code): the ack before the emit; a flat `data` (reader renders `{}`); `source_ref` without the
  `fleet-events:` prefix (reader returns nothing); `derive_uid("ev_", …)`; the bound dropped (a 50 KB arc → `detail_truncated=1`);
  a stale entrypoint "guessed" from the plugin cache; the migration refusal firing on D009; `CLAUDNA_STATE_DIR` conditional on
  `claudna_version`; a `"true"` string arming `claudna.harvest`.
- [ ] **The two-leg gate** (rc + scoped names + count line, against Task 0), CI on Linux, `bash harness/validate-bot-change.sh`
  directly.
- [ ] **Canary root (mandatory runtime validation).** One host, one real Claude bot with `claudna.session_summary: true` (one
  bot of many — the switch's own arm line) and a second with it unset; Claudron v0.9.0 installed; the vault migrated per Task 3's
  step. After a `/clear` or a SessionEnd and one tick (≤ 15 min): `claudlobby --json event list --type session_summary --since 1h`
  renders one row with `data.status == "ok"` and `data.journey.title` populated — **the thing the digest never achieved** — and one
  with `data.status == "skipped"`, `data.skipped_reason == "disabled"`; `<BOT_DIR>/data/claudna/sessions/<sid>/consumers.json`
  shows `claudlobby.through_seg`; `<BOT_DIR>/data/.plane-session` is **not** rewritten by the new session; `config plan` on the
  canary fleet passes with the migrated vault and is refused (with the named fix) against a scratch clone left at format 2.
  Cite the observation in the PR body behind `no_names`.

## Door consumer tables

| Door | Consumers at `cd292cb` | After this PR |
|---|---|---|
| `event list --type session_digest` | `fleet-digest/SKILL.md:58,60` (→ `fleet-observe` reads its output), `fleet-monitoring.md:98`, `ai-platform-monitor.md:22` (prose) | all four read `--type session_summary`; `test_no_retired_digest_reference.py` fails on any `library/` line that still names the digest |
| `$BOT_DIR/data/.plane-session` | `transcript-digest.sh:256` only (`test_plane_gauntlet_doors.py:148` is a dangling comment) | no reader, no writer |
| `derive_session_uid` | Python `plane/ids.py:79-89`; bash mirror `plane-session-start.sh:59-70`, parity-pinned by `tests/test_plane_session_hook.py:31-46` | Python only (`(id, runtime)` from P1); the parity suite retires with the mirror |
| `claudron doctor --json` | `doctor._claudron_doctor` (`doctor.py:795-870`) | `claudron_compat.vault_migration_state` — read by `doctor` (rows) and `validator` (the refusal) |

## Stated limitations

- **`skipped_reason: disabled` is the common row.** Summaries stay off for bots unless a fleet arms `claudna.session_summary`;
  the monitor sees identity and volume for every segment and a journey only where the spend was chosen. That is F6's coverage
  concern kept (F15), not a defect.
- **`tool_calls` is gone until P2** (`session.tool_calls` samples); `fleet-observe`'s token-bloat lens reads `prompts` meanwhile.
- **`config plan` now spends up to 60 s per distinct session-loop vault** on `claudron doctor --json` (Claudron #201 class); a
  timeout warns and never refuses.
- **F14's "then remove"** assumes the service user runs only bots; the same default root serves that user's interactive
  sessions, so the runbook makes removal conditional rather than scheduled.
- **No `config explain` provenance for `claudna.*`** (`scalar_config_origin` raises for mappings, `config.py:2262-2297`); the
  mapping precedent lands with P2's telemetry block.

## Test Plan

Unit: `tests/test_session_export.py` (new), `tests/test_composer.py` (two new lines, the no-retired-hook test, timer-set pins),
`tests/test_config.py` (the `claudna:` mapping), `tests/test_switches.py`, `tests/test_claudron_compat.py`,
`tests/test_claudron_loop.py` (validator refusal cells; `TestSnippetParity` under the vault extra), `tests/test_event_type_registry.py`,
`tests/test_no_retired_digest_reference.py`, `tests/test_fleet_mission.py` (example pin), `tests/test_instruction_budget.py`.
Removed suites: `test_transcript_digest.sh`, `test_transcript_digest_isolation.py`, `test_plane_session_hook.py`. Bash: the harness
scenario through `harness/validate-bot-change.sh` directly. The two-leg gate against Task 0's before-leg; CI on Linux.

## Verification Checklist

- [ ] `grep -c 'export CLAUDNA_STATE_DIR="$BOT_DIR/data/claudna"' <every composed bot.conf>` prints 1.
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
  no fresh `.plane-session`; `config plan` refused against the format-2 scratch clone with the `claudron doctor --fix` line, and
  accepted after the migration.
- [ ] The two-leg diff introduces no new failure names; the harness pair matches the Test Plan by name.

## What NOT To Do

- Do not write `session_summary` with a flat `data` or a non-`fleet-events:` `source_ref`: the reader (`plane-readers.py:1155-1164,1228`)
  would drop it exactly as it drops the digest today. Do not add a second reader.
- Do not rely on ingest truncation; bound `data` in the emitter (epic §11).
- Do not guess an entrypoint from the plugin cache (`installed_plugins.json`) when `entrypoint.json` is stale or absent — skip the
  bot and say so (F16).
- Do not ack before `emit_batch` returns; do not ack after an exception.
- Do not read a worker's session uid from `.plane-session`; do not keep a bash derivation (epic §11).
- Do not refuse a plan for D007/D008/D009/D010 or structure findings, and never run `claudron doctor --fix` from Claudlobby (it
  commits to a shared vault).
- Do not arm `claudna.*` through `bots.<name>.env` (`defaults.env` is not merged; the mapping is the one way).
- Do not touch `architecture/system-map-2026-07-30.md` or `CHANGELOG.md` history; do not remove `session_digest` or `tool_call`
  from the registry.
- Do not compose or validate a Codex bot here (F11).

## Context

area: compose / plane / library · effort: **M** per the epic index, honestly **L** across eight tasks — the seam after Task 3 is
where to split · risk: medium (a fleet timer that spawns a subprocess per bot; a `config plan` refusal that depends on an external
CLI; a library re-point that changes what the monitor reads) · priority: P3 · related: #2145 (epic), #1503 (the digest's plane
cutover), #1961 (the waived comparison, F15), #785 (the monitor), Claudron #190/#201 (doctor), clauDNA plan 5
(`2026-10-04-runtime-neutral-observability-p3-claudna-export-contract.md`).

Spec spellings corrected here, decisions unchanged: `derive_uid("ev", …)` not `"ev_"` (`ids.py:57-60`); the test that binds the
protocol rewrite to the registry entry is `test_no_retired_digest_reference.py` plus `test_event_type_registry.py:323`, not gate (d)
(`fleet-monitoring.md` is outside `DOCS`, `:404-408`).
