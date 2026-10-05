---
title: "P1 — the runtime field, the join key on the doors, and the receipt that survives a retry (Claudlobby)"
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

# P1 — the runtime field, the join key on the doors, and the receipt that survives a retry (Claudlobby)

> **Status:** draft, authored by `/claudna:forge` on 2026-10-04 from epic plan §6 P1 (Claudlobby bullets) and its
> `#### Spec: the request-receipt change for F17`. Code references are to Claudlobby `cd292cb` (the checkout's
> `origin/main`, `4da926d3`, is identical for every line cited). Depends on: nothing in another repo (the Claudron
> boundary-spec PR in §6 P1 is independent). Waits on canaries: **C11** (and C10) for Half B's child-shell guard —
> the fail-safe ships either way; C10 also decides whether the conditional Task 7b runs; see the last section.
> Operator decisions carried: **Q1** (Task 1, the key name) and **D1** (Task 5 Step 2 — the mission amendment,
> proposed as fork F18 in the epic's §16; the PR does not merge before the operator ratifies it). Held on epic
> §16 amendments: Task 6 (A-F17), Tasks 7–8 (A-F1). Reforged 2026-10-05 from ironclad cycle 1 (L1–L12; cross-cutting
> X1, X2, X4, X9, X11, X13–X15) — no fork changed; line numbers below were re-verified against the checkout.

## Summary

Half A gives a bot a declared runtime (`runtime: claude | codex`, default `claude`), carries it into `bot.conf`
as `CLAUDLOBBY_RUNTIME`, puts it on the registry keyframe contract, and teaches `derive_session_uid` the F2 rule
(`"<runtime>:" + id` for non-Claude; Claude byte-identical to today), then records the design-v2 amendments of
epic §1.1. Half B makes the worker's task doors, the report encoder and the message doors write the plane's
session slots (`events.session_uid`, `communications.sender_session_uid`) from **the caller's own session-id env**
(F1(c)), and — because that uid is already inside every request's hashed facts — freezes it on the receipt
(`RequestIntent.session_uid`, receipt format 2, F17(a) — decoder-first across two releases, Task 6) so a retried
`--request-id` after `/clear` replays instead of raising `ReceiptConflict`. A `codex` bot is refused by the validator
("execution adapter not shipped") until the companion (#2149, F11) lands; nothing here launches, composes hooks for,
or validates a Codex bot. The work **splits** at the marked seam: Half A plus the receipt *decoder* is release N;
Half B — the writer flip, the doors, one Python fleet-event row helper (Task 9b) and, if C10 shows a leak, the
`start-bot.sh` scrub (Task 7b) — is release N+1, cut after every host has activated N (F17 decoder-first, Task 6).
The F2 material rule lives in exactly one function, `ids.session_alias` (Task 4); every sibling cites it.

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
- `claudlobby/message_context.py:30-46` `MessageRoute` (15 fields, built positionally at `:156-161`); `:165-178` `HumanReplyRoute`
  (built at `:217-219`). `claudlobby/message_operations.py:449-466` `send_message.run()` replay checks; `:471-474` and `:489-493` the two
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
  `tests/test_release_install.py:118`. **Restamp facts (ironclad, X9):** `_save` serializes `asdict(receipt)` (`:526`) and every
  mutation is `_save(replace(receipt, …))` (`begin_attempt` `:555`, `begin_native_attempt` `:580-582`), so a loaded file is
  rewritten whole on its next write — "a v1 receipt is never restamped" was never true; the decoder is strict
  (`RequestIntent(**intent)` `:429`, `TypeError → ReceiptError` `:449-450`), so a pre-P1 release refuses any file carrying an
  unknown `session_uid` key whatever its stamp says; the planner trusts the stamp (`migration_plan.py:201-205`) and
  `_readability_blockers` (`:69-78`) counts every retained version — `state/requests/` has no prune lane.
- Fleet-event rows (X2): bash `emit_fleet_event` (`_runtime_scripts/lib-common.sh:2118-2170`) composes the one row shape at
  `:2166-2167` — `{"event_type":"system","emitter":<src>,"source_ref":"fleet-events:sha:"+sha256_hex32(legacy line),"fleet":…,
  "occurred_at":<utc>,"payload":{"event":<type>,"subject_kind":…,"subject":…,"data":{"source","legacy_ts","data"}}}`;
  `sha256_hex32` (`:6260-6266`) is the first 32 hex, i.e. `ids.derive_hex` (`plane/ids.py:49-54`). Two hand-rolled Python copies
  exist: `fleet_notification.py:91-97` (`fleet_alert`/`fleet_notice`, `source_ref` from the full 64-hex digest of `request_id`)
  and `message_operations.py:790-797` (`delivery_enter_repaired`). The reader selects by prefix only (`plane-readers.py:1155-1164`
  `FLEET_EVENTS_PREFIX`, `:1208-1229` `legacy_event_row` reads `data.source`/`legacy_ts`/`data.data`), so the digest width is
  not load-bearing. The writer scans: `tests/test_event_type_registry.py:256-278` (`ROW`, `CONDITIONAL`, `VARIABLE_ROWS`,
  `PY_WRITERS`), `:320-330` the positive control, `:363-366` literal-type rule, `:384-399` the system-family tripwire.
- `start-bot.sh:268` `CLAUDE_CMD=". '$BOT_ENV_FILE'; exec $CLAUDE ${CLAUDE_FLAGS:-} --name \"$SESSION_NAME\""` — the pane shell
  sources the env file then execs; the tmux server (and so the pane) inherits the launcher's environment, which is where a
  parent Claude session's `CLAUDE_CODE_CHILD_SESSION=1` would ride in (C10). Text-pin precedent for the script:
  `tests/test_boot_policy_conformance.py:247-269`; subprocess precedent: `tests/test_bot_operations.py:220-235`.
- History (L7): `lib/report-back.sh` wrote `session_uid` from `data/.plane-session` onto its task facts from `86b4a987` (#1372)
  until `c4682f7a` (#1989) deleted the script, unrecorded; the join this PR writes is *restored*, not new.
- Readers: `claudlobby/task_state.py:77-85` reducer `TaskEvent`; `:200-204` `task_event_from_row` copies six columns and drops `session_uid`
  although `:352` selects `e.*`; `commands/task_read.py:69-73` prints `asdict(task)`. `_runtime_scripts/plane-lookup.py:158-178`
  `_by_assignment` over `plane-readers.py:442-449` `_ASG_ROW_COLS`/`_ASG_ROW_SELECT`; no bash consumer of `--by-assignment` remains
  (`task-act.sh` is gone; grep of `_runtime_scripts/*.sh`), but `_runtime_scripts/CLAUDE.md:119` documents its columns and `:116` still says
  "`report-back.sh` attaches `session_uid` to its task facts" — that script no longer exists and nothing attaches the uid.
- Tests construct contexts and routes **positionally**: `tests/test_task_operations.py:31-43` (`TaskOperationContext(context, host, fleet_uid, caller, actors)`),
  `tests/test_message_operations.py:32-50` (`MessageRoute(` with 15 args). A new field must be last, with a default.
- Docs to amend: `documentation/fleet-yaml-schema.md:27` (`effort:` in the shape), `:89-91` (per-bot `account/model/effort`), `:214` (the scalar
  merge rule), `:885-891` (per-field sections); `fleet.yaml.example:58-59`, `:155`; `documentation/environment-variables.md:34-42` (Bot Identity
  table); `PROJECT_MISSION.md:5,11,114` (and the 2026-07-06 ratification paragraph at `:17`, #515 — the amendment form), `README.md:3,12`,
  `CLAUDE.md:3`; design v2 `:59`, `:63` (`process_uid`), `:542`, `:545`, `:588`, `:606` (amendment precedent: the bracketed dated note at
  `:603`; §19's numbered items run 1–8, `:601-608`); `plane/ids.py:25-28` (the `process_uid` comment: "minted fresh per process at
  SessionStart"); `documentation/architecture/observable-plane.md:69`; `documentation/fleet-update-lifecycle.md` (no receipt or rollback
  sentence today; sections end at `## Reference` `:576`). Instruction files: `CLAUDE.md:229` — each `AGENTS.md` is a byte copy, enforced by `tests/test_instruction_budget.py`.
- Prior art: #1997 (the `agent_cli:` proposal and the `runtime` name clash with `activation_runtime.py`/`runtime_admission.py`/`runtime_versions.py`,
  `host update runtime`, `config validate --runtime`) — read from epic §4.1 only; the sandbox refused `gh` (TLS), so this plan did not read the issue itself.

## Implementation Plan

### Dependencies
None in code. Operator decisions: Q1 (key name) before the PR opens; **D1 — the mission decision this plan depends on** (epic §16): Claudlobby's mission as written excludes composing Codex bots (`PROJECT_MISSION.md:5,11,114`) and no ratified fork decides it, so Task 5 Step 2 proposes fork F18's wording and the operator ratifies it by approving the PR — Half A does not merge without it. Amendments A-F17 and A-F1 (epic §16) hold Tasks 6–8 until the operator rules. Canaries C11/C10 before the child-shell guard is relaxed (never before merge); C10 decides whether Task 7b runs. Release N+1 (Half B) waits for every fleet host to have activated release N (Task 6).

### Blocks
P2 (`agent.runtime` in `OTEL_RESOURCE_ATTRIBUTES` reads `bot.runtime`; the intake's derive-not-mint rule sets `subject = session_alias(id, runtime)` and composes `derive_session_uid(…, runtime)` from it; P2's intake events and P3's `session_export._system_event` call Task 9b's `fleet_event_request` — the helper lands here, so neither plan carries a copy), P3 (the clauDNA export's `runtime` joins on the same uid; P3 clauDNA Task 5 waits on Task 7b when C10 shows a leak), the companion #2149 (needs `BotConfig.runtime`, the payload field and the receipt shape). Task 10 opens the Claudron PR that flips register row 10 to shipped.

### Steps

Tasks follow as H3 siblings.

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

### Task 4: `ids.session_alias(platform_session_id, runtime)` — the one F2 rule — and `derive_session_uid` composed from it

**Files:** `claudlobby/plane/ids.py`, `tests/test_plane_ids.py`, `tests/test_plane_session_hook.py` (unchanged, must stay green).

- [ ] **Step 1 (tests first):** `tests/test_plane_ids.py` after `test_session_uid_is_derived_and_stable` (`:33-43`): `test_session_alias_is_the_raw_id_for_claude_and_qualified_otherwise` — `session_alias(x) == x == session_alias(x, "claude")`; `session_alias(x, "codex") == "codex:" + x`; empty/whitespace id or runtime → `ValueError`. `test_session_uid_is_derived_from_the_alias_not_a_second_spelling` — for `runtime in ("claude", "codex")`: `derive_session_uid(x, runtime) == derive_uid("sess", session_alias(x, runtime))` (pins the **composition**, so the rule cannot drift into a second copy); `derive_session_uid(x) == derive_session_uid(x, runtime="claude") == "sess_" + sha256(x)[:32]` (Claude rows keep joining); `derive_session_uid(x, runtime="codex") != derive_session_uid(x)` (two vendors' ids cannot collide); the result matches `ID_PATTERNS["session"]`.
- [ ] **Step 2:** `ids.py:79-89` becomes two functions; `derive_uid` (`:57-60`) already exists and is what `derive_session_uid` now rides:

```python
def session_alias(platform_session_id: str, runtime: str = "claude") -> str:
    """The F2 material (#2145 §2.2): the ONE place the (runtime, session_id) join key is
    spelled. `claude` is the raw id — byte-identical to every row written so far; any
    other runtime is "<runtime>:<id>", so two vendors' ids never collide. P2's intake
    sets its session `subject` from this function; clauDNA and Claudron cite register
    row 10, which states this rule once. The vocabulary is config's
    (known_values.KNOWN_RUNTIMES); this module refuses only an empty runtime."""
    if not platform_session_id or not platform_session_id.strip():
        raise ValueError("empty platform session id — refusing to derive")
    if not runtime or not runtime.strip():
        raise ValueError("empty runtime — refusing to derive")
    return platform_session_id if runtime == "claude" else f"{runtime}:{platform_session_id}"


def derive_session_uid(platform_session_id: str, runtime: str = "claude") -> str:
    """sess_ uid DERIVED from the platform session id (sha256, first 32 hex).
    ... (existing docstring) ...
    #2145 F2: composed from session_alias — never a second spelling of the rule."""
    return derive_uid("sess", session_alias(platform_session_id, runtime))
```

`derive_uid("sess", x)` is `"sess_" + sha256(x).hexdigest()[:32]` (`:49-60`), so a `claude` uid is byte-identical to today's. The bash mirror `plane-session-start.sh:59-70` is **not** touched: it is Claude-only until it retires in P3 (no Codex bot can run before the companion), and `test_bash_derivation_matches_python_byte_for_byte` keeps passing because the default is byte-identical. In the same file, the `process_uid` comment (`:25-28`, "minted fresh per process at SessionStart") gains one dated clause: "*(#2145 P1 note: its only minter, `plane-session-start.sh`, retires in #2145 P3; the `proc_` prefix stays registered so historical rows classify — P3 rewrites this comment.)*" (X13; the design-v2 half is Task 5 Step 1).
- [ ] **Step 3:** Verify: `./.venv/bin/pytest tests/test_plane_ids.py tests/test_plane_session_hook.py -q`. Commit: `feat(plane): session_alias is the one F2 rule; derive_session_uid composes it — claude unchanged, others qualified (#2145 F2)`.

### Task 5: design v2 amendments (epic §1.1), the mission amendment (D1), Half A's CHANGELOG

**Files:** `documentation/plans/2026-08-18-observable-plane-design-v2.md`, `documentation/architecture/observable-plane.md`, `PROJECT_MISSION.md`, `README.md`, `CLAUDE.md` + `AGENTS.md` (byte copy), `CHANGELOG.md`.

- [ ] **Step 1:** design v2, in the bracketed dated-note form of `:603`, each note opening `**[2026-10-04 amendment, #2145 P1]**`: `:59` (the `session_uid` bullet) — the derivation is `(runtime, session_id)`: `session_alias(id, runtime)` (raw id for `claude`, `"<runtime>:" + id` otherwise, F2), one function in `plane/ids.py`; **`:63` (the `process_uid` bullet)** — "`process_uid` is unminted after #2145 P3 retires the SessionStart hook, its only minter; the `proc_` prefix stays registered so historical rows classify; concurrent resumes are not distinguished by the plane — the OTel join is by the normalized session attribute (P2), not by process" (X13); `:542` item 2 and `:545` pilot (b) — the LangSmith plugin pilot is **superseded** (LangSmith is out of scope; telemetry stays local); the tmux-boundary `trace_id`/`span_id` instrumentation is **deferred** (interactive sessions ignore inbound trace context, Codex documents no propagation; the dispatch join is the session uid of §2.2, the envelope columns stay); and **the OTel direction is inverted**: item 2's "export through OTel — authoritative domain record stays local" described the plane *emitting*; under #2145 OTel flows *into* the plane (the runtimes export, Claudlobby's normalization layer and intake consume — P2), and the outbound half is **deferred, not superseded** — nothing reads an outbound stream today and §11 of #2145 keeps telemetry on the local box (answers ironclad E23); `:588` Phase 3 — "OTel + LangSmith" becomes "native OTel normalized into the plane (epic P2)"; `:606` — the SessionStart hook and `process_uid` minting are **superseded**: the doors derive the uid from the caller's own session-id env (F1(c)); the hook retires with the digest in P3. **§19 gains a numbered item 9** (after item 8, `:608`; the superseded SessionStart ruling is item 6, `:606`): "**9. #2145 P1 (2026-10-04):** item 6's SessionStart-hook mechanics are superseded by F1(c) — the task, report and message doors derive the caller's uid from its own session-id env, runtime-qualified by `session_alias` (F2); `process_uid` is unminted once the hook retires (P3); §12 item 2's OTel direction is inverted (inbound, P2) and its outbound half deferred. The join this restores was first written by `lib/report-back.sh` from `data/.plane-session` (`86b4a987`, #1372) and silently lost when `c4682f7a` (#1989) deleted the script." One line in §9b (`:409-426` region header): "`session_usage`/`utilization_windows`: decided on P2's evidence". `observable-plane.md:69`: "pinned byte-identical to `ids.derive_session_uid`" → "pinned byte-identical to `ids.derive_session_uid(id)` for runtime `claude`; other runtimes derive in Python only, through `ids.session_alias` (#2145 F2)".
- [ ] **Step 2 — the mission amendment (D1; unconditional before merge; the operator ratifies by approving this PR).** Claudlobby's mission excludes this epic's premise — `PROJECT_MISSION.md:114` ("Per-bot LLM provider abstraction. Claudlobby is for Claude Code specifically …"), `:5` ("a fleet of always-on Claude Code bots"), `:11` ("operating Claude Code bots in production"), `README.md:3` ("A **compositor** for Claude Code agent fleets"), `:12` ("Runs anywhere Claude Code does"), `CLAUDE.md:3` ("Compositor for Claude Code agent fleets") — and no ratified fork decides it: F11 decides *where* Codex launching lives, not *whether* Claudlobby composes Codex bots, so the former "record that F11 supersedes the line" option is **removed**. Proposed, as the epic's §16 D1 records it: a new fork **F18** — "Claudlobby composes and supervises agent CLIs: Claude Code today, Codex through an execution adapter (#2149); the model stays each CLI's concern; no LLM-provider abstraction beneath the CLI" — written into the mission in the 2026-07-06 form (`PROJECT_MISSION.md:17`, #515: an italic dated ratification paragraph, placed after `:17`, naming #2145 and F18), and the lines it amends, all in this PR and coherently: `:5` → "a fleet of always-on agent-CLI bots — Claude Code today, Codex through its execution adapter (#2149) — on a single Linux or macOS host"; `:11` → "The reference runtime for operating agent-CLI bots in production"; `:114` → `- **Per-bot LLM provider abstraction.** Claudlobby composes and supervises agent CLIs — Claude Code today, OpenAI Codex through its execution adapter (#2145 F18/F11, #2149) — and the model stays each CLI's concern. It does not abstract LLM providers beneath the CLI, and a bot that is not an agent CLI belongs in a different framework.`; `README.md:3` → "A **compositor** for agent-CLI fleets (Claude Code today; Codex through #2149)"; `README.md:12` → "Runs anywhere the composed CLI does: Mac mini, Linux box, Raspberry Pi 5."; `CLAUDE.md:3` → "Compositor for agent-CLI fleets — Claude Code today, Codex through the execution adapter (#2149). Transforms …", then `cp CLAUDE.md AGENTS.md` (`tests/test_instruction_budget.py`). Coordinate with `origin/codex/974-mission-consolidation` (#974 rewrites the mission and keeps `:114`'s line): whichever merges second carries the F18 paragraph and the three one-liners. The PR body quotes the F18 text and asks for ratification in so many words; the merge is the ratification. (The clauDNA sibling, D2, is the P1 clauDNA plan's Task 5.)
- [ ] **Step 3:** `CHANGELOG.md` `[Unreleased]`: `### Added — a bot declares its runtime, and the plane's session uid is runtime-qualified (#2145 P1, Half A)` with one bullet per Task 1–4 (the refusal wording, the env name, the keyframe rule, `session_alias` + the F2 derivation), one for the design-v2 amendments, one for the mission amendment (F18, D1), and one for Task 6 Step 2a's decoder ("reads receipt format 2 ahead of writing it; see Half B").
- [ ] **Step 4:** Commit: `docs: record the #2145 amendments to design v2, the runtime vocabulary, and the F18 mission amendment (P1 Half A)`.

---

**SEAM — the work splits here, and the split is required at the release level.** Half A (Tasks 1–5, plus Task 6 **Step 2a**, the receipt *decoder*) is release N, complete and shippable alone. Half B (Task 6 Step 2b and Tasks 7–11, with 7b and 9b) reads `CLAUDLOBBY_RUNTIME`, calls `derive_session_uid(…, runtime)` and flips the receipt *writer* to format 2 — it is release N+1, and its PR opens only after every fleet host has activated N (F17 decoder-first: a host still on pre-N cannot read the first v2 receipt N+1 writes, and `host migrate` would plan that rollback as blocked — Task 6). Half B's Dependencies line names Half A's release.

### Task 6: the receipt carries the uid — `RequestIntent.session_uid`, format 2 decoder-first across two releases, two literals become one (F17)

> **Held:** this task implements F17 as ratified; amendment A-F17 (epic §16) proposes otherwise. Do not start it before the operator rules.

**Files:** `claudlobby/runtime_versions.py`, `claudlobby/request_receipts.py`, `tests/test_request_receipts.py`, `tests/test_releases.py`, `tests/test_migration_plan.py`.

**The (a)-correct shape (ironclad X9; the epic's F17 spec is corrected the same way).** The old text — "a loaded v1 receipt keeps `format_version: 1` and is never restamped" — was false: every mutation is `_save(replace(receipt, …))` and `_save` serializes `asdict(receipt)` (`:519-527`, `:555`), so a v1 file touched by a release that knows `session_uid` would gain the key while keeping the `1` stamp, and a pre-P1 decoder (`RequestIntent(**intent)`, `:429`) would raise `TypeError → ReceiptError` (`:449-450`) on a file the planner had passed as readable (`migration_plan.py:201-205`). Three rules fix it: (1) **decoder first, writer later** — release N ships `SUPPORTED = {0, 1, 2}` with the writer still at 1 (Step 2a, with Half A); release N+1 flips `RECEIPT_FORMAT_VERSION` to 2 (Step 2b, with Half B) once every host has activated N; (2) **`_save` stamps the current `FORMAT_VERSION` on every write and serializes the intent in that format's shape** — format 1 carries no `session_uid` key, format 2 does — so the stamp is always true for the bytes; (3) the **rollback consequence is disclosed**: once N+1 writes its first v2 receipt, `host migrate` to any release before N is blocked for as long as that receipt is retained, and `state/requests/` has no prune lane, so in practice permanently; N stays a legal rollback target (Task 10 writes this into the CHANGELOG and `documentation/fleet-update-lifecycle.md`; Task 11 observes it).

- [ ] **Step 1 (tests first; release N unless marked N+1):** `tests/test_request_receipts.py::test_uuid_conflicts_and_private_digest_only_storage` (`:119-131`): the `format_version = 2` leg now **loads**; add `3` → refused; `replace(intent, session_uid="sess_zz")` → `ReceiptError`; `replace(intent, session_uid="sess_" + "a"*32)` prepares under a fresh request id and round-trips through `load()`. New `test_a_v1_receipt_loads_and_its_next_write_is_stamped_in_the_current_format`: write the store's file by hand as a v1 document (`format_version: 1`, `intent` without `session_uid`) → `store.load().intent.session_uid is None`, `.format_version == 1`; after `store.begin_attempt()` the file says `format_version: FORMAT_VERSION` and — parametrized over `monkeypatch.setattr(rr, "FORMAT_VERSION", v)` for `v in (1, 2)` — the intent has **no** `session_uid` key when `v == 1` and has it when `v == 2`; a **new** receipt from `prepare` is stamped `FORMAT_VERSION` too. New `test_a_format_1_file_never_carries_session_uid`: with `FORMAT_VERSION == 1`, `prepare(replace(intent, session_uid="sess_" + "a"*32))` → the file has no `session_uid` key and `load().intent.session_uid is None` (the uid is not durable until N+1 — which is why the doors ship with N+1). `tests/test_releases.py:95`: stays `"receipt_format": 1` in N; `2` in N+1. `tests/test_migration_plan.py`, release N: a hand-written v2 receipt beside the v1 one → `inventory["versions"] == [1, 2]`, `plan.readability_blockers(target_N.compatibility) == ()` (N reads 2), and a target declaring `{"read": [0, 1], "write": 1}` (pre-N) → `("target cannot read retained receipt_format: 2",)` in blockers; keep the `:416-421` parametrization (`{"read": [1], "write": 1}` still a blocker). **Release N+1 — the `host migrate` preview test for the N+1 → N pair:** a receipt written by N+1's code (format 2) and a rollback target carrying N's declaration `{"read": [0, 1, 2], "write": 1}` → `readability_blockers(...) == ()` (N is a legal target); the pre-N declaration → the blocker (the permanent one). N+1 also moves the writer pins: `:232` → `"receipt_format"] == 2`; `:233` and `:363` → `"unsupported receipt_format: 2"`; `:357-358` → `[2]` and `format_version == 2`. `tests/test_release_install.py:118` is unchanged in both and must stay green (the sealed manifest picks the declaration up).
- [ ] **Step 2a (release N, ships with Half A) — the decoder and the honest `_save`.** `runtime_versions.py:22` → `SUPPORTED_RECEIPT_FORMAT_VERSIONS = frozenset({0, 1, 2})` (`:21` stays `1`); docstring `:6-7` → "Receipt v1 and v2 use request_receipts; v2 adds intent.session_uid (#2145 F17). Readable from release N, written from N+1 — decoder-first, per :10; v1 is back-filled as None." `request_receipts.py:23-25`:

```python
from .plane.ids import ID_PATTERNS
from .runtime_versions import RECEIPT_FORMAT_VERSION as FORMAT_VERSION, SUPPORTED_RECEIPT_FORMAT_VERSIONS

_READABLE_FORMATS = SUPPORTED_RECEIPT_FORMAT_VERSIONS - {0}   # 0 means "absent", never a file
```

`RequestIntent` (`:142`) gains the last field `session_uid: str | None = None  # #2145 F17: the caller's session uid at the first attempt; a replay reuses it. Serialized only in format >= 2`. `_validate` (`:300-301`) → `if type(receipt.format_version) is not int or receipt.format_version not in _READABLE_FORMATS: raise ReceiptError("unsupported request receipt format")`; after the `message_id` loop (`:309-312`): `if intent.session_uid is not None: _id(intent.session_uid, "session")`. `_decode` (`:416-418`): back-fill `"session_uid": None` the same way `expected_by` is, so `:446-447` holds for every file on disk. **`_save` (`:518-533`) changes** — before: `stream.write((json.dumps(asdict(receipt), …`; after:

```python
    def _save(self, receipt):
        receipt = replace(receipt, format_version=FORMAT_VERSION)   # the stamp is the format this code writes — always
        self.assert_locked()
        _validate(receipt)
        self._check_route_root(receipt)
        document = asdict(receipt)
        if FORMAT_VERSION < 2:
            document["intent"].pop("session_uid", None)   # a format-1 file has no such key: a pre-P1 decoder is strict
        ...
                stream.write((json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode())
```

and `_save` returns the restamped `receipt`, so every caller's in-memory copy agrees with the file. A loaded v1 is thus rewritten as the current format on its next write — in N that is `1` again without the key (bytes identical to a v1 writer's), in N+1 it is `2` with `session_uid: null` — and the planner's stamp is true either way.
- [ ] **Step 2b (release N+1, ships with Half B) — the writer flip.** `runtime_versions.py:21` → `RECEIPT_FORMAT_VERSION = 2`; the test pins of Step 1 marked N+1. Nothing else moves: the decoder, `_save` and the field were right since N.
- [ ] **Step 3:** Verify: `./.venv/bin/pytest tests/test_request_receipts.py tests/test_releases.py tests/test_migration_plan.py tests/test_release_install.py tests/test_request_facts.py -q`, in both releases. Commits: N — `feat(receipts): read format 2 ahead of writing it — intent.session_uid decodes, _save stamps the format it writes (#2145 F17, release N)`; N+1 — `feat(receipts): write format 2 — the intent freezes the caller's session uid (#2145 F17, release N+1)`.

### Task 7: one resolver, two binders — `caller_session_uid()` on the contexts

> **Held:** this task implements F1(c) as ratified; amendment A-F1 (epic §16) proposes otherwise. Do not start it before the operator rules.

**Files:** `claudlobby/operation_context.py`, `claudlobby/task_operations.py`, `claudlobby/message_context.py`, `tests/test_operation_context.py`, `tests/test_message_context.py`, `tests/test_task_operations.py`.

- [ ] **Step 1 (tests first):** `tests/test_operation_context.py`: `test_caller_session_uid_reads_the_runtime_env_and_records_nothing_rather_than_a_wrong_uid` — with `CLAUDE_CODE_SESSION_ID=<id>` and no `CLAUDLOBBY_RUNTIME` → `derive_session_uid(id)`; with `CLAUDLOBBY_RUNTIME=claude` the same; with the id absent, empty or whitespace → `None`; with `CLAUDLOBBY_RUNTIME=codex` → `None` (no Codex session env is known until C1 records it — the companion adds the row); with `CLAUDLOBBY_RUNTIME=gemini` → `None`; with `CLAUDE_CODE_CHILD_SESSION=1` → `None` while `SUBAGENT_SHELL_SHARES_SESSION_ID` is not `True`, and the uid when the test monkeypatches it `True` (the C11 flip, pinned both ways). `test_generated_origin_binds_the_callers_session_uid`: `resolve_task_mutation_context` under the generated env of `:101-120` plus `CLAUDE_CODE_SESSION_ID` → `ctx.session_uid == derive_session_uid(id)`; without it `None`. `tests/test_task_operations.py`: `TaskOperationContext(..., session_uid="sess_zz")` → `TaskQueryError`. `tests/test_message_context.py::test_generated_route_keeps_caller_and_peer_ids_with_plane_absent` (`:99`): set the env → `bare.session_uid == derive_session_uid(id)`.
- [ ] **Step 2:** `operation_context.py` — two import edits first (L5; today `derive_session_uid` would `NameError` and `Mapping` is unimported): `:25` `from .plane.ids import ID_PATTERNS, derive_uid` → `from .plane.ids import ID_PATTERNS, derive_uid, derive_session_uid`, and `from collections.abc import Mapping` inserted after `from contextlib import closing` (`:14`); `import os` is already there (`:15`). Then:

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

`bind_task_context` `:136` → `return TaskOperationContext(destination, host_uid, fleet_uid, caller, bots, caller_fleet_uid, session_uid=caller_session_uid())`. `task_operations.TaskOperationContext` (`:46-52`) gains the last field `session_uid: str | None = None`, validated in `__post_init__` with `ID_PATTERNS["session"]` when not `None` (`TaskQueryError("operation context requires a canonical session uid")`). `message_context.MessageRoute` (`:46`) and `HumanReplyRoute` (`:178`) gain the last field `session_uid: str | None = None`; `resolve_message_route` (`:156-161`) and `resolve_human_reply_route` (`:217-219`) pass `session_uid=caller_session_uid()`. One resolver, called by the two binders; the operations modules keep **zero** env reads. **Why a trailing field on the three contexts and not `TaskActor.session_uid` (L5):** `TaskActor` is *who* — a `(uid, alias)` pair shared by `caller`, `recipient`, `manager`, every entry of the `bots` dict and `_identities` (which registers every actor it is handed); the session is *where the caller's act ran* and applies to the caller alone, so a slot on `TaskActor` would be `None` on every actor but one and would ride into `parties`/`_identities` for no reason. Trailing fields with defaults also leave the positional test constructors (`tests/test_task_operations.py:31-43`, `tests/test_message_operations.py:32-50`) untouched.
- [ ] **Step 3:** Verify: `./.venv/bin/pytest tests/test_operation_context.py tests/test_message_context.py tests/test_task_operations.py tests/test_message_operations.py -q`. Commit: `feat(context): the operation context and message routes carry the caller's own session uid (#2145 F1)`.

### Task 7b: the C10 scrub — `start-bot.sh` unsets `CLAUDE_CODE_CHILD_SESSION` before `exec $CLAUDE` (conditional on C10)

**Runs only if C10 shows the marker leaking** into a bot whose tmux server was started from inside a Claude session; if C10 shows no leak, this task is recorded in the run log as "skipped: C10 = no leak" and nothing lands. It unsets **only the child marker** — not `CLAUDE_CODE_SESSION_ID`, which the bot's own `claude` process owns for its children. P3 clauDNA's Task 5 names this task as what it waits on (X3/X4); the 0.28 clauDNA release does not wait.

**Files:** `claudlobby/_runtime_scripts/start-bot.sh`, `tests/test_boot_policy_conformance.py`, `CHANGELOG.md`.

- [ ] **Step 1 (tests first):** beside `TestStartBotShReadyTimeoutFloor` (`tests/test_boot_policy_conformance.py:250-269`, which reads the shipped script's text), `TestStartBotShScrubsTheChildMarker`: (a) `test_the_launch_command_unsets_the_child_marker` — `re.search(r"CLAUDE_CMD=\". '\$BOT_ENV_FILE'; unset CLAUDE_CODE_CHILD_SESSION; exec \$CLAUDE ", text)` matches once; (b) `test_a_bot_started_from_inside_a_claude_session_sees_no_marker` — take the `CLAUDE_CMD` value from the script text, substitute `$BOT_ENV_FILE` with an empty temp file and `$CLAUDE` with a stub script (`#!/bin/sh\nprintenv CLAUDE_CODE_CHILD_SESSION; printenv CLAUDE_CODE_SESSION_ID`), run it with `subprocess.run(["/bin/bash", "-c", cmd], env={..., "CLAUDE_CODE_CHILD_SESSION": "1", "CLAUDE_CODE_SESSION_ID": "parent"}, …)` (the `tests/test_bot_operations.py:220-235` shape) → stdout is `\nparent\n`: the marker is gone, the id untouched. Red before Step 2.
- [ ] **Step 2:** `start-bot.sh:268` — before: `CLAUDE_CMD=". '$BOT_ENV_FILE'; exec $CLAUDE ${CLAUDE_FLAGS:-} --name \"$SESSION_NAME\""`; after: `CLAUDE_CMD=". '$BOT_ENV_FILE'; unset CLAUDE_CODE_CHILD_SESSION; exec $CLAUDE ${CLAUDE_FLAGS:-} --name \"$SESSION_NAME\""`, with the comment above it gaining: "`CLAUDE_CODE_CHILD_SESSION` is scrubbed in the pane shell, after the env file and right before exec: a tmux server started from inside a Claude session inherits that session's marker (#2145 C10), and a bot carrying it would record no session uid on any door (`operation_context.caller_session_uid`). Only the marker — the bot's own `claude` sets `CLAUDE_CODE_SESSION_ID` for its children." Bash 3.2 rules apply (`unset` is POSIX).
- [ ] **Step 3:** Verify: `./.venv/bin/pytest tests/test_boot_policy_conformance.py -q`; `bash -n claudlobby/_runtime_scripts/start-bot.sh`; the harness (`harness/validate-bot-change.sh`) unchanged by name. CHANGELOG (Task 10) gains one line. Commit: `fix(start-bot): scrub CLAUDE_CODE_CHILD_SESSION before exec — a bot started from inside a Claude session is not a child (#2145 C10)`.

### Task 8: the doors write the join key, and a retry reuses the frozen one (F1(c), F17)

> **Held:** this task implements F1(c) and F17(a) as ratified; amendments A-F1 and A-F17 (epic §16) propose otherwise. Do not start it before the operator rules.

**Files:** `claudlobby/task_operations.py`, `claudlobby/report_payload.py`, `claudlobby/message_payload.py`, `claudlobby/message_operations.py`, `tests/test_task_operations.py`, `tests/test_report_payload.py`, `tests/test_message_operations.py`, `tests/test_task_write_cli.py`, `tests/test_message_write_cli.py`.

**Which test proves F17 (L6).** When the first attempt *committed*, `accept` returns from `_existing`/`_replayed` before `_prepare` (`task_operations.py:449-454`), so a committed-then-retried test passes with or without the frozen uid; the conflict at `request_receipts.py:542-543` fires only when the previous receipt is **prepared but unrecorded** and the retry recomputes an intent whose facts differ by the uid. Each door therefore gets two legs: the committed replay (the behaviour users see) and the prepared-unrecorded retry (the proof). "The receipt file bytes are unchanged" is dropped everywhere — the retry's `begin_attempt` rewrites the file (`:555`); what holds is `intent.session_uid` unchanged and the stamp `FORMAT_VERSION`.

- [ ] **Step 1 (tests first).** Operations level, with `worker = replace(ctx, caller=ctx.bots["worker"], session_uid=A)` and `B` a second uid:
  - `test_accept_records_the_callers_session_and_a_retry_from_a_new_session_replays` (beside `:332`): `accept(worker_A, rid, asg)` → `events.session_uid == A` (read the row); `accept(replace(worker_A, session_uid=B), rid, asg).replayed` is `True`, no `ReceiptConflict`, the row still carries `A`, `_receipt(ctx, rid).intent.session_uid == A`; a **new** request from `B` on a fresh assignment carries `B`; a caller with `session_uid=None` writes `NULL` and its retry from `A` also replays (the pre-P1 receipt case: the frozen `None` is reused).
  - `test_accept_prepared_in_session_a_and_retried_from_session_b_commits_with_a` — **the F17 leg:** drive `accept(worker_A, rid, asg)` with `_commit` monkeypatched to raise `RuntimeError("crash")` after `_prepare` returned (the receipt is on disk, prepared, `stages[0]` unrecorded — the equivalent hand state is `store.outcome(0, "unrecorded")`); then `accept(replace(worker_A, session_uid=B), rid, asg)` **commits** (`replayed` is `False`), raises no `ReceiptConflict` from `request_receipts.py:542-543`, and the row carries `A` — the uid frozen at the first attempt, not `B`. Mutant: `previous.intent.session_uid` → `ctx.session_uid` in `accept` makes this leg raise `ReceiptConflict` (Task 11).
  - `test_assignment_reports_record_the_session_on_both_facts_and_replay_across_a_clear` (beside `:783`): `progress(worker_A, …)` → `communications.sender_session_uid == A` and the linked task row's `session_uid == A`; the retry from `B` replays with the same `message_id` and both rows unchanged; plus the prepared-unrecorded leg for `_assignment_report` in the same shape as `accept`'s.
  - `tests/test_report_payload.py` (beside `:70`): `_facts(report, link, session_uid=A)` puts `sender_session_uid` on the communication and `session_uid` on the task detail; unlinked: on the communication only, and the `system` marker dict is byte-identical to today (`:207-212`, no session slot).
  - `tests/test_message_operations.py`: `test_send_records_the_sender_session_and_a_retry_from_a_new_session_replays` — `route_A = replace(route, session_uid=A)`; send → `sender_session_uid == A`; the same `request_id` with `replace(route, session_uid=B)` and `retry_uncertain=True` → `replayed`, one native send, no `ReceiptConflict` at `:494-495`; **the prepared-unrecorded leg:** `store.prepare` done from `route_A` and the native send never attempted (fail the transport stub before `begin_native_attempt`), then the retry from `route_B` with `retry_uncertain=True` recomputes facts equal to the stored ones (`:494`) and sends once; the same pair for `send_unlinked_report` (`fleet reports submit`, beside `:329`) and for `record_reply_to_human` (`:667-669`).
  - CLI level (one test each in `tests/test_task_write_cli.py` and `tests/test_message_write_cli.py`): the generated-caller env (`_generated`, `tests/test_message_write_cli.py`) plus `monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", id)`; `assignment accept --request-id R` writes `derive_session_uid(id)`; `setenv` a second id and repeat → `replayed: true`; `fleet reports submit` the same. These are the tests the epic's spec names ("the F17 test itself", for the task, message and report doors).
- [ ] **Step 2 — task doors.** `_prepare` (`:320-331`) gains `session_uid=None` and passes `session_uid=session_uid` into `RequestIntent`. `accept` (`:445-463`): `session_uid = previous.intent.session_uid if previous else ctx.session_uid` (the `expected_by` pattern, `:267-278`); the payload dict (`:460-461`) gains `session_uid=session_uid`; `_prepare(..., session_uid=session_uid)`. `_assignment_report` (`:476-524`): the same line beside `message_id`/`event_ids` (`:511-513`); `encode_report_facts(..., session_uid=session_uid)`; `_prepare(..., session_uid=session_uid)`. `_existing` (`:245-260`) is **unchanged** — a different uid is the same request, retried. `report_payload.encode_report_facts` (`:168-170`) gains `session_uid: str | None = None`; when not `None`: `comm["sender_session_uid"] = session_uid` (`:189-190`) and, linked only, `detail["session_uid"] = session_uid` (`:198-199`). A `None` leaves both dicts byte-identical to today.
- [ ] **Step 3 — message doors.** `message_payload.encode_communication` (`:145-152`): `if intent.session_uid is not None: payload["sender_session_uid"] = intent.session_uid` — it already takes the intent, which is also how a replay reuses the frozen value (`intent = existing.intent`, `:497`; re-verified at the checkout, X15). `message_operations.send_message.run()` (`:449-500`): `session_uid = old.session_uid if existing is not None else route.session_uid` beside `message_id`; both `RequestIntent(…)` constructions (`:471-474`, `:489-493`) gain `session_uid=session_uid`; the unlinked report passes `session_uid=session_uid` to `encode_report_facts` (`:476-479`). `record_reply_to_human` (`:647-675`): `old.session_uid` / `route.session_uid` into the draft and the prepared intent. The recomputed facts then equal the stored ones and `:494-495` / `:667-669` no longer fire on a changed session.
- [ ] **Step 4:** Verify: `./.venv/bin/pytest tests/test_task_operations.py tests/test_message_operations.py tests/test_report_payload.py tests/test_task_write_cli.py tests/test_message_write_cli.py tests/test_plane_cutover_reports.py tests/test_request_facts.py -q`. Commit: `feat(doors): accept, the assignment reports, reports submit and message send record the caller's session; a retry after /clear replays (#2145 F1 F17)`.

### Task 9: the readers show it — `task show`, `plane-lookup.py --by-assignment`, `plane-lookup.py --session`

Follows Task 8 (its rows are what these readers show) and is held with it.

**Files:** `claudlobby/task_state.py`, `claudlobby/_runtime_scripts/plane-readers.py`, `claudlobby/_runtime_scripts/plane-lookup.py`, `claudlobby/_runtime_scripts/CLAUDE.md` + `AGENTS.md`, `documentation/architecture/observable-plane.md`, `tests/test_task_state.py`, `tests/test_plane_lookup.py`.

- [ ] **Step 1 (tests first):** `tests/test_task_state.py`: an event inserted with `session_uid="sess_" + "1"*32` (`_insert`, `:22`) surfaces as `history[i].session_uid` and in `asdict(task)["history"][i]["session_uid"]`; a row without it reads `None`. `tests/test_plane_lookup.py::test_by_assignment_returns_open_only_unless_any_state` (`:113`): the lines gain a trailing `-`; new `test_by_assignment_prints_the_executing_session`: emit an `accepted` task event with `session_uid` for the assignment → the trailing column is that uid; a later `nudged` event with no uid leaves it (the newest **non-null** wins — a manager's act from an operator shell carries none, and the reader asks which session executed). New `test_session_mode_lists_what_one_session_wrote` (L10): emit an `accepted` task event and a communication carrying uid `A`, another pair carrying `B`; `--session A` prints one `task <work_item> <assignment> accepted <occurred_at>` line and one `message <msg_id> <recipient> <occurred_at>` line, nothing of `B`'s; a value not matching `^sess_[0-9a-f]{32}$` exits 2 with a usage note naming the one-liner below; an unknown uid exits 0 with empty stdout and a stderr note (the unreachable-is-not-empty rule).
- [ ] **Step 2:** `task_state.TaskEvent` (`:77-85`) gains `session_uid: str | None` after `successor_id`; `task_event_from_row` (`:200-204`) adds `"session_uid"` to its tuple (the column is already selected, `:352`); `task_read.py` is untouched — `asdict(task)` carries it. `plane-readers.py:442-449`: `_ASG_ROW_COLS += ("session_uid",)` and `_ASG_ROW_SELECT` gains `, (SELECT e.session_uid FROM events e WHERE e.kind='task' AND e.assignment_id=a.assignment_id AND e.session_uid IS NOT NULL ORDER BY e.ingest_seq DESC LIMIT 1) AS session_uid`. `plane-lookup.py:158-178` prints ` {row['session_uid'] or '-'}` last; docstring and `--by-assignment` help list the column. **`--session <sess_uid>` (L10):** a new mode beside `--by-assignment` (`:338`), `_by_session(a)` under the same `_with_plane` ladder (`:57`), two read-only queries — `events WHERE kind='task' AND session_uid=?` and `communications WHERE sender_session_uid=?` — printed oldest first in the two line shapes above. It takes the **uid form only**: the script is stdlib (`:24-32`, no package import) and the F2 rule lives in exactly one function (Task 4), so the raw-id → uid step is the documented one-liner `python3 -c 'from claudlobby.plane.ids import derive_session_uid as d; print(d("<raw id>", "<runtime>"))'`, not a second spelling. The plane stores no raw id (the uid is a one-way hash; the raw id lives in the runtime's transcript name and raw telemetry), so "print `session_id` beside `session_uid`" is honestly impossible from P1's rows — the place a raw id *is* known to the plane is the receiver hook's payload, which is what A-F1 (epic §16) proposes writing onto the received `Transmission`; if ratified, that task adds the raw id to this mode's output. `observable-plane.md` Identity section (`:60-69`): one sentence — "`plane-lookup.py --session <sess_uid>` lists what one session wrote (task events, communications); the `state/otel/` cost query joins on the same uid and lands with the raw files (P2)." `_runtime_scripts/CLAUDE.md:119`: the by-assignment column list gains `<session_uid|->` and a `--session` sentence; `:116`: replace "where `report-back.sh` attaches `session_uid` to its task facts" with "(read only by `transcript-digest.sh`; `report-back.sh` attached the uid from `86b4a987` until `c4682f7a` deleted it; the Python doors now derive the caller's uid from its own session-id env, #2145 F1(c), and never from this file)". Then `cp claudlobby/_runtime_scripts/CLAUDE.md claudlobby/_runtime_scripts/AGENTS.md`.
- [ ] **Step 3:** Verify: `./.venv/bin/pytest tests/test_task_state.py tests/test_task_audit.py tests/test_task_queries.py tests/test_plane_lookup.py tests/test_task_read_cli.py tests/test_instruction_budget.py -q`. Commit: `feat(readers): task show, plane-lookup --by-assignment and --session name the session that ran the work (#2145)`.

### Task 9b: one Python fleet-event row helper — `plane/fleet_events.py`, bash parity, the two copies adopt it

The row shape `emit_fleet_event` writes (`lib-common.sh:2166-2167`) has one bash writer and two hand-rolled Python copies (`fleet_notification.py:91-97`, `message_operations.py:790-797`); P2's intake events and P3's `session_export._system_event` would have been the third and fourth, each with its own `fleet-events:<name>:…` sub-grammar. The digest is being retired because a writer got this shape wrong (#1456/#1503). One helper, here, in whichever half ships first in practice (Half B); P2 and P3 call it and add no sub-grammar — `event_id` carries the dedup key and `fleet-events:sha:<hex>` is the only provenance form (X2).

**Files:** `claudlobby/plane/fleet_events.py` (new), `tests/test_plane_fleet_events.py` (new), `claudlobby/fleet_notification.py`, `claudlobby/message_operations.py`, `tests/test_event_type_registry.py`.

- [ ] **Step 1 (tests first) — bash/Python parity in the style of `tests/test_plane_session_hook.py:31-46`:** `test_the_python_row_is_the_bash_row_byte_for_byte` runs `bash -c` with `source claudlobby/_runtime_scripts/lib-common.sh; plane_armed() { return 0; }; ts_iso() { printf '2026-10-05T00:00:00Z'; }; date() { printf '2026-10-05T00:00:00Z\n'; }; plane_emit_bounded() { printf '%s' "$3" > "$CAPTURE"; }; FLEET_NAME=f1 emit_fleet_event vault_sync test-src '{"a":1}' "" bot1` (shell functions shadow the commands, so the two clocks are pinned), parses `$CAPTURE` and asserts `json.loads(captured)["events"][0] == fleet_event_request("vault_sync", fleet="f1", subject_kind="actor", subject="bot:f1/bot1", bot="bot1", source="test-src", data={"a": 1}, occurred_at="2026-10-05T00:00:00Z", legacy_ts="2026-10-05T00:00:00Z")` minus `event_id` (bash mints none; the daemon does) — every other key equal, `source_ref` included; the same for the fleet anchor (`bot_dir` and `bot_id` empty → `subject_kind fleet`, subject `f1`, bot `fleet`) and the host anchor (no `FLEET_NAME` → fleet `_host`, `subject_kind host`, bot `host`, subject = `hostname`). `test_source_ref_is_derive_hex_of_the_legacy_line` pins `source_ref == "fleet-events:sha:" + derive_hex('{"ts":"…","bot":"bot1","type":"vault_sync","source":"test-src","data":{"a":1}}')` with the legacy line composed exactly as `printf -v _line` composes it (`:2160-2161`; `data` is the compact `json.dumps(data, separators=(",", ":"))`). `test_a_caller_may_supply_its_own_key` — `key="req-123"` → `derive_hex("req-123")`, the two adopters' materials.
- [ ] **Step 2 — the module** (stdlib + `plane.ids`; the daemon spools and ingests it like any row):

```python
"""One Python spelling of the fleet-event row bash `emit_fleet_event` writes
(lib-common.sh emit_fleet_event; parity-pinned). Readers select these rows by the
`fleet-events:` prefix and read payload.data.{source,legacy_ts,data} (plane-readers.py
FLEET_EVENTS_SQL / legacy_event_row), so every Python writer must build exactly this."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .ids import derive_hex, mint_event_id


def fleet_event_request(event_type: str, *, fleet: str, subject_kind: str, subject: str, source: str,
                        data: dict, bot: str | None = None, occurred_at: str | None = None,
                        legacy_ts: str | None = None, event_id: str | None = None,
                        key: str | None = None) -> dict:
    """The row as bash composes it. `source_ref` is "fleet-events:sha:" + derive_hex(key), where
    `key` defaults to the legacy ledger line {ts, bot, type, source, data} — the content key the
    retired file ledgers used; a caller with its own idempotency material (a request id) passes it."""
    now = occurred_at or datetime.now(timezone.utc).isoformat()
    ts = legacy_ts or now
    who = bot or ("fleet" if subject_kind == "fleet" else "host" if subject_kind == "host" else subject.rsplit("/", 1)[-1])
    compact = json.dumps(data, separators=(",", ":"))
    legacy = f'{{"ts":"{ts}","bot":"{who}","type":"{event_type}","source":"{source}","data":{compact}}}'
    return {"event_id": event_id or mint_event_id(), "event_type": "system", "emitter": source,
            "source_ref": "fleet-events:sha:" + derive_hex(key or legacy), "fleet": fleet, "occurred_at": now,
            "payload": {"event": event_type, "subject_kind": subject_kind, "subject": subject,
                        "data": {"source": source, "legacy_ts": ts, "data": data}}}
```

- [ ] **Step 3 — the two copies adopt it, in this PR.** `fleet_notification.py:91-97` → `raw = fleet_event_request("fleet_alert" if level == "alert" else "fleet_notice", fleet=selected.fleet.name, subject_kind="fleet", subject=selected.fleet.name, source="fleet-notify", data={"event": event, "message": message, "request_id": request_id, "at": at.isoformat()}, occurred_at=at.isoformat(), legacy_ts=at.isoformat(), event_id=event_id, key=request_id)`; `message_operations.py:790-797` → `fleet_event_request("delivery_enter_repaired", fleet=route.peer_destination.fleet, subject_kind="actor", subject=route.peer.alias, source="message", data={...the same dict...}, occurred_at=at.isoformat(), legacy_ts=at.isoformat(), key=f"delivery_enter_repaired:{message_id}")`. Both `source_ref`s shrink from the full 64-hex digest to `derive_hex`'s 32 — readers match the `fleet-events:` prefix only (`plane-readers.py:1155-1164`), and `event_id` carries the ledger's dedup, so no reader changes and no historical row is re-keyed; say so in the CHANGELOG line.
- [ ] **Step 4 — the writer scans.** `tests/test_event_type_registry.py`: `PY_WRITERS` (`:275-278`) gains `"claudlobby/plane/fleet_events.py"`, `"claudlobby/fleet_notification.py"` and `"claudlobby/message_operations.py"` → `"fleet_event_request"` (the system-family tripwire `:384-399` marks the module, and `set(PY_WRITERS)` is what clears it); `VARIABLE_ROWS` (`:269-273`) gains `("claudlobby/plane/fleet_events.py", "event_type"): "fleet_event_request"` for the module's own `"event": event_type, "subject_kind": …` row; `_py_writer_sites` (`:307-317`) admits the `CONDITIONAL` first argument the row scan already admits (`:298`), so the alert/notice site keeps one call; the positive control (`:323-330`) moves `fleet_alert`, `fleet_notice` and `delivery_enter_repaired` from `rows` to `helpers`.
- [ ] **Step 5:** Verify: `./.venv/bin/pytest tests/test_plane_fleet_events.py tests/test_event_type_registry.py tests/test_fleet_notification.py tests/test_message_operations.py -q`. Commit: `refactor(plane): one Python fleet-event row helper, parity-pinned to emit_fleet_event; fleet-notify and enter-repair adopt it (#2145)`.

### Task 10: Half B docs, CHANGELOG, the rollback disclosure, the row-10 flip

**Files:** `documentation/architecture/observable-plane.md`, `documentation/environment-variables.md`, `documentation/fleet-update-lifecycle.md`, `CHANGELOG.md`; one Claudron PR.

- [ ] `observable-plane.md` Identity section (`:60-69`): one sentence — "The task, report and message doors record the caller's session uid from its runtime's session-id env (`CLAUDE_CODE_SESSION_ID` for `claude`); a door with no such env records none (#2145 §2.2)". `environment-variables.md`: no new var (the doors read Claude Code's own `CLAUDE_CODE_SESSION_ID`); add to the `CLAUDLOBBY_RUNTIME` row from Task 2 that `CLAUDE_CODE_CHILD_SESSION=1` suppresses the uid until C11 is measured (and that Task 7b scrubs a leaked marker at launch if C10 says it leaks).
- [ ] **`documentation/fleet-update-lifecycle.md`** — a new section before `## Reference` (`:576`): `## Receipt format 2 and the rollback floor (#2145 P1)` — decoder-first in two releases (N reads `{0,1,2}` and writes 1; N+1 writes 2); `_save` stamps the format it writes, so the planner's stamp is true for the bytes; **once N+1 writes its first v2 receipt, `host migrate` to any release before N is blocked for as long as that receipt is retained — `state/requests/` has no prune lane, so treat it as permanent**; N stays a legal rollback target, which is why N+1 waits for every host to be on N; `canary-rollout.md:49`'s "retain previous immutable releases for explicit rollback" holds only down to N.
- [ ] `CHANGELOG.md` `[Unreleased]` (release N+1): `### Added — every task, report and message door records the session that ran it, and a retried request after /clear replays (#2145 P1, Half B)` with bullets for the doors (**"restores** the join `lib/report-back.sh` wrote from `data/.plane-session` between `86b4a987` (#1372) and `c4682f7a` (#1989), now from the caller's own env" — L7 answered: yes, say *restored*), receipt format 2 (v1 stays readable; `host migrate` planning reports a v2 receipt as unreadable to any release before N — **permanently, while the receipt is retained**), the readers (`--session`), the C11 fail-safe, Task 9b's helper (the `source_ref` width note), and — if it ran — Task 7b's scrub. Commit: `docs: the join key on the doors, receipt format 2 and its rollback floor (#2145 P1 Half B)`.
- [ ] **Register row 10 flip (X14).** After release N+1 is activated: open the one-line Claudron PR that turns row 10's text cell in `documentation/plans/2026-07-20-claudfather-boundary-separation.md` from **planned** to the shipped cite (`ids.session_alias`/`derive_session_uid`, `claudlobby/plane/ids.py`; text in `observable-plane.md`) — the Claudron P1 plan's Step 6 names this PR as row 10's flipper. Never before the release exists (R6).

### Task 11: gate and the canary-root observation

- [ ] Committed-code mutants, one per task, each shown failing then restored: `KNOWN_RUNTIMES` without `codex` (Task 1 parse test); the validator rung demoted to `warn` (Task 1 validator test); the `CLAUDLOBBY_RUNTIME` line dropped (Task 2); `runtime` always written to the payload (Task 3's "no key for claude" test); `session_alias`'s `claude` branch removed (Task 4's Claude-identity test and the bash parity pin); `_READABLE_FORMATS` back to `{FORMAT_VERSION}` (Task 6's v1 load) and `_save` stamping `receipt.format_version` instead of `FORMAT_VERSION` (Task 6's restamp test); `previous.intent.session_uid` replaced by `ctx.session_uid` in `accept` — **the prepared-unrecorded leg** raises `ReceiptConflict` (Task 8; the committed-replay leg passes either way, by design); the `unset` dropped from `CLAUDE_CMD` (Task 7b's subprocess test, if it ran); `fleet_event_request` building `"data": data` flat instead of nested (Task 9b's parity test); `task_event_from_row` without `session_uid` (Task 9).
- [ ] Two-leg gate: full suite on the branch tip vs `before.txt` by failure name (no new names); `tests/test_instruction_budget.py` green (AGENTS.md copies, budgets); the harness directly (`harness-before.txt` line and names unchanged — no harness scenario drives these doors); CI (`pytest`, `supported-platforms`, `harness` jobs of `.github/workflows/test.yml`).
- [ ] **48 h keepalive / restart-count observation (L9).** Half B changes every task, report and message door on every bot and the receipt format, and no harness scenario drives them (`:307`): on the canary root, record the keepalive restart count and `fleet-pulse`'s verdicts for the 48 h before activation and the 48 h after; the pass criterion is no new restart cause and no new `activity_stuck`/busy-verdict change attributable to a door; write both windows into the run log.
- [ ] **Canary-root observation (mandatory, `CLAUDE.md:262`; the doors change what a bot's session writes to the plane).** In the independent canary root (`documentation/validating-bot-changes.md:35-50`; `library/protocols/canary-rollout.md`): seal the branch's release, `claudlobby --fleet <canary> config plan --release RELEASE_ID`, `config diff PLAN_ID`, `host activate PLAN_ID --install-directory PATH`; confirm `grep -c '^export CLAUDLOBBY_RUNTIME=claude$' <canary bot>/bot.conf` prints 1. From the canary **worker's live session** (its Bash tool, which carries `CLAUDE_CODE_SESSION_ID`): the manager admits and assigns a task; the worker runs `claudlobby --json assignment accept ASG --request-id R1`, then `claudlobby --json task show TASK` — the `accepted` history entry carries a `session_uid` that **equals** the `session_uid` in the worker's `data/.plane-session` (the hook derived it from the payload's `session_id`; equality is the main-thread half of C11, measured for free) — and `python3 -S -E claudlobby/_runtime_scripts/plane-lookup.py --root ROOT --by-assignment ASG --any-state` prints it as the trailing column. `/clear` in the worker, then re-run `assignment accept ASG --request-id R1` → `"replayed": true`, exit 0, the row unchanged, `state/requests/<fleet>/R1.json` rewritten by the retry's `begin_attempt` (`:555`) with `intent.session_uid` still `A` and stamped `"format_version":2` (the file is **not** byte-identical — L6). This live replay is the committed case; the prepared-unrecorded case that proves F17 has no safe live equivalent (it needs a door killed between prepare and commit) and is proven by Task 8's unit leg. Repeat the pair for `fleet reports submit --request-id R2` (`communications.sender_session_uid`). From an Agent subagent in the same session: `assignment progress … --request-id R3` → record whether its row carries the uid (the C11 subagent half — with the fail-safe shipped it records none; write the measured `CLAUDE_CODE_SESSION_ID` vs hook id into the run log). **Rollback direction (L9/X9):** with the v2 receipt retained, run `host migrate`'s planning toward release N and toward the pre-P1 release on the canary root (the `build_migration_manifest` preview; `commands/releases.py`): toward N the plan shows no `receipt_format` blocker; toward pre-P1 it lists `target cannot read retained receipt_format: 2` — record both lines. If C10 showed a leak, also start the canary bot's tmux server from inside a Claude session and confirm `printenv CLAUDE_CODE_CHILD_SESSION` in the bot's pane prints nothing (Task 7b). Record all of it in the epic's run log (`documentation/plans/2026-10-04-runtime-neutral-observability-run-log.md`, the style of `2026-09-28-unified-cli-run-log.md`, identifiers scrubbed) and **cite the observation in the PR body**. Production is untouched; the canary bot is reaped per `validating-bot-changes.md:53-55`.

## Test Plan

Unit: `test_config.py`, `test_known_values.py`, `test_validator.py`, `test_env_register.py`, `test_composer.py`, `test_plane_contracts.py`, `test_plane_registry*.py`, `test_migration_plan.py` (both releases' readability legs and the N+1 → N preview), `test_plane_ids.py` (`session_alias` and the composition pin), `test_plane_session_hook.py` (unchanged, pins parity), `test_request_receipts.py` (the restamp and format-1-shape tests), `test_releases.py`, `test_release_install.py`, `test_operation_context.py`, `test_message_context.py`, `test_task_operations.py` and `test_message_operations.py` (committed-replay **and** prepared-unrecorded legs per door), `test_report_payload.py`, `test_task_state.py`, `test_plane_lookup.py` (`--by-assignment`, `--session`), `test_plane_fleet_events.py` (bash/Python parity, three anchors), `test_event_type_registry.py` (the scans know the helper), `test_boot_policy_conformance.py` (Task 7b's text pin and subprocess check, if C10 leaks), `test_instruction_budget.py`. CLI: `test_task_write_cli.py`, `test_message_write_cli.py` (the generated-caller env plus `CLAUDE_CODE_SESSION_ID`). The harness as the regression leg; the canary-root observation of Task 11 (doors, rollback direction, 48 h keepalive) for behaviour.

## Verification Checklist

- [ ] `claudlobby config validate` on a manifest with `runtime: codex` prints an error containing `execution adapter not shipped`; with `runtime: claude` it does not; `config plan` on the former raises `PlanError`.
- [ ] `claudlobby --json config explain bot.runtime --bot B` answers `built_in` / `set` with no `defaults.runtime`, and `fleet.defaults` with one.
- [ ] `grep -c '^export CLAUDLOBBY_RUNTIME=' <every composed bot.conf>` prints 1.
- [ ] `bot_payload(...)` for a claude bot has no `runtime` key and its `declared_hash` is unchanged from the base commit on the same fixture; `tests/test_plane_registry.py:95-99` `bot_stub` still validates.
- [ ] `session_alias("x") == "x"`, `session_alias("x", "codex") == "codex:x"`; `derive_session_uid("x", r) == derive_uid("sess", session_alias("x", r))` for both runtimes; `derive_session_uid("x") == derive_session_uid("x", runtime="claude")`; `tests/test_plane_session_hook.py::test_bash_derivation_matches_python_byte_for_byte` passes unchanged; `grep -rn '"<runtime>:"\|f"{runtime}:' claudlobby/` hits `plane/ids.py` only.
- [ ] Release N: `runtime_declaration()["receipt_format"] == {"read": [0, 1, 2], "write": 1}`; a v1 receipt written on the base commit loads with `intent.session_uid is None`; after one `begin_attempt` the file is stamped `1` and has no `session_uid` key (bytes a v1 writer could have produced). Release N+1: `{"read": [0, 1, 2], "write": 2}`; the same v1 file's next write is stamped `2` with `"session_uid":null`; a new receipt says `"format_version":2`.
- [ ] After `accept` from session A and a retry of the same `--request-id` from session B: `replayed: true`, `events.session_uid == A`, `intent.session_uid == A` in the rewritten file; the same for `fleet reports submit` and `message send`. The prepared-unrecorded leg per door commits with `A`, no `ReceiptConflict`.
- [ ] `host migrate` planning toward release N with a retained v2 receipt lists no `receipt_format` blocker; toward the pre-P1 release it lists `target cannot read retained receipt_format: 2`; `documentation/fleet-update-lifecycle.md` has the "rollback floor" section and the CHANGELOG says "permanently, while the receipt is retained".
- [ ] `caller_session_uid()` returns `None` with the id absent, with `CLAUDLOBBY_RUNTIME=codex`, and under `CLAUDE_CODE_CHILD_SESSION=1` while `SUBAGENT_SHELL_SHARES_SESSION_ID is not True`.
- [ ] `task show --json` history entries carry `session_uid`; `plane-lookup.py --by-assignment ASG --any-state` prints it last; `plane-lookup.py --session <sess_uid>` lists that session's task events and communications and nothing else's.
- [ ] `tests/test_plane_fleet_events.py` parity passes for the actor, fleet and host anchors; `grep -rn '"fleet-events:sha:" + sha256' claudlobby/` prints nothing (both hand-rolled copies are gone); `tests/test_event_type_registry.py` green with the helper listed in `PY_WRITERS`.
- [ ] If C10 leaked: `grep -c "unset CLAUDE_CODE_CHILD_SESSION; exec \$CLAUDE" claudlobby/_runtime_scripts/start-bot.sh` prints 1 and the subprocess test is green; otherwise the run log says "Task 7b skipped: C10 = no leak".
- [ ] `PROJECT_MISSION.md:5,11,114`, `README.md:3,12`, `CLAUDE.md:3` carry the F18 wording and the `:17`-form ratification paragraph names #2145 F18; `cmp CLAUDE.md AGENTS.md` silent.
- [ ] `cmp claudlobby/_runtime_scripts/CLAUDE.md claudlobby/_runtime_scripts/AGENTS.md` is silent; `tests/test_instruction_budget.py` green.
- [ ] Live, in the canary root: the door-recorded uid equals `data/.plane-session`'s `session_uid`; the post-`/clear` retry replays; the rollback-direction plan lines and the two 48 h keepalive windows are in the run log; the PR body cites it.

## What NOT To Do

- Do not compose, launch, hook-wire or validate a Codex bot; do not add a Codex session-id env name — the companion does, on C1's measured names (F11).
- Do not touch `plane-session-start.sh`'s derivation, `data/.plane-session`, or `transcript-digest.sh` — the hook and the digest retire in P3; the doors never read that file (§2.2).
- Do not bump `registry_emit._SCHEMA`, add `session_uid` to `Transmission` or any migration (F1(b) is rejected; no CHECK change, no `0014`).
- Do not make `_existing` compare the session uid, and do not drop `session_uid` from the hashed projection (F17(c) was rejected at ratification; A-F17 in the epic's §16 proposes it again, which is why Task 6 is held — this plan implements the ratified (a) until the operator rules): the uid is frozen on the intent so the replay proves the same facts.
- Do not flip the receipt writer to 2 in the same release that first reads 2, and do not let `_save` write a stamp that differs from the bytes' shape (Task 6).
- Do not attach the uid to the manager-side doors (`admit`, `assign`, `withdraw`, `reassign`, `escalate`, `nudge`, `recheck`, `deliver`, workstreams) in this PR; they can follow the same three lines once F17's shape is in, as their own change.
- Do not flip `SUBAGENT_SHELL_SHARES_SESSION_ID` without the C11 run-log entry; do not scrub `CLAUDE_CODE_CHILD_SESSION` anywhere but Task 7b, and run Task 7b only if C10 shows a leak; never unset `CLAUDE_CODE_SESSION_ID` there.
- Do not spell the F2 material rule anywhere but `ids.session_alias` — not in `plane-lookup.py`, not in the P2 intake, not in a test's expected value except as `derive_uid("sess", session_alias(...))`.
- Do not hand-roll a fleet-event row in Python again, and do not add a `fleet-events:<name>:…` sub-grammar — `fleet_event_request` (Task 9b) and `fleet-events:sha:<hex>` are the only shape.
- Do not hand-edit `AGENTS.md` or `system.yaml.example`; do not write fleet names, session ids or hosts into any committed file or the run log.

## Context

area: config · compose · plane contracts · request receipts · task/message doors · launcher env · fleet-event rows — effort: **M** (Half A S, Half B M; Tasks 7b and 9b each S) — risk: **Medium** (the doors and the receipt format change runtime behaviour; mitigated by the frozen-uid replay, decoder-first sequencing with the rollback floor disclosed, v1 readability, the fail-safe `None`, the 48 h keepalive observation and the canary-root observation) — priority: P1 of #2145 — related: #1997 (`agent_cli:`, mission line), #974 (mission consolidation branch, coordinate the F18 paragraph), #515 (the mission's amendment form), #1372/#1989 (`report-back.sh` wrote and lost the join), #1724 (older-daemon keyframe rule), #1747/#1989 (receipt format literals), #1456/#1503 (the fleet-event row shape a writer got wrong), #2149 (companion), clauDNA#404 — reforged 2026-10-05 (ironclad cycle 1).

Tasks: 0 worktree (**S**) · **Half A = release N** 1 runtime field + refusal + docs (**S**) · 2 `CLAUDLOBBY_RUNTIME` (**S**) · 3 payload field (**S**) · 4 `session_alias` + F2 derivation (**S**) · 5 design-v2 amendments, F18 mission amendment (D1) (**S**) · 6 Step 2a receipt decoder (**S**) · **SEAM** · **Half B = release N+1** 6 Step 2b writer flip (**S**; held, A-F17) · 7 resolver + contexts (**S**; held, A-F1) · 7b C10 scrub (**S**, conditional) · 8 doors + F17 legs (**M**; held, A-F1/A-F17) · 9 readers + `--session` + instruction rows (**S**) · 9b fleet-event helper + parity + two adopters (**S**) · 10 docs, rollback floor, row-10 flip (**S**) · 11 gate + canary root + 48 h (**M**).

## Canary answers this PR waits on

| Canary | What this PR does before the answer | What changes with the answer |
|---|---|---|
| **C11** — does a subagent's shell (`CLAUDE_CODE_CHILD_SESSION=1`) carry the hook payload's `session_id`? | Ships the fail-safe: `caller_session_uid()` returns `None` under the marker, so a door run from a subagent records no uid, never another session's. Task 11's live step measures the main-thread half (door uid == `.plane-session` uid) and records the subagent half. | Same id → set `SUBAGENT_SHELL_SHARES_SESSION_ID = True` (one constant, both branches already tested) and subagent-run doors join too. Different id → the constant stays, and P3's clauDNA store decides how to represent child sessions. |
| **C10** — does `CLAUDE_CODE_CHILD_SESSION` leak into a bot whose tmux server was started from inside another Claude session? | Until C10 is in the run log, `start-bot.sh` does not scrub env; if it leaks, every door of that bot sees the marker and records no uid — disclosed as `NULL`, not wrong. | A leak → **Task 7b** runs in Half B (`unset CLAUDE_CODE_CHILD_SESSION` before `exec $CLAUDE`, `start-bot.sh:268`, with its two tests); P3 clauDNA Task 5 waits on Task 7b and the 0.28 clauDNA release does not; until it lands the join is dark for bots started that way, and the run log says so. No leak → Task 7b is skipped and recorded as such. |
| **C1** — Codex's session-id env name for child shells | `_SESSION_ID_ENV` has only the `claude` row; `CLAUDLOBBY_RUNTIME=codex` yields `None`. | The companion adds the measured name; no change here. |

## Proposed amendments

None of this plan's own; the ironclad cycle-1 amendments are recorded in the epic's **§16**, and this plan only references them. What changes here if the operator ratifies: **A-F17** (adopt (c)) — Task 6 is dropped in both releases, `expected_fact` excludes `session_uid`/`sender_session_uid` from the hashed projection, `reconcile_facts` hashes each receipt's own `fields` tuple, Task 8 needs no frozen uid and no format bump or rollback floor exists; **A-F1** (land (a) first) — Half B gains, ahead of Tasks 7–8, a task writing `Transmission.session_uid` (legal only with `state == "received"`, the `received_bytes` precedent `contracts.py:296-311`) into `detail` from `plane-dispatch-in.sh`'s `hook["session_id"]`, declared in `wire_additions` and read back through `_DELIVERY_MSG`; Tasks 7–8 then follow after C11, and Task 9's `--session` mode prints the raw id that row carries; **D1** (F18) — Task 5 Step 2 is its proposal and lands as written. Until the operator rules, Tasks 6–8 carry their **Held** lines and are not started.
