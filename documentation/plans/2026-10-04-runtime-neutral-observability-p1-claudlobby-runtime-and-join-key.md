---
title: "P1 — the runtime field, the join key on the doors, and the receipt that survives a retry (Claudlobby)"
type: plan
status: draft
owner: chrisrogers37
created: 2026-10-04
epic: documentation/plans/2026-10-04-runtime-neutral-observability-plan.md
spec: documentation/plans/2026-08-18-observable-plane-design-v2.md
issue: "#2145"
repos: Claudfather/Claudlobby
---

# P1 — the runtime field, the join key on the doors, and the receipt that survives a retry (Claudlobby)

> **Status:** draft, authored by `/claudna:forge` on 2026-10-04 from epic plan §6 P1 (Claudlobby bullets) and its
> `#### Spec: the request-receipt change for F17`. Code references are to Claudlobby `cd292cb` (the checkout's
> `origin/main`, `4da926d3`, is identical for every line cited). Depends on: nothing in another repo (the Claudron
> boundary-spec PR in §6 P1 is independent). Waits on canaries: **C11** (and C10) for Half B's child-shell guard —
> the fail-safe ships either way; see the last section. Two operator decisions are carried as conditional steps:
> **Q1** (Task 1) and **Q2** (Task 5).

## Summary

Half A gives a bot a declared runtime (`runtime: claude | codex`, default `claude`), carries it into `bot.conf`
as `CLAUDLOBBY_RUNTIME`, puts it on the registry keyframe contract, and teaches `derive_session_uid` the F2 rule
(`"<runtime>:" + id` for non-Claude; Claude byte-identical to today), then records the design-v2 amendments of
epic §1.1. Half B makes the worker's task doors, the report encoder and the message doors write the plane's
session slots (`events.session_uid`, `communications.sender_session_uid`) from **the caller's own session-id env**
(F1(c)), and — because that uid is already inside every request's hashed facts — freezes it on the receipt
(`RequestIntent.session_uid`, receipt format 2, F17(a)) so a retried `--request-id` after `/clear` replays instead
of raising `ReceiptConflict`. A `codex` bot is refused by the validator ("execution adapter not shipped") until the
companion (#2149, F11) lands; nothing here launches, composes hooks for, or validates a Codex bot. The PR may split
at the marked seam: Half A is shippable alone; Half B needs Half A's `CLAUDLOBBY_RUNTIME` and `derive_session_uid(runtime=)`.

## Evidence (Claudlobby at `cd292cb`)

- `claudlobby/config.py:669-805` `BotConfig`; `:704` `effort: str | None = None`; `:1811-1813` the enum-scalar shape
  `effort=_parse_enum("effort", _select_bot_scalar(raw, defaults, "effort")[0], KNOWN_EFFORTS)`; `:1721-1731`
  `_parse_enum` (returns `None` for `None`, so a non-`None` default must come from `_select_bot_scalar`'s `fallback`);
  `:1735-1741` `_select_bot_scalar` (presence-based: an explicit bot-level `null` wins); `:2262-2267`
  `_EXPLAIN_BOT_INHERITED_SCALARS` (a field not listed makes `config explain` say "provenance unsupported",
  `commands/config_explain.py:68-69`); no unknown-key check at bot or `defaults` level, so a misspelt `runtime:` is ignored today.
- `claudlobby/known_values.py:38-40` `KNOWN_EFFORTS`. `claudlobby/validator.py:629-634` `_validate_bots`; `:1259-1268` the
  per-bot model rung (`report.warn("model-unknown", …)`); `report.errors` is what `config plan` turns into `PlanError`
  (`config_staging.py:174-176`) and `config validate` prints (`config_validation.py:124`).
- `claudlobby/composer.py:977-978` `compose_bot_conf`; `:1091-1092` `# Exports for skills + scripts` / `export FLEET_NAME=`;
  `_shq` `:543-548`. `_runtime_scripts/start-bot.sh:223-229` sources `bot.conf` under `set -a`, so every export reaches the
  session and every CLI the session runs. Read **once at session start** (`CLAUDE.md:72`): a running bot sees the new line at its next restart.
- `claudlobby/plane/contracts.py:184-185` `_Strict` (`extra="forbid"`); `:670-688` `BotPayload` (`effort: Optional[str] = None` `:679`,
  `schema_version: str` `:688`); `:851-862` the inner entity payload is validated **at ingest**. `claudlobby/plane/registry_emit.py:77`
  `_SCHEMA = "1"`; `:414-431` the bot payload dict (`"effort": bot.effort` `:420`); `:434-436` `declared_hash`; **`:170-173`**
  the house rule for a new keyframe field: "generate writes with its own contract, but a keyframe spooled on a busy db is drained by
  the daemon, and a daemon older than this field quarantines the key (#1724). A measured keyframe keeps the shape every daemon
  accepts" — the field is written **only when it carries information**. Additive-field declaration precedent:
  `WorkstreamEvent.waiting_on` (`contracts.py:514`) + `migration_plan.py:421-424` `wire_additions`. No payload-schema bump has ever
  shipped and no reader validates the payload's `schema_version` (research r3 §f; `plane/registry_read.py` has no check).
- `claudlobby/plane/ids.py:79-89` `derive_session_uid(platform_session_id)` = `"sess_" + sha256(id)[:32]`, empty id refused.
  Bash mirror `_runtime_scripts/plane-session-start.sh:59-70` (reads the hook payload's `session_id`, never env); parity pin
  `tests/test_plane_session_hook.py:31-46`; Python pin `tests/test_plane_ids.py:33-43`.
- Session slots exist and are unwritten: `TaskEvent.session_uid` (`contracts.py:364`, `kind='task'` is the only kind that allows it,
  `KIND_MANIFEST` `:112-116`, DDL `0011:143-156`), `Communication.sender_session_uid` (`:210`), mapped at `plane/ingest.py:256` and `:162`.
  Both keys are **always** emitted into the fact projection (`request_facts.py:42-51` via `_family_values`), so `null` is already inside
  every door receipt's `projection_sha256` — filling the value is what makes a new session's retry hash differently.
- The doors read no `CLAUDE_*` env today (grep: zero hits in `claudlobby/`); the one env-reading binder is
  `claudlobby/operation_context.py:174-219` `_selected_task_contexts` (`FLEET_NAME`, `CLAUDLOBBY_FLEET`, `CLAUDLOBBY_ROOT`, `FLEET_ROOT`,
  `BOT_DIR`), and `:109-136` `bind_task_context` builds `TaskOperationContext(destination, host_uid, fleet_uid, caller, bots, caller_fleet_uid)`
  (`:136`). `claudlobby/task_operations.py:46-66` `TaskOperationContext` (frozen, ids validated in `__post_init__`); `:245-260` `_existing`
  (never compares fact hashes); `:267-278` `_deadline` reuses `previous.intent.expected_by` — the frozen-value pattern; `:292-309` `_replayed`;
  `:312-317` `_raw`; `:320-331` `_prepare` → `store.prepare(RequestIntent(…))`; `:445-463` `accept` (payload dict `:460-461`);
  `:476-524` `_assignment_report` (`message_id`/`event_ids` reuse `:511-513`, `encode_report_facts` `:514-517`, `_prepare` `:518-521`).
- `claudlobby/message_context.py:30-46` `MessageRoute` (15 fields, built positionally at `:156-160`); `:165-178` `HumanReplyRoute`
  (built at `:217`). `claudlobby/message_operations.py:449-466` `send_message.run()` replay checks; `:471-474` and `:489-493` the two
  `RequestIntent(…)` constructions; `:494-495` `ReceiptConflict("capture policy or communication projection changed")` — where the message
  doors conflict, **before** `store.prepare`; `:598-609` `send_unlinked_report`; `:647-675` `record_reply_to_human` (same conflict `:667-669`).
  `claudlobby/report_payload.py:168-170` `encode_report_facts` signature; `:189-190` the communication dict; `:198-199` the linked task
  detail; `:207-212` the unlinked `system` marker (no session slot — `contracts.py:122-125`). `claudlobby/message_payload.py:132-156`
  `encode_communication(intent, …)` reads the intent; payload dicts `:145-152`.
- Receipts: `claudlobby/request_receipts.py:23` imports only `ID_PATTERNS`; `:25` `FORMAT_VERSION = 1`; `:128-142` `RequestIntent`
  (`expected_by` last, `:142`); `:177` `format_version: int = FORMAT_VERSION`; `:299-301` `_validate` refuses `format_version != FORMAT_VERSION`;
  `:305-312` per-field `_id` checks; `:414-418` `_decode` back-fills `expected_by`; `:446-447` the round-trip equality; `:518-533` `_save`;
  `:535-545` `prepare` (`previous.intent != intent` → conflict, `:542-543`). `claudlobby/runtime_versions.py:10` "Add readable versions only
  alongside their implemented decoders"; `:21-22` `RECEIPT_FORMAT_VERSION = 1`, `SUPPORTED = {0, 1}`; `:39` the declaration row. The gate is
  `releases.py:110-120` (`SupportedVersions`, sorted read set containing write) consumed by `migration_plan.py:69-78` and `:425-430`
  (`target cannot read retained receipt_format: N`); a decode failure is `:201-209` → `unknown retained receipt_format`. Pins:
  `tests/test_request_receipts.py:119-131`, `tests/test_releases.py:93-96`, `tests/test_migration_plan.py:37-38,229-233,356-366,416-421`,
  `tests/test_release_install.py:118`.
- Readers: `claudlobby/task_state.py:77-85` reducer `TaskEvent`; `:200-204` `task_event_from_row` copies six columns and drops `session_uid`
  although `:352` selects `e.*`; `commands/task_read.py:69-73` prints `asdict(task)`. `_runtime_scripts/plane-lookup.py:158-178`
  `_by_assignment` over `plane-readers.py:442-449` `_ASG_ROW_COLS`/`_ASG_ROW_SELECT`; no bash consumer of `--by-assignment` remains
  (`task-act.sh` is gone; grep of `_runtime_scripts/*.sh`), but `_runtime_scripts/CLAUDE.md:119` documents its columns and `:116` still says
  "`report-back.sh` attaches `session_uid` to its task facts" — that script no longer exists and nothing attaches the uid.
- Tests construct contexts and routes **positionally**: `tests/test_task_operations.py:31-43` (`TaskOperationContext(context, host, fleet_uid, caller, actors)`),
  `tests/test_message_operations.py:32-50` (`MessageRoute(` with 15 args). A new field must be last, with a default.
- Docs to amend: `documentation/fleet-yaml-schema.md:27` (`effort:` in the shape), `:89-91` (per-bot `account/model/effort`), `:214` (the scalar
  merge rule), `:885-891` (per-field sections); `fleet.yaml.example:58-59`, `:155`; `documentation/environment-variables.md:34-42` (Bot Identity
  table); `PROJECT_MISSION.md:114`; design v2 `:59`, `:542`, `:545`, `:588`, `:606` (amendment precedent: the bracketed dated note at `:603`);
  `documentation/architecture/observable-plane.md:69`. Instruction files: `CLAUDE.md:229` — each `AGENTS.md` is a byte copy, enforced by `tests/test_instruction_budget.py`.
- Prior art: #1997 (the `agent_cli:` proposal and the `runtime` name clash with `activation_runtime.py`/`runtime_admission.py`/`runtime_versions.py`,
  `host update runtime`, `config validate --runtime`) — read from epic §4.1 only; the sandbox refused `gh` (TLS), so this plan did not read the issue itself.

## Implementation Plan

### Dependencies
None in code. Operator decisions Q1 (key name) and Q2 (mission line) before the PR opens; canaries C11/C10 before the child-shell guard is relaxed (never before merge).

### Blocks
P2 (`agent.runtime` in `OTEL_RESOURCE_ATTRIBUTES` reads `bot.runtime`; the intake's derive-not-mint rule reads `derive_session_uid(…, runtime)`), P3 (the clauDNA export's `runtime` joins on the same uid), the companion #2149 (needs `BotConfig.runtime`, the payload field and the receipt shape).

### Steps

### Task 0: worktree, before-leg, evidence

- [ ] Worktree `~/Projects/claudlobby-worktrees/p1-runtime-join` on branch `obs/p1-runtime-and-join-key` from `origin/main` (`4da926d3`). Venv per `CLAUDE.md` Key Commands (`python3 -m venv .venv && ./.venv/bin/python -m pip install -e '.[dev]'`). Evidence dir `~/Projects/claudlobby-worktrees/p1-runtime-join-out/`.
- [ ] Before leg, on the base commit: `./.venv/bin/pytest -q 2>&1 | tee before.txt` (the baseline is not green — record the failing names, the two-leg comparison is by name) and `PLANE_EMIT_CLI=$PWD/.venv/bin/claudlobby bash harness/validate-bot-change.sh 2>&1 | tee harness-before.txt` (record the `=== N passed, M failed ===` line and the names).

### Task 1: `BotConfig.runtime` — the vocabulary, refused beyond `claude`

**Files:** `claudlobby/known_values.py`, `claudlobby/config.py`, `claudlobby/validator.py`, `tests/test_config.py`, `tests/test_known_values.py`, `tests/test_validator.py`, `tests/test_env_register.py`, `documentation/fleet-yaml-schema.md`, `fleet.yaml.example`.

- [ ] **Step 1 (tests first):** `tests/test_config.py` beside `test_headless_config_defaults_from_fleet` (`:224`): `test_runtime_defaults_to_claude_and_follows_bot_over_defaults` — `_coerce_bot("t", {"expertise": ["eng"]}, {}).runtime == "claude"`; `defaults={"runtime": "codex"}` → `"codex"`; bot `{"runtime": "claude"}` over those defaults → `"claude"`; an explicit bot-level `null` reads as the built-in `"claude"` (never `None`: the composer must always have a string). `test_runtime_rejects_unknown_values`: `{"runtime": "code"}` → `ValueError` matching `Invalid runtime 'code'.*Did you mean 'codex'`; `{"runtime": 1}` → `ValueError`. `tests/test_known_values.py`: `KNOWN_RUNTIMES == frozenset({"claude", "codex"})`. `tests/test_validator.py::TestValidate`: `test_codex_runtime_is_refused_until_the_adapter_ships` — rewrite the fixture manifest with `runtime: codex` on one bot → `report.has_errors` and one error containing `execution adapter not shipped` naming the bot; the same manifest with `runtime: claude` adds no error. `tests/test_env_register.py`: extend `test_config_explain_scalar_sources_follow_loader_without_values` (`:135`) with `bot.runtime` → (`built_in`, `set`, `None`) when unset and (`fleet.defaults`, `fleet.defaults.runtime`) when set under `defaults:`.
- [ ] **Step 2:** `known_values.py`, after `KNOWN_EFFORTS` (`:40`):

```python
# ── Runtime ──────────────────────────────────────────────────────
# The agent CLI a bot runs under (#2145 F2/F9 vocabulary). `codex` is declared
# here so fleet.yaml, the plane and clauDNA share one spelling; composing or
# launching a codex bot is the execution-adapter companion's (#2149, F11).
KNOWN_RUNTIMES: frozenset[str] = frozenset({"claude", "codex"})
```

`config.py` `BotConfig`, after `effort` (`:704`): `runtime: str = "claude"  # claude | codex — #2145; see known_values.KNOWN_RUNTIMES`. In `_coerce_bot` after the `effort=` lines (`:1811-1813`):

```python
        runtime=_parse_enum(
            "runtime",
            _select_bot_scalar(raw, defaults, "runtime", "claude")[0] or "claude",  # an explicit null reads as the default
            KNOWN_RUNTIMES,
        ),
```

Add `"runtime"` to `_EXPLAIN_BOT_INHERITED_SCALARS` (`:2262-2267`) — it is a plain inherited scalar, exactly `effort`'s path, so `config explain bot.runtime --bot B` works with no other change (`scalar_config_origin`, `:2271-2297`).
- [ ] **Step 3:** `validator.py`, in `_validate_bots`' per-bot loop directly above the model rung (`:1259`), an **error** (not a warning — `config plan` must refuse, `config_staging.py:174-176`):

```python
        # #2145 F11: the vocabulary ships now; only the Claude adapter exists.
        if bot.runtime != "claude":
            report.errors.append(
                f"bot '{bot_name}': runtime '{bot.runtime}' — execution adapter not shipped. "
                f"This release composes and launches Claude Code bots only; the Codex adapter is the "
                f"companion epic (#2149). Set runtime: claude, or remove the bot until it lands."
            )
```

- [ ] **Step 4 (docs):** `documentation/fleet-yaml-schema.md`: `:27` gains `runtime: claude | codex              # agent CLI (default: claude; codex refused until #2149)` after `effort`; the per-bot stanza gains `runtime: <runtime>` after `:91` (`effort: <effort>`); `:214` scalars list adds `runtime`; a new section before `### \`bots.<name>.remote_control\`` (`:885`): `### \`bots.<name>.runtime\`` — "String enum, `claude` (default) or `codex`. The agent CLI the bot runs under; composed into `bot.conf` as `CLAUDLOBBY_RUNTIME` and read by every door that derives the bot's session uid (#2145 §2.2). `codex` is accepted by the parser and **refused by the validator** (`execution adapter not shipped`) until the Codex execution adapter ships (#2149). Can be set in `defaults:`. Not to be confused with `host update runtime` (the Claude Code binary) or `config validate --runtime` (the composed-output audit)." `fleet.yaml.example`, after `:59` (`effort: max`): `# runtime: claude                          # claude | codex — codex waits for the execution adapter (#2149)`.
- [ ] **Step 5:** Verify: `./.venv/bin/pytest tests/test_config.py tests/test_known_values.py tests/test_validator.py tests/test_env_register.py tests/test_cold_start_contract.py -q`. Commit: `feat(config): a bot declares its runtime — claude today; codex is refused until the execution adapter ships (#2145)`.
- [ ] **Q1 (operator, before the PR opens).** This plan uses `runtime:`. If the operator picks #1997's `agent_cli:` instead, the fleet.yaml key, the `BotConfig` field, the explain-allowlist entry, the schema-doc heading and the example line are spelled `agent_cli` and `KNOWN_RUNTIMES` becomes `KNOWN_AGENT_CLIS`, while the composed env name `CLAUDLOBBY_RUNTIME`, `BotPayload.runtime`, `derive_session_uid(…, runtime=)` and the cross-repo F2/F9 vocabulary keep `runtime`, with one mapping sentence in the schema doc.

### Task 2: `CLAUDLOBBY_RUNTIME` in `bot.conf`

**Files:** `claudlobby/composer.py`, `tests/test_composer.py`, `documentation/environment-variables.md`.

- [ ] **Step 1 (tests first):** `tests/test_composer.py`, in `TestComposerProvidedPathAnchorsExported`'s neighbourhood (`:1033`): `test_bot_conf_exports_the_runtime` — the composed text of a default bot contains exactly one line `export CLAUDLOBBY_RUNTIME=claude`; a `BotConfig(..., runtime="codex")` (constructed directly — the validator is not in the composer's path) composes `export CLAUDLOBBY_RUNTIME=codex`.
- [ ] **Step 2:** `compose_bot_conf`, directly after `export FLEET_NAME=` (`:1092`):

```python
    # #2145 §2.2: the doors derive the caller's session uid from its runtime's
    # session-id env; this names the runtime so they derive with the right rule.
    lines.append(f"export CLAUDLOBBY_RUNTIME={_shq(bot.runtime)}")
```

Always emitted (a door with no value defaults to `claude`, so an un-regenerated bot behaves identically).
- [ ] **Step 3 (docs):** `documentation/environment-variables.md` Bot Identity table (`:36-42`), a row after `BOT_DIR`: `| \`CLAUDLOBBY_RUNTIME\` | \`bots.<name>.runtime\` | The bot's agent CLI (\`claude\` or \`codex\`). Read by the task, report and message doors to derive the caller's session uid (\`derive_session_uid(id, runtime)\`, #2145 F2); absent means \`claude\` |`.
- [ ] **Step 4:** Verify: `./.venv/bin/pytest tests/test_composer.py tests/test_freshbox_selfcontained.py tests/test_boot_policy_conformance.py -q`. Commit: `feat(compose): bot.conf names the bot's runtime (#2145)`.

### Task 3: `BotPayload.runtime` — additive, declared, written only when it says something

**Decision (the epic left it to this plan):** an **additive optional field with a `wire_additions` declaration, no payload-schema bump**. Why: (1) the daemon validates the inner payload strictly at ingest (`contracts.py:851-862`), and a bump does nothing for that — an older daemon refuses an unknown key whatever `schema_version` says; (2) the repo's own rule for exactly this case is `registry_emit.py:170-173`: write the new key **only when it carries information**, so a keyframe spooled on a busy db and drained by an older daemon keeps "the shape every daemon accepts"; in P1 every bot is `claude` (Task 1 refuses the rest), so no keyframe carries the key until the companion ships on a release whose floor already includes this contract; (3) no reader validates the payload's `schema_version` and ~10 fixture literals pin `"1"`, so a bump is churn without a reader. Absence reads as `claude`.

**Files:** `claudlobby/plane/contracts.py`, `claudlobby/plane/registry_emit.py`, `claudlobby/migration_plan.py`, `tests/test_plane_contracts.py`, `tests/test_plane_registry.py`, `tests/test_migration_plan.py`.

- [ ] **Step 1 (tests first):** `tests/test_plane_contracts.py`: a bot keyframe payload (shape of `tests/test_plane_registry.py::bot_stub`, `:95-99`) with `"runtime": "codex"` validates; `"runtime": "gemini"` is a `ContractViolation`; the stub without the key still validates (older emitters). `tests/test_plane_registry.py`: `bot_payload(...)` for a default bot has **no** `runtime` key and its `declared_hash` equals today's (compute it in the test from the same inputs without the key); for `replace(bot, runtime="codex")` the payload carries `"runtime": "codex"` and a different `declared_hash`. `tests/test_migration_plan.py`: `operational["wire_additions"]` contains the new row.
- [ ] **Step 2:** `contracts.py` `BotPayload`, after `effort` (`:679`): `runtime: Optional[Literal["claude", "codex"]] = None   # #2145; absent = claude (keyframes before P1, and every P1 keyframe — see registry_emit)`. `registry_emit.bot_payload` (`:414-431`): after the dict, `if bot.runtime != "claude": payload["runtime"] = bot.runtime` with the `:170-173` rule quoted in a comment; `declared_hash` (`:434-436`) input gains `**({"runtime": bot.runtime} if bot.runtime != "claude" else {})` — a declaration fact, hashed only when present so today's hashes are byte-identical. `migration_plan.py:421-424` adds `{"family": "registry_snapshot", "field": "runtime", "location": "payload (entity bot)", "classification": "optional_metadata", "old_reader": "a keyframe without it reads as runtime claude"}`.
- [ ] **Step 3:** Verify: `./.venv/bin/pytest tests/test_plane_contracts.py tests/test_plane_registry.py tests/test_plane_registry_read.py tests/test_migration_plan.py tests/test_leaf_manager_role.py tests/test_requires_linking.py -q`. Commit: `feat(plane): the bot keyframe may name its runtime; additive, declared, written only for a non-claude bot (#2145)`.

### Task 4: `derive_session_uid(platform_session_id, runtime="claude")` — F2

**Files:** `claudlobby/plane/ids.py`, `tests/test_plane_ids.py`, `tests/test_plane_session_hook.py` (unchanged, must stay green).

- [ ] **Step 1 (tests first):** `tests/test_plane_ids.py` after `test_session_uid_is_derived_and_stable` (`:33-43`): `test_session_uid_is_runtime_qualified_for_non_claude` — `derive_session_uid(x) == derive_session_uid(x, runtime="claude") == "sess_" + sha256(x)[:32]` (Claude rows keep joining); `derive_session_uid(x, runtime="codex") == "sess_" + sha256("codex:" + x)[:32]` and `!= derive_session_uid(x)` (two vendors' ids cannot collide); `runtime=""` and `runtime="  "` raise `ValueError`; the result matches `ID_PATTERNS["session"]`.
- [ ] **Step 2:** `ids.py:79-89`:

```python
def derive_session_uid(platform_session_id: str, runtime: str = "claude") -> str:
    """sess_ uid DERIVED from the platform session id (sha256, first 32 hex).
    ... (existing docstring) ...
    #2145 F2: `claude` hashes the raw id (byte-identical to every row written so
    far); any other runtime hashes "<runtime>:<id>", so two vendors' ids never
    collide. The vocabulary is config's (known_values.KNOWN_RUNTIMES); this
    module stays import-free of it and refuses only an empty runtime."""
    if not platform_session_id or not platform_session_id.strip():
        raise ValueError("empty platform session id — refusing to derive")
    if not runtime or not runtime.strip():
        raise ValueError("empty runtime — refusing to derive")
    material = platform_session_id if runtime == "claude" else f"{runtime}:{platform_session_id}"
    return "sess_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
```

The bash mirror `plane-session-start.sh:59-70` is **not** touched: it is Claude-only until it retires in P3 (no Codex bot can run before the companion), and `test_bash_derivation_matches_python_byte_for_byte` keeps passing because the default is byte-identical.
- [ ] **Step 3:** Verify: `./.venv/bin/pytest tests/test_plane_ids.py tests/test_plane_session_hook.py -q`. Commit: `feat(plane): derive_session_uid takes the runtime — claude unchanged, others qualified (#2145 F2)`.

### Task 5: design v2 amendments (epic §1.1), the mission line (Q2), Half A's CHANGELOG

**Files:** `documentation/plans/2026-08-18-observable-plane-design-v2.md`, `documentation/architecture/observable-plane.md`, `PROJECT_MISSION.md` (conditional), `CHANGELOG.md`.

- [ ] **Step 1:** design v2, in the bracketed dated-note form of `:603`, each note opening `**[2026-10-04 amendment, #2145 P1]**`: `:59` (the `session_uid` bullet) — the derivation is `(runtime, session_id)`: raw id for `claude`, `"<runtime>:" + id` otherwise (F2); `:542` and `:545` pilot (b) — the LangSmith plugin pilot is **superseded** (LangSmith is out of scope; telemetry stays local), and the tmux-boundary `trace_id`/`span_id` instrumentation is **deferred** (interactive sessions ignore inbound trace context, Codex documents no propagation; the dispatch join is the session uid of §2.2, the envelope columns stay); `:588` Phase 3 — "OTel + LangSmith" becomes "native OTel through a local Collector (epic P2)"; `:606` — the SessionStart hook and `process_uid` minting are **superseded**: the doors derive the uid from the caller's own session-id env (F1(c)); the hook retires with the digest in P3. One line in §9b (`:409-426` region header): "`session_usage`/`utilization_windows`: decided on P2's evidence". `observable-plane.md:69`: "pinned byte-identical to `ids.derive_session_uid`" → "pinned byte-identical to `ids.derive_session_uid(id)` for runtime `claude`; other runtimes derive in Python only (#2145 F2)".
- [ ] **Step 2 — conditional on Q2, `**Operator confirms**`:** `PROJECT_MISSION.md:114` currently reads `- **Per-bot LLM provider abstraction.** Claudlobby is for Claude Code specifically. Bots running on other LLMs would require enough divergence that they belong in a different framework.` Proposed replacement: `- **Per-bot LLM provider abstraction.** Claudlobby composes and supervises agent CLIs — Claude Code today, OpenAI Codex through its execution adapter (#2145 F11, #2149) — and the model stays each CLI's concern. It does not abstract LLM providers beneath the CLI, and a bot that is not an agent CLI belongs in a different framework.` Either the operator confirms this wording in the PR, or records on #2145 that the F11 ratification supersedes the line and this step is dropped.
- [ ] **Step 3:** `CHANGELOG.md` `[Unreleased]`: `### Added — a bot declares its runtime, and the plane's session uid is runtime-qualified (#2145 P1, Half A)` with one bullet per Task 1–4 (the refusal wording, the env name, the keyframe rule, the F2 derivation) and one for the design-v2 amendments.
- [ ] **Step 4:** Commit: `docs: record the #2145 amendments to design v2, and the runtime vocabulary (P1 Half A)`.

---

**SEAM — the PR may split here.** Half A (Tasks 1–5) is complete and shippable alone. Half B (Tasks 6–11) reads `CLAUDLOBBY_RUNTIME` and calls `derive_session_uid(…, runtime)`; if split, Half B's Dependencies line names Half A's PR.

### Task 6: the receipt carries the uid — `RequestIntent.session_uid`, format 2, two literals become one (F17)

**Files:** `claudlobby/runtime_versions.py`, `claudlobby/request_receipts.py`, `tests/test_request_receipts.py`, `tests/test_releases.py`, `tests/test_migration_plan.py`.

- [ ] **Step 1 (tests first):** `tests/test_request_receipts.py::test_uuid_conflicts_and_private_digest_only_storage` (`:119-131`): the `format_version = 2` leg now **loads**; add `3` → refused; `replace(intent, session_uid="sess_zz")` → `ReceiptError`; `replace(intent, session_uid="sess_" + "a"*32)` prepares under a fresh request id and round-trips. New `test_v1_receipt_loads_with_no_session_uid_and_is_not_restamped`: write the store's file by hand as a v1 document (`format_version: 1`, `intent` without `session_uid`) → `store.load().intent.session_uid is None`, `.format_version == 1`; after `store.begin_attempt()` the file still says `format_version: 1` (every mutation is `replace(receipt, …)` — `:555`, `:580-582`); a **new** receipt from `prepare` says `2`. `tests/test_releases.py:95`: `"receipt_format": 2`. `tests/test_migration_plan.py`: `:232` → `"receipt_format"] == 2`; `:233` and `:363` → `"unsupported receipt_format: 2"`; `:357-358` → `[2]` and `format_version == 2`; keep the `:416-421` parametrization (`{"read": [1], "write": 1}` now also yields `target cannot read retained receipt_format: 2`, still a blocker); **add** the F17 legs: a hand-written v1 receipt beside the v2 one → `inventory["versions"] == [1, 2]`, `plan.readability_blockers(target.compatibility) == ()` (v1 stays readable), and a target declaring `{"read": [0, 1], "write": 1}` → `("target cannot read retained receipt_format: 2",)` in blockers. `tests/test_release_install.py:118` is unchanged and must stay green (the sealed manifest picks the declaration up).
- [ ] **Step 2:** `runtime_versions.py:21-22` → `RECEIPT_FORMAT_VERSION = 2` and `SUPPORTED_RECEIPT_FORMAT_VERSIONS = frozenset({0, 1, RECEIPT_FORMAT_VERSION})`; docstring `:6-7` → "Receipt v1 and v2 use request_receipts; v2 adds intent.session_uid (#2145 F17) and v1 stays readable, back-filled as None." `request_receipts.py:23-25`:

```python
from .plane.ids import ID_PATTERNS
from .runtime_versions import RECEIPT_FORMAT_VERSION as FORMAT_VERSION, SUPPORTED_RECEIPT_FORMAT_VERSIONS

_READABLE_FORMATS = SUPPORTED_RECEIPT_FORMAT_VERSIONS - {0}   # 0 means "absent", never a file
```

`RequestIntent` (`:142`) gains the last field `session_uid: str | None = None  # #2145 F17: the caller's session uid at the first attempt; a replay reuses it`. `_validate` (`:300-301`) → `if type(receipt.format_version) is not int or receipt.format_version not in _READABLE_FORMATS: raise ReceiptError("unsupported request receipt format")`; after the `message_id` loop (`:309-312`): `if intent.session_uid is not None: _id(intent.session_uid, "session")`. `_decode` (`:416-418`): back-fill `"session_uid": None` the same way `expected_by` is, so `:446-447` holds for every file on disk. `_save` is unchanged: a new receipt is stamped `2` by the dataclass default; a loaded v1 keeps `1`.
- [ ] **Step 3:** Verify: `./.venv/bin/pytest tests/test_request_receipts.py tests/test_releases.py tests/test_migration_plan.py tests/test_release_install.py tests/test_request_facts.py -q`. Commit: `feat(receipts): format 2 — the intent freezes the caller's session uid, v1 stays readable, one version literal (#2145 F17)`.

### Task 7: one resolver, two binders — `caller_session_uid()` on the contexts

**Files:** `claudlobby/operation_context.py`, `claudlobby/task_operations.py`, `claudlobby/message_context.py`, `tests/test_operation_context.py`, `tests/test_message_context.py`, `tests/test_task_operations.py`.

- [ ] **Step 1 (tests first):** `tests/test_operation_context.py`: `test_caller_session_uid_reads_the_runtime_env_and_records_nothing_rather_than_a_wrong_uid` — with `CLAUDE_CODE_SESSION_ID=<id>` and no `CLAUDLOBBY_RUNTIME` → `derive_session_uid(id)`; with `CLAUDLOBBY_RUNTIME=claude` the same; with the id absent, empty or whitespace → `None`; with `CLAUDLOBBY_RUNTIME=codex` → `None` (no Codex session env is known until C1 records it — the companion adds the row); with `CLAUDLOBBY_RUNTIME=gemini` → `None`; with `CLAUDE_CODE_CHILD_SESSION=1` → `None` while `SUBAGENT_SHELL_SHARES_SESSION_ID` is not `True`, and the uid when the test monkeypatches it `True` (the C11 flip, pinned both ways). `test_generated_origin_binds_the_callers_session_uid`: `resolve_task_mutation_context` under the generated env of `:101-120` plus `CLAUDE_CODE_SESSION_ID` → `ctx.session_uid == derive_session_uid(id)`; without it `None`. `tests/test_task_operations.py`: `TaskOperationContext(..., session_uid="sess_zz")` → `TaskQueryError`. `tests/test_message_context.py::test_generated_route_keeps_caller_and_peer_ids_with_plane_absent` (`:99`): set the env → `bare.session_uid == derive_session_uid(id)`.
- [ ] **Step 2:** `operation_context.py` (imports `os` and `.plane.ids` already, `:14`, `:25`):

```python
# #2145 F1(c): which env names the caller's own session id, per runtime. Codex's
# (CODEX_SESSION_ID, source-only until canary C1 measures it) is added by the companion.
_SESSION_ID_ENV = {"claude": "CLAUDE_CODE_SESSION_ID"}
# Canary C11: does a subagent's shell (CLAUDE_CODE_CHILD_SESSION=1) carry the SAME id
# as the hook payload? Unmeasured (None) is treated as "no": record no uid rather than
# another session's. Flip to True with C11's run-log entry; the test pins both branches.
SUBAGENT_SHELL_SHARES_SESSION_ID: bool | None = None


def caller_session_uid(environ: Mapping[str, str] | None = None) -> str | None:
    """The caller's own session uid (§2.2), or None when the env cannot name one."""
    env = os.environ if environ is None else environ
    runtime = (env.get("CLAUDLOBBY_RUNTIME") or "claude").strip() or "claude"
    name = _SESSION_ID_ENV.get(runtime)
    if name is None:
        return None
    if env.get("CLAUDE_CODE_CHILD_SESSION") == "1" and SUBAGENT_SHELL_SHARES_SESSION_ID is not True:
        return None
    raw = env.get(name, "")
    if not raw.strip():
        return None
    return derive_session_uid(raw, runtime=runtime)
```

`bind_task_context` `:136` → `return TaskOperationContext(destination, host_uid, fleet_uid, caller, bots, caller_fleet_uid, session_uid=caller_session_uid())`. `task_operations.TaskOperationContext` (`:46-52`) gains the last field `session_uid: str | None = None`, validated in `__post_init__` with `ID_PATTERNS["session"]` when not `None` (`TaskQueryError("operation context requires a canonical session uid")`). `message_context.MessageRoute` (`:46`) and `HumanReplyRoute` (`:178`) gain the last field `session_uid: str | None = None`; `resolve_message_route` (`:156-160`) and `resolve_human_reply_route` (`:217`) pass `session_uid=caller_session_uid()`. One resolver, called by the two binders; the operations modules keep **zero** env reads.
- [ ] **Step 3:** Verify: `./.venv/bin/pytest tests/test_operation_context.py tests/test_message_context.py tests/test_task_operations.py tests/test_message_operations.py -q`. Commit: `feat(context): the operation context and message routes carry the caller's own session uid (#2145 F1)`.

### Task 8: the doors write the join key, and a retry reuses the frozen one (F1(c), F17)

**Files:** `claudlobby/task_operations.py`, `claudlobby/report_payload.py`, `claudlobby/message_payload.py`, `claudlobby/message_operations.py`, `tests/test_task_operations.py`, `tests/test_report_payload.py`, `tests/test_message_operations.py`, `tests/test_task_write_cli.py`, `tests/test_message_write_cli.py`.

- [ ] **Step 1 (tests first).** Operations level, with `worker = replace(ctx, caller=ctx.bots["worker"], session_uid=A)` and `B` a second uid:
  - `test_accept_records_the_callers_session_and_a_retry_from_a_new_session_replays` (beside `:332`): `accept(worker_A, rid, asg)` → `events.session_uid == A` (read the row); `accept(replace(worker_A, session_uid=B), rid, asg).replayed` is `True`, no `ReceiptConflict`, the row still carries `A`, the receipt file bytes are unchanged, `_receipt(ctx, rid).intent.session_uid == A`; a **new** request from `B` on a fresh assignment carries `B`; a caller with `session_uid=None` writes `NULL` and its retry from `A` also replays (the pre-P1 receipt case: the frozen `None` is reused).
  - `test_assignment_reports_record_the_session_on_both_facts_and_replay_across_a_clear` (beside `:783`): `progress(worker_A, …)` → `communications.sender_session_uid == A` and the linked task row's `session_uid == A`; the retry from `B` replays with the same `message_id` and both rows unchanged.
  - `tests/test_report_payload.py` (beside `:70`): `_facts(report, link, session_uid=A)` puts `sender_session_uid` on the communication and `session_uid` on the task detail; unlinked: on the communication only, and the `system` marker dict is byte-identical to today (`:207-212`, no session slot).
  - `tests/test_message_operations.py`: `test_send_records_the_sender_session_and_a_retry_from_a_new_session_replays` — `route_A = replace(route, session_uid=A)`; send → `sender_session_uid == A`; the same `request_id` with `replace(route, session_uid=B)` and `retry_uncertain=True` → `replayed`, one native send, no `ReceiptConflict` at `:494-495`; the same pair for `send_unlinked_report` (`fleet reports submit`, beside `:329`) and for `record_reply_to_human`.
  - CLI level (one test each in `tests/test_task_write_cli.py` and `tests/test_message_write_cli.py`): the generated-caller env (`_generated`, `tests/test_message_write_cli.py`) plus `monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", id)`; `assignment accept --request-id R` writes `derive_session_uid(id)`; `setenv` a second id and repeat → `replayed: true`; `fleet reports submit` the same. These are the tests the epic's spec names ("the F17 test itself", for the task, message and report doors).
- [ ] **Step 2 — task doors.** `_prepare` (`:320-331`) gains `session_uid=None` and passes `session_uid=session_uid` into `RequestIntent`. `accept` (`:445-463`): `session_uid = previous.intent.session_uid if previous else ctx.session_uid` (the `expected_by` pattern, `:267-278`); the payload dict (`:460-461`) gains `session_uid=session_uid`; `_prepare(..., session_uid=session_uid)`. `_assignment_report` (`:476-524`): the same line beside `message_id`/`event_ids` (`:511-513`); `encode_report_facts(..., session_uid=session_uid)`; `_prepare(..., session_uid=session_uid)`. `_existing` (`:245-260`) is **unchanged** — a different uid is the same request, retried. `report_payload.encode_report_facts` (`:168-170`) gains `session_uid: str | None = None`; when not `None`: `comm["sender_session_uid"] = session_uid` (`:189-190`) and, linked only, `detail["session_uid"] = session_uid` (`:198-199`). A `None` leaves both dicts byte-identical to today.
- [ ] **Step 3 — message doors.** `message_payload.encode_communication` (`:145-152`): `if intent.session_uid is not None: payload["sender_session_uid"] = intent.session_uid` — it already takes the intent, which is also how a replay reuses the frozen value (`intent = existing.intent`, `:497`). `message_operations.send_message.run()` (`:449-500`): `session_uid = old.session_uid if existing is not None else route.session_uid` beside `message_id`; both `RequestIntent(…)` constructions (`:471-474`, `:489-493`) gain `session_uid=session_uid`; the unlinked report passes `session_uid=session_uid` to `encode_report_facts` (`:476-479`). `record_reply_to_human` (`:647-675`): `old.session_uid` / `route.session_uid` into the draft and the prepared intent. The recomputed facts then equal the stored ones and `:494-495` / `:667-669` no longer fire on a changed session.
- [ ] **Step 4:** Verify: `./.venv/bin/pytest tests/test_task_operations.py tests/test_message_operations.py tests/test_report_payload.py tests/test_task_write_cli.py tests/test_message_write_cli.py tests/test_plane_cutover_reports.py tests/test_request_facts.py -q`. Commit: `feat(doors): accept, the assignment reports, reports submit and message send record the caller's session; a retry after /clear replays (#2145 F1 F17)`.

### Task 9: the readers show it — `task show`, `plane-lookup.py --by-assignment`

**Files:** `claudlobby/task_state.py`, `claudlobby/_runtime_scripts/plane-readers.py`, `claudlobby/_runtime_scripts/plane-lookup.py`, `claudlobby/_runtime_scripts/CLAUDE.md` + `AGENTS.md`, `tests/test_task_state.py`, `tests/test_plane_lookup.py`.

- [ ] **Step 1 (tests first):** `tests/test_task_state.py`: an event inserted with `session_uid="sess_" + "1"*32` (`_insert`, `:22`) surfaces as `history[i].session_uid` and in `asdict(task)["history"][i]["session_uid"]`; a row without it reads `None`. `tests/test_plane_lookup.py::test_by_assignment_returns_open_only_unless_any_state` (`:113`): the lines gain a trailing `-`; new `test_by_assignment_prints_the_executing_session`: emit an `accepted` task event with `session_uid` for the assignment → the trailing column is that uid; a later `nudged` event with no uid leaves it (the newest **non-null** wins — a manager's act from an operator shell carries none, and the reader asks which session executed).
- [ ] **Step 2:** `task_state.TaskEvent` (`:77-85`) gains `session_uid: str | None` after `successor_id`; `task_event_from_row` (`:200-204`) adds `"session_uid"` to its tuple (the column is already selected, `:352`); `task_read.py` is untouched — `asdict(task)` carries it. `plane-readers.py:442-449`: `_ASG_ROW_COLS += ("session_uid",)` and `_ASG_ROW_SELECT` gains `, (SELECT e.session_uid FROM events e WHERE e.kind='task' AND e.assignment_id=a.assignment_id AND e.session_uid IS NOT NULL ORDER BY e.ingest_seq DESC LIMIT 1) AS session_uid`. `plane-lookup.py:158-178` prints ` {row['session_uid'] or '-'}` last; docstring and `--by-assignment` help list the column. `_runtime_scripts/CLAUDE.md:119`: the by-assignment column list gains `<session_uid|->`; `:116`: replace "where `report-back.sh` attaches `session_uid` to its task facts" with "(read only by `transcript-digest.sh`; the Python doors derive the caller's uid from its own session-id env, #2145 F1(c), and never from this file)". Then `cp claudlobby/_runtime_scripts/CLAUDE.md claudlobby/_runtime_scripts/AGENTS.md`.
- [ ] **Step 3:** Verify: `./.venv/bin/pytest tests/test_task_state.py tests/test_task_audit.py tests/test_task_queries.py tests/test_plane_lookup.py tests/test_task_read_cli.py tests/test_instruction_budget.py -q`. Commit: `feat(readers): task show and plane-lookup --by-assignment name the session that ran the work (#2145)`.

### Task 10: Half B docs and CHANGELOG

**Files:** `documentation/architecture/observable-plane.md`, `documentation/environment-variables.md`, `CHANGELOG.md`.

- [ ] `observable-plane.md` Identity section (`:60-69`): one sentence — "The task, report and message doors record the caller's session uid from its runtime's session-id env (`CLAUDE_CODE_SESSION_ID` for `claude`); a door with no such env records none (#2145 §2.2)". `environment-variables.md`: no new var (the doors read Claude Code's own `CLAUDE_CODE_SESSION_ID`); add to the `CLAUDLOBBY_RUNTIME` row from Task 2 that `CLAUDE_CODE_CHILD_SESSION=1` suppresses the uid until C11 is measured. `CHANGELOG.md` `[Unreleased]`: `### Added — every task, report and message door records the session that ran it, and a retried request after /clear replays (#2145 P1, Half B)` with bullets for the doors, the receipt format 2 (v1 stays readable; `host migrate` planning reports a v2 receipt as unreadable only to a release older than this one), the readers, and the C11 fail-safe. Commit: `docs: the join key on the doors, receipt format 2 (#2145 P1 Half B)`.

### Task 11: gate and the canary-root observation

- [ ] Committed-code mutants, one per task, each shown failing then restored: `KNOWN_RUNTIMES` without `codex` (Task 1 parse test); the validator rung demoted to `warn` (Task 1 validator test); the `CLAUDLOBBY_RUNTIME` line dropped (Task 2); `runtime` always written to the payload (Task 3's "no key for claude" test); `runtime == "claude"` branch removed from `derive_session_uid` (Task 4 Claude-identity and the bash parity pin); `_READABLE_FORMATS` back to `{FORMAT_VERSION}` (Task 6's v1 load); `previous.intent.session_uid` replaced by `ctx.session_uid` in `accept` (Task 8's replay test raises `ReceiptConflict`); `task_event_from_row` without `session_uid` (Task 9).
- [ ] Two-leg gate: full suite on the branch tip vs `before.txt` by failure name (no new names); `tests/test_instruction_budget.py` green (AGENTS.md copies, budgets); the harness directly (`harness-before.txt` line and names unchanged — no harness scenario drives these doors); CI (`pytest`, `supported-platforms`, `harness` jobs of `.github/workflows/test.yml`).
- [ ] **Canary-root observation (mandatory, `CLAUDE.md:262`; the doors change what a bot's session writes to the plane).** In the independent canary root (`documentation/validating-bot-changes.md:35-50`; `library/protocols/canary-rollout.md`): seal the branch's release, `claudlobby --fleet <canary> config plan --release RELEASE_ID`, `config diff PLAN_ID`, `host activate PLAN_ID --install-directory PATH`; confirm `grep -c '^export CLAUDLOBBY_RUNTIME=claude$' <canary bot>/bot.conf` prints 1. From the canary **worker's live session** (its Bash tool, which carries `CLAUDE_CODE_SESSION_ID`): the manager admits and assigns a task; the worker runs `claudlobby --json assignment accept ASG --request-id R1`, then `claudlobby --json task show TASK` — the `accepted` history entry carries a `session_uid` that **equals** the `session_uid` in the worker's `data/.plane-session` (the hook derived it from the payload's `session_id`; equality is the main-thread half of C11, measured for free) — and `python3 -S -E claudlobby/_runtime_scripts/plane-lookup.py --root ROOT --by-assignment ASG --any-state` prints it as the trailing column. `/clear` in the worker, then re-run `assignment accept ASG --request-id R1` → `"replayed": true`, exit 0, the row unchanged, `state/requests/<fleet>/R1.json` byte-identical (`sha256sum` before/after) and stamped `"format_version":2`. Repeat the pair for `fleet reports submit --request-id R2` (`communications.sender_session_uid`). From an Agent subagent in the same session: `assignment progress … --request-id R3` → record whether its row carries the uid (the C11 subagent half — with the fail-safe shipped it records none; write the measured `CLAUDE_CODE_SESSION_ID` vs hook id into the run log). Record all of it in the epic's run log (`documentation/plans/2026-10-04-runtime-neutral-observability-run-log.md`, the style of `2026-09-28-unified-cli-run-log.md`, identifiers scrubbed) and **cite the observation in the PR body**. Production is untouched; the canary bot is reaped per `validating-bot-changes.md:53-55`.

## Test Plan

Unit: `test_config.py`, `test_known_values.py`, `test_validator.py`, `test_env_register.py`, `test_composer.py`, `test_plane_contracts.py`, `test_plane_registry*.py`, `test_migration_plan.py`, `test_plane_ids.py`, `test_plane_session_hook.py` (unchanged, pins parity), `test_request_receipts.py`, `test_releases.py`, `test_release_install.py`, `test_operation_context.py`, `test_message_context.py`, `test_task_operations.py`, `test_message_operations.py`, `test_report_payload.py`, `test_task_state.py`, `test_plane_lookup.py`, `test_instruction_budget.py`. CLI: `test_task_write_cli.py`, `test_message_write_cli.py` (the generated-caller env plus `CLAUDE_CODE_SESSION_ID`). The harness as the regression leg; the canary-root observation of Task 11 for behaviour.

## Verification Checklist

- [ ] `claudlobby config validate` on a manifest with `runtime: codex` prints an error containing `execution adapter not shipped`; with `runtime: claude` it does not; `config plan` on the former raises `PlanError`.
- [ ] `claudlobby --json config explain bot.runtime --bot B` answers `built_in` / `set` with no `defaults.runtime`, and `fleet.defaults` with one.
- [ ] `grep -c '^export CLAUDLOBBY_RUNTIME=' <every composed bot.conf>` prints 1.
- [ ] `bot_payload(...)` for a claude bot has no `runtime` key and its `declared_hash` is unchanged from the base commit on the same fixture; `tests/test_plane_registry.py:95-99` `bot_stub` still validates.
- [ ] `derive_session_uid("x") == derive_session_uid("x", runtime="claude")`; `tests/test_plane_session_hook.py::test_bash_derivation_matches_python_byte_for_byte` passes unchanged.
- [ ] A v1 receipt written on the base commit loads on the branch with `intent.session_uid is None` and is never restamped; a receipt written on the branch says `"format_version":2`; `runtime_declaration()["receipt_format"] == {"read": [0, 1, 2], "write": 2}`.
- [ ] After `accept` from session A and a retry of the same `--request-id` from session B: `replayed: true`, `events.session_uid == A`, the receipt file is byte-identical; the same for `fleet reports submit` and `message send`.
- [ ] `caller_session_uid()` returns `None` with the id absent, with `CLAUDLOBBY_RUNTIME=codex`, and under `CLAUDE_CODE_CHILD_SESSION=1` while `SUBAGENT_SHELL_SHARES_SESSION_ID is not True`.
- [ ] `task show --json` history entries carry `session_uid`; `plane-lookup.py --by-assignment ASG --any-state` prints it last.
- [ ] `cmp claudlobby/_runtime_scripts/CLAUDE.md claudlobby/_runtime_scripts/AGENTS.md` is silent; `tests/test_instruction_budget.py` green.
- [ ] Live, in the canary root: the door-recorded uid equals `data/.plane-session`'s `session_uid`; the post-`/clear` retry replays; the run-log entry exists and the PR body cites it.

## What NOT To Do

- Do not compose, launch, hook-wire or validate a Codex bot; do not add a Codex session-id env name — the companion does, on C1's measured names (F11).
- Do not touch `plane-session-start.sh`'s derivation, `data/.plane-session`, or `transcript-digest.sh` — the hook and the digest retire in P3; the doors never read that file (§2.2).
- Do not bump `registry_emit._SCHEMA`, add `session_uid` to `Transmission` or any migration (F1(b) is rejected; no CHECK change, no `0014`).
- Do not make `_existing` compare the session uid, and do not drop `session_uid` from the hashed projection (F17(c) is rejected): the uid is frozen on the intent so the replay proves the same facts.
- Do not attach the uid to the manager-side doors (`admit`, `assign`, `withdraw`, `reassign`, `escalate`, `nudge`, `recheck`, `deliver`, workstreams) in this PR; they can follow the same three lines once F17's shape is in, as their own change.
- Do not flip `SUBAGENT_SHELL_SHARES_SESSION_ID` without the C11 run-log entry; do not scrub `CLAUDE_CODE_CHILD_SESSION` in `start-bot.sh` here (C10, P3).
- Do not hand-edit `AGENTS.md` or `system.yaml.example`; do not write fleet names, session ids or hosts into any committed file or the run log.

## Context

area: config · compose · plane contracts · request receipts · task/message doors — effort: **M** (Half A S, Half B M) — risk: **Medium** (the doors and the receipt format change runtime behaviour; mitigated by the frozen-uid replay, v1 readability, the fail-safe `None`, and the canary-root observation) — priority: P1 of #2145 — related: #1997 (`agent_cli:`, mission line), #1724 (older-daemon keyframe rule), #1747/#1989 (receipt format literals), #2149 (companion), clauDNA#404.

Tasks: 0 worktree (**S**) · **Half A** 1 runtime field + refusal + docs (**S**) · 2 `CLAUDLOBBY_RUNTIME` (**S**) · 3 payload field (**S**) · 4 F2 derivation (**S**) · 5 design-v2 amendments, Q2 (**S**, one conditional step) · **SEAM** · **Half B** 6 receipts format 2 (**M**) · 7 resolver + contexts (**S**) · 8 doors + F17 replay tests (**M**) · 9 readers + instruction rows (**S**) · 10 docs (**S**) · 11 gate + canary root (**M**).

## Canary answers this PR waits on

| Canary | What this PR does before the answer | What changes with the answer |
|---|---|---|
| **C11** — does a subagent's shell (`CLAUDE_CODE_CHILD_SESSION=1`) carry the hook payload's `session_id`? | Ships the fail-safe: `caller_session_uid()` returns `None` under the marker, so a door run from a subagent records no uid, never another session's. Task 11's live step measures the main-thread half (door uid == `.plane-session` uid) and records the subagent half. | Same id → set `SUBAGENT_SHELL_SHARES_SESSION_ID = True` (one constant, both branches already tested) and subagent-run doors join too. Different id → the constant stays, and P3's clauDNA store decides how to represent child sessions. |
| **C10** — does `CLAUDE_CODE_CHILD_SESSION` leak into a bot whose tmux server was started from inside another Claude session? | Nothing; `start-bot.sh` does not scrub env. If it leaks, every door of that bot sees the marker and records no uid — disclosed as `NULL`, not wrong. | A leak means P3's scrub in `start-bot.sh` lands before the guard can be relied on; until then the join is dark for bots started that way, and the run log says so. |
| **C1** — Codex's session-id env name for child shells | `_SESSION_ID_ENV` has only the `claude` row; `CLAUDLOBBY_RUNTIME=codex` yields `None`. | The companion adds the measured name; no change here. |
