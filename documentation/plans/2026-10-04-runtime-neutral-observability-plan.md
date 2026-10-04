---
title: "Runtime-neutral observability and memory for mixed Claude/Codex teams — plan (epic)"
type: plan
status: draft
owner: chrisrogers37
created: 2026-10-04
updated: 2026-10-04
issue: "TBD"
spec: documentation/plans/2026-08-18-observable-plane-design-v2.md
repos: [Claudfather/Claudlobby, Claudfather/clauDNA, Claudfather/Claudron]
---

# Runtime-neutral observability and memory for mixed Claude/Codex teams

> **Status:** draft for operator review. Nothing here is built. Decision forks (§3) are proposals
> until ratified. Line references are to Claudlobby `cd292cb`, clauDNA `e5c413e` (v0.26.0) and
> Claudron `29fdcf3` (v0.9.0). Vendor behavior marked *(doc)* comes from vendor documentation read
> on 2026-10-03 and is verified by the P0 canaries before anything depends on it.

## 1. Summary

Three repos each record part of what an agent does, and part of that duplicates what the agent
runtimes now export natively. The next large body of work — Claude Code and OpenAI Codex bots on
one team — needs every layer to stop assuming Claude Code.

This epic does four things:

1. **Buy the intra-session layer.** Claude Code and Codex both export OpenTelemetry *(doc)*. A local
   OpenTelemetry Collector normalizes both into one schema. Claudlobby stops emitting per-tool-call
   events: `bot-vitals.sh` `tool_call` rows are ~84% of system-event volume (98% together with
   `wip_uncommitted`, `claudlobby/plane/retention.py:84-87`) and nothing reads them back
   (`:77-90`). clauDNA freezes its activity layer.
2. **Keep building what is ours, once each.** Claudlobby keeps the inter-agent record (tasks,
   messages, delivery evidence, liveness). clauDNA owns session segmentation, summaries and
   harvest; Claudlobby's dormant `transcript-digest.sh` retires. Claudron keeps durable reviewed
   knowledge.
3. **One join key:** `(runtime, session_id)`. Today no door writes the plane's session slots
   (`events.session_uid`, `communications.sender_session_uid`), so tasks cannot be joined to the
   sessions that ran them.
4. **Runtime-specific at the edges, runtime-neutral in the middle.** Each repo gets a thin adapter
   per runtime (hook payloads, transcript reader, launcher, telemetry mapping); everything past it
   consumes one neutral model. Codex's documented hook lifecycle mirrors Claude Code's *(doc)*,
   which is what makes this tractable.

Decided out of scope: LangChain-family tooling (LangSmith, Langfuse) and any hosted export —
Claudlobby runs on a local box and telemetry stays there. A spawn-lineage tree is not built.

### 1.1 What this amends

This plan executes the *adopt native OTel* half of design v2 §12 and §18 Phase 3, and amends the
rest. The amendment is recorded in v2 when this plan is ratified (P1):

- v2 §12 pilot (b) and the LangSmith half of §18 Phase 3 (`2026-08-18-observable-plane-design-v2.md:543-545, 589`): **superseded**, LangSmith is out of scope.
- v2 §12's commitment to instrument the tmux boundary with `trace_id`/`span_id` and export it
  through OTel: **deferred**. Interactive sessions ignore inbound `TRACEPARENT` *(doc)* and Codex
  documents no propagation, so the join for a dispatch is the session uid (§2.2), not trace context.
  The envelope's `trace_id`/`span_id` columns stay as they are.
- v2 §9b (`:409-426`) `session_usage` and `utilization_windows`: decided on P2's evidence.
- v2's round-2 session-identity ruling (`:606`): the SessionStart hook `plane-session-start.sh` and
  its `process_uid` minting ("recorded for the future OTel/process join", `plane-session-start.sh:20-23`)
  are **superseded**. The doors derive the uid from the caller's own session id (§2.2), and the OTel
  join is by the normalized session attribute, so the hook retires with the digest in P3.

## 2. Architecture

### 2.1 Layers and owners

| Layer | Owner | Answers | Source |
|---|---|---|---|
| Inside a session | **Bought:** native OTel from each runtime, normalized by a local Collector; the Collector config and the normalized attribute names are Claudlobby's contract | what ran, what it cost, which tools failed | runtime exporters |
| What a session meant | **clauDNA** session store | segments, summaries, harvest candidates | hooks + transcript |
| What's durably true | **Claudron** vault | reviewed knowledge with provenance | harvest drafts, people |
| Who asked whom, did it land, is it alive | **Claudlobby** plane | tasks, messages, delivery, liveness, host health | doors, hooks, timers |

Claudlobby also *composes* the others per bot: telemetry on, `CLAUDNA_*` set, vault pointed, each
owner's hook adapter installed for the bot's runtime.

### 2.2 The join key

`(runtime, session_id)`, where `session_id` is what the runtime gives its hooks and `runtime` is
`claude` or `codex`.

- **Plane.** `session_uid = "sess_" + sha256(<input>)[:32]`. `<input>` is the raw id for `claude`
  (byte-identical to today's `derive_session_uid`, `claudlobby/plane/ids.py:79-89`, so existing
  rows still join) and `"<runtime>:" + id` otherwise (F2). One implementation per language
  (`derive_session_uid` in Python; `plane-session-start.sh`'s Claude-only bash mirror stays
  parity-tested until it retires in P3).
- **Where it is written (F1).** The worker's task doors (`assignment accept|progress|block|return|
  complete|fail`) and `fleet reports submit` / `message send` attach it: `task`-kind events already
  allow `session_uid` (`claudlobby/plane/contracts.py:114,364`, mapped at `claudlobby/plane/ingest.py:256`)
  and `Communication.sender_session_uid` exists (`contracts.py:210`, `ingest.py:162`). No migration.
  The doors run inside the bot's session (`bot.conf` is sourced under `set -a`,
  `start-bot.sh:224-227`), so they derive the uid from **the caller's own session id**
  (`CLAUDE_CODE_SESSION_ID`: clauDNA records that a nested child inherits it,
  `lib/claudna/session_store/boundaries.py:290`, and strips it from its summarizer child,
  `summarize.py:84`; that it equals the hook payload's `session_id` in a tmux-hosted bot, from the
  main thread and from a subagent, is canary C11; the Codex analogue is C1), never from
  `data/.plane-session`, which is bot-global and latest-writer-wins and can name another session
  (`plane-session-start.sh:17-23`). With no session-id env, the door records no uid rather than a
  possibly wrong one.
- **clauDNA store.** Sessions keyed by `session_id` as today; `runtime` recorded at open.
- **Claudron.** Provenance `session:<sid>:<seg>` stays opaque to the engine
  (`claudron/engine.py:160-161`); clauDNA writes a runtime-qualified ref for non-Claude runtimes
  (F9).
- **Telemetry.** The Collector maps Claude's `session.id` and Codex's `conversation.id` *(doc)* onto
  one attribute and adds `agent.runtime` (F5).

### 2.3 Runtime-specific signals are enrichment only

A Claude-only or Codex-only signal may enrich a view; it is never load-bearing for liveness, task
state, rollups, paging or summaries. Enrichment only: Claude subagent span nesting, `TRACEPARENT`
into `-p` children, `CLAUDE_CODE_CHILD_SESSION`, the undocumented `$CLAUDE_PID`, Codex
`PostCompact`.

### 2.4 Telemetry path (local only)

```
claude bot ──OTLP──┐                         ┌─► state/otel/<signal>.jsonl  (raw; content gates off)
codex bot  ──OTLP──┼─► otelcol (127.0.0.1) ───┤
                   │  transform: normalize    └─► plane-otel (OTLP/HTTP, encoding json) ─► emit_batch
                   │  resource: fleet/bot/runtime     session-level metric_samples + a few system events
```

The plane stays the domain record and ingests only an allowlist its readers need. The intake uses
OTLP/HTTP with JSON encoding so it stays stdlib (no protobuf dependency).

## 3. Decision forks

**F1 — Where the session uid lands.**
- Context: `Transmission` forbids `session_uid` (`contracts.py:96-110, 257-330`); changing that
  needs a CHECK change, which is the 12-step full-table copy of
  `claudlobby/plane/migrations/0011_received_transmission.sql:16-40` (write lock, the table's size
  again plus WAL) on a table that is still mostly `tool_call` rows until P2.
- Options: (a) a `detail` field on the received transmission; (b) the column, migration `0014`;
  (c) the worker's task and report doors attach it, derived from the caller's session-id env (no
  contract or schema change).
- Lean: **(c)**. Revisit (b) only if a reader needs the uid on the transmission row itself, and then
  after P2 stops `tool_call` and a prune.
- Ratifier: operator. Status: open.

**F2 — Session-uid derivation for new runtimes.**
- Options: (a) `sha256("<runtime>:" + id)` for non-Claude, Claude unchanged; (b) raw id everywhere.
- Lean: **(a)**: ids from two vendors must not collide, and Claude rows must keep joining.
- Ratifier: operator. Status: open.

**F3 — Where telemetry lands.**
- Options: (a) Collector → files only; (b) files for raw plus the `plane-otel` intake for an
  allowlist; (c) spans in the plane (new family and migration).
- Lean: **(b)**.
- Ratifier: operator. Status: open.

**F4 — Telemetry switch.**
- Context: the Defaults rule makes anything that deletes data opt-in; raw telemetry needs rotation
  and retention, which deletes. Bots configured to export with no Collector running would send to
  an unbound port.
- Options: (a) ships on; (b) one opt-in `Switch` (`telemetry`, carrier: system.yaml enroll for the
  Collector host service) that gates both the Collector and the per-bot `OTEL_*` env.
- Lean: **(b)**, permanently opt-in like `plane-prune`.
- Ratifier: operator. Status: open.

**F5 — Normalized attribute names.**
- Options: (a) OTel GenAI semantic conventions (`gen_ai.conversation.id`, `gen_ai.tool.name`,
  `gen_ai.usage.*`) plus `agent.runtime`; (b) house names.
- Lean: **(a)**.
- Ratifier: operator. Status: open.

**F6 — What replaces `session_digest`, including skipped sessions.**
- Context: consumers are `library/skills/fleet-digest/SKILL.md`, `library/skills/fleet-observe/SKILL.md`
  (`:24-43,132`), `library/protocols/fleet-monitoring.md:98-120` and
  `library/expertise/ai-platform-monitor.md:22`. The digest also emitted `status: skipped` rows the
  monitor uses; clauDNA's export passes over skipped segments.
- Options: (a) keep the `session_digest` type, fed from the export; (b) a new `session_summary`
  system event; plus, either way, clauDNA's export gains status-only items for skipped segments
  behind an opt-in `--include-skipped` flag, so the default `claudna.export/1` item shape (`summary`
  always a segment summary, `export.py:11-12`) is unchanged.
- Lean: **(b)** plus the export change; keep `session_digest` registered so history classifies
  (`claudlobby/plane/registries.py:111-121`).
- Ratifier: operator. Status: open.

**F7 — Who ships the Codex hook adapters.**
- Options: (a) each contract owner ships its own (Claudron `hooks.py` per boundary spec Q1; clauDNA
  a Codex host manifest with its store hooks) and Claudlobby composes them with a parity gate, as it
  does for the Claudron loop (`tests/test_claudron_loop.py`); (b) Claudlobby writes every Codex hook.
- Lean: **(a)**.
- Ratifier: operator. Status: open.

**F8 — clauDNA activity layer.**
- Options: (a) freeze: no new kinds, hooks stay wired; (b) unwire the three activity hooks, keep the
  registry and readers; (c) remove.
- Lean: **(a)** now; (b) once interactive users have OTel too.
- Ratifier: operator. Status: open.

**F9 — Harvest provenance for non-Claude sessions.**
- Options: (a) `session:<runtime>/<sid>:<seg>` for non-Claude, unchanged for Claude; (b) always
  qualified.
- Lean: **(a)**, mirroring F2.
- Ratifier: operator. Status: open.

**F10 — Summarizer for Codex sessions.**
- Options: (a) keep `claude -p` (Haiku) for every runtime; (b) a per-runtime runner (`codex exec`).
- Lean: **(a)**. Consequence: a host running Codex bots with summaries on also needs a Claude binary
  and account. A Codex-only host means (b), or summaries off.
- Ratifier: operator. Status: open.

**F11 — Launching Codex bots.**
- Options: (a) in this epic; (b) a companion epic (§5).
- Lean: **(b)**. This epic lands the vocabulary and the owner-side adapters; every step that
  composes or validates a Codex bot lives in the companion, because Claudlobby's mandatory runtime
  validation needs a bot that can run.
- Ratifier: operator. Status: open.

**F12 — clauDNA skills on Codex.** Out of scope (behavior parity is its own epic). The Codex host
manifest ships store hooks only. Ratifier: operator. Status: open.

**F13 — clauDNA's owner-pid field for Codex.**
- Context: `session.opened.claude_pid` (`lib/claudna/session_store/events.py:96`).
- Options: (a) reuse `claude_pid`, documented as "the owning agent process"; (b) a new optional
  `owner_pid`.
- Lean: **(a)** until the next envelope major (no schema change; older readers unaffected); rename
  then.
- Ratifier: operator. Status: open.

**F14 — Per-bot clauDNA state directory cutover.**
- Context: bots share `~/.claudna` today; P3 moves each to `$BOT_DIR/data/claudna`, which strands
  open sessions and export cursors in the old root.
- Options: (a) seal every open session in the old root at the switch (`session_store seal`), keep it
  read-only until its retention window passes, then remove; (b) migrate sessions into per-bot roots.
- Lean: **(a)**.
- Ratifier: operator. Status: open.

**F15 — Waive the pre-registered summarizer comparison.**
- Context: clauDNA spec §1.1 rule 4 requires a pre-registered siloed comparison of
  `transcript-digest.sh` and the store's summarizer before one owner is chosen (Claudlobby#1961).
  The operator has decided to retire the digest, which is dormant by default.
- Options: (a) waive the comparison and record the decision in the spec; (b) run the comparison as
  a P3 entry gate.
- Lean: **(a)**, keeping rule 4's coverage concern (skipped sessions) through F6.
- Ratifier: operator. Status: open.

**F16 — How Claudlobby finds clauDNA's export entrypoint.**
- Context: the door is `python3 <plugin root>/lib/claudna/session_store export …`; there is no
  `claudna` executable, and the plugin cache is per account (`CLAUDE_CONFIG_DIR`,
  `claudlobby/composer.py:1046`) and per version. `plugin_ensure` only greps the registry for the
  plugin name (`lib-common.sh:4699`); nothing resolves an install path today.
- Options: (a) parse Claude Code's `installed_plugins.json` (`installPath`) under the bot's config
  dir, a dependency on an undocumented format; (b) clauDNA's own SessionStart hook records its
  entrypoint (derived from `${CLAUDE_PLUGIN_ROOT}`) in the bot's state dir as a documented contract
  (`<CLAUDNA_STATE_DIR>/entrypoint.json`), and Claudlobby reads that.
- Lean: **(b)**: consume by contract; it works for any host whose hooks know their plugin root (C8).
  The export job checks that the recorded path exists; a missing one (a plugin update applied by
  `/reload` without a restart, `reload-fleet.sh`) is reported as a stale entrypoint and the bot is
  skipped until its next SessionStart rewrites the file.
- Ratifier: operator. Status: open.

**F17 — Session uid on a retried request.**
- Context: `session_uid` is part of a request's hashed facts (`claudlobby/request_facts.py:42-51`),
  so a retried `--request-id` after a `/clear` or restart (new session id) raises `ReceiptConflict`
  (`claudlobby/request_receipts.py:541-543`). A receipt stores no field values to reuse:
  `ExpectedFact` holds the event id, family, projection hash and field names (`:61-65`), and
  `RequestIntent` has no slot (`:129-142`).
- Options: (a) add `session_uid` to `RequestIntent` so a replay reuses it; this changes the receipt
  format (bump `RECEIPT_FORMAT_VERSION`, `claudlobby/runtime_versions.py:21-22`, with its release
  gate); (b) on an uncommitted replay, rebuild the facts with the old uid found by hash-matching;
  (c) leave `session_uid` out of the hashed projection.
- Lean: **(a)**: the replay then proves the same facts, and the format bump is the existing,
  gated path for exactly this kind of change.
- Ratifier: operator. Status: open.

## 4. Evidence

Claudlobby (`cd292cb`):
- No runtime abstraction. `BotConfig` has no runtime field (`claudlobby/config.py:669-803`), and top-level bot keys have no unknown-key check, so a `runtime:` key is silently ignored. Launch is `exec $CLAUDE ${CLAUDE_FLAGS} --name …` in tmux (`claudlobby/_runtime_scripts/start-bot.sh:265-325`). Design v2 names the seam unbuilt (`:541`).
- Plane session slots are unwritten:
  - `plane-dispatch-in.sh` reads only `prompt` (`:93`), though the payload carries `session_id`.
  - `plane-session-start.sh` writes `data/.plane-session` (`:59-84`).
  - That file's only reader is `transcript-digest.sh` (`:254-308`), which P3 retires.
- `bot-vitals.sh` emits `tool_call` (`:62-64`), and nothing reads those rows (`claudlobby/plane/retention.py:77-90`). The `data/.last-tool-call` marker it touches (`:94`) **is** load-bearing:
  - keepalive busy detection (`keepalive.sh:123,378,435`);
  - the shared busy gate (`claudlobby/_runtime_scripts/lib-common.sh:3562-3603`);
  - fleet-pulse idle and `activity_stuck` (`fleet-pulse.sh:519-610`).
- No OTLP home today:
  - the daemon is ingest-only over a Unix socket (`claudlobby/plane/daemon.py`);
  - the view is GET-only (`claudlobby/plane/view.py:7`), on `127.0.0.1:8899` (`claudlobby/commands/_parsers.py:121,124`).
- `transcript-digest.sh` is opt-in (`claudlobby/switches.py:529-540`). Its hook is at `claudlobby/system.yaml:402-417` and `system.yaml.example:444`.
- clauDNA wiring: only `CLAUDNA_VERSION` is exported (`claudlobby/composer.py:1319-1330`). The Claudron install pin is `v0.6.1` (`pyproject.toml:32`, `.github/workflows/conformance.yml:37`, `documentation/integrations/claudron-integration.md:7,44,48`).
- `RC_KILLING_ENV_VARS` includes `DISABLE_TELEMETRY` and the nonessential-traffic umbrella (`claudlobby/known_values.py:115-118`). `CLAUDE_CODE_ENABLE_TELEMETRY` is a different switch.
- `SQL_SCHEMA_VERSION` support is exactly `{13}` (`claudlobby/runtime_versions.py:13-14`). This is a cost on every migration.

clauDNA (v0.26.0):
- `boundaries.py` is "the one Claude Code-specific module" (docstring). It holds:
  - payload parsing;
  - the nested-child guard (`:287-314`);
  - the entrypoint allowlist (`:49`);
  - the owner pid (`:111-117`).
- `transcript.py` parses Claude JSONL in `turn_of` (`:50`) and `read_range` (`:66-96`). `summarize.py` runs `claude -p` through an injectable runner (`:73-111`).
- `session.opened.actor` is `additionalProperties: false`, and older readers apply it. A top-level optional key folds in 0.23–0.26 readers (`events.py:18-21`); a key nested in `actor` does not.
- `read_projection` re-folds an unknown projection tag (`project.py:429-441`), but `OLDER_PROJECTIONS` (`:35`) and `stale_projections` (`:417-426`) cover only `segment.json`.
- The layering gate globs flat `session_store/*.py` (`tests/test_runtime_layout.py:150-155`).
- `scripts/release.sh:20-21,150-155` bumps exactly two manifests.
- Spec rules that this plan changes (`documentation/specs/2026-09-28-session-store-design.md` §1.1):
  - rule 1 assumes the plane records every tool call;
  - rule 4 requires a pre-registered comparison before retiring either summarizer.

Claudron (v0.9.0):
- `hooks.py` is the Claude Code adapter of the session-loop contract. `SNIPPET_EVENTS` is at `:235-239` and is duplicated in `merge_settings` (`:388-392`).
- The adapter reads only `session_id`, and relies on SessionStart stdout injection and PreCompact `decision: block`.
- The ops log keys directories on a session-id regex (`ops.py:32-33`).
- The single-dependency rule (PyYAML) applies.
- The boundary spec is a draft plan with no formal amendment process. Changes land as a PR to Claudron (`documentation/plans/2026-07-20-claudfather-boundary-separation.md`).

Runtimes *(doc)*:
- **Claude Code:**
  - OTel metrics and logs via `CLAUDE_CODE_ENABLE_TELEMETRY`, with traces in beta.
  - Attributes include `session.id` and `prompt.id`.
  - `TRACEPARENT` is passed to Bash children and read by `-p`; interactive sessions ignore it.
  - No parent session id in hooks. `CLAUDE_CODE_CHILD_SESSION=1` marks children.
- **Codex:**
  - Hooks are on by default, in `~/.codex/hooks.json`, `config.toml`, repo `.codex/`, or a plugin manifest.
  - Hook events: `SessionStart` (`startup|resume|clear|compact`), `SessionEnd`, `PreCompact`/`PostCompact`, `UserPromptSubmit`, `Pre/PostToolUse`, `SubagentStart/Stop`, `Stop`. Payloads carry `session_id`, `transcript_path`, `cwd`.
  - OTel is configured under `[otel]` (logs and metrics) and keyed by `conversation.id`.
  - Traces and propagation are undocumented.

## 5. Companion plans and blocks

- **Companion — Codex execution adapter** (F11): launching, supervising and validating Codex bots,
  and the Claudlobby-side Codex composition (hooks file, `[otel]`, plane hooks). Blocked by P1 and
  P4 here. Ends with a mixed canary fleet.
- **Companion — clauDNA on Codex** (F12): skills and agents for Codex hosts.
- **Blocks:** any mixed-runtime team feature; the v2 §9b usage decisions.

## 6. Implementation plan

Order: P0 → P1 → (P2 ∥ P3 ∥ P4). Releases gate cross-repo steps and are named in each phase.
Every PR that changes runtime behavior cites canary-root observation
(`harness/validate-bot-change.sh`) per Claudlobby's mandatory runtime validation.

### P0 — Canaries and fork ratification

Record results in a run log next to this plan, in the style of
`2026-09-28-unified-cli-run-log.md`.

- [ ] **C1** Codex hook payloads: the real field names per event and the `session_id` format.
  - Is `transcript_path` set?
  - Does `PreCompact` fire for automatic compaction, and is it followed by `SessionStart(compact)`?
  - Is there a session-id env var for tools and CLIs (the analogue of `CLAUDE_CODE_SESSION_ID`)?
- [ ] **C2** Codex owner process: is there an env var like `$CLAUDE_PID`, and what does an ancestor walk find?
- [ ] **C3** Codex context injection: is `SessionStart` stdout injected? Can `PreCompact` block or add an instruction?
- [ ] **C4** Codex rollout files: are they append-only and byte-addressable? Which record types carry user and assistant text?
- [ ] **C5** Codex OTel:
  - the actual event names and attributes;
  - whether `conversation.id` equals the hook `session_id`;
  - whether any traces exist;
  - whether it supports OTLP/HTTP JSON and resource attributes.
- [ ] **C6** Claude OTel on a bot:
  - does `session.id` equal the hook `session_id`?
  - event volume per bot-hour;
  - Collector RSS and CPU on the smallest host class;
  - does it cover every `tool_call` the plane records today?
- [ ] **C7** Codex in tmux: prompt glyph, slash-command injection, restart behavior. Feeds the companion.
- [ ] **C8** Codex plugin-manifest hooks: how does a hook command resolve its plugin root (clauDNA uses `${CLAUDE_PLUGIN_ROOT}`)?
- [ ] **C9** Host identification: confirm the adapter can be selected by the hook command itself (`… hook <event> --host codex`) rather than sniffed.
- [ ] **C10** `CLAUDE_CODE_CHILD_SESSION` leak: when a bot's tmux server is started from inside another Claude session, does the variable reach the bot? `start-bot.sh` does not scrub env. If it does, P3's guard change needs a scrub first.
- [ ] **C11** Claude session id in tools: in a tmux-hosted bot, does the Bash tool's `CLAUDE_CODE_SESSION_ID` equal the SessionStart payload's `session_id`, from the main thread and from an Agent subagent (whose shells carry `CLAUDE_CODE_CHILD_SESSION=1`)? If a subagent's id differs, F1(c) doors running under `CLAUDE_CODE_CHILD_SESSION=1` record no uid.
- [ ] Ratify F1–F17.

### P1 — Vocabulary, join key and the contracts behind them

Claudron:
- [ ] PR amending the boundary spec (`documentation/plans/2026-07-20-claudfather-boundary-separation.md`):
  - §10.2: the bought layer, owned by Claudlobby (Collector config plus normalized attribute names);
  - §10.4: register rows for the join key, the export contract's new fields, and the Codex session-loop snippet (landing in P4);
  - the §2.3 rule from this plan.

  This lands **before** P2 ships the attribute contract (R1/R2).
- [ ] Unify `merge_settings`'s `event_cmds` with `SNIPPET_EVENTS`.
- [ ] Confirm that the ops-log id regex admits Codex ids (C1). If it doesn't, widen it together with its doc-parity update.

Claudlobby:
- [ ] `BotConfig.runtime: Literal["claude","codex"] = "claude"`, parsed with `_select_bot_scalar`/`_parse_enum`.
  - The validator rejects unknown values.
  - A `codex` bot fails validation with "execution adapter not shipped" until the companion lands.
  - Docs: `documentation/fleet-yaml-schema.md`, `fleet.yaml.example`, `config_explain`.
- [ ] Composed `CLAUDLOBBY_RUNTIME` in `bot.conf`, so the doors know the runtime for `derive_session_uid(…, runtime)`. Add a row in `documentation/environment-variables.md` and a composer test.
- [ ] `BotPayload.runtime` in the registry keyframe (`contracts.py:670-688`, `extra="forbid"`), with a payload schema bump and `registry_emit.bot_payload`.
- [ ] `derive_session_uid(platform_session_id, runtime="claude")` per F2, in Python (the doors' only derivation). The bash mirror in `plane-session-start.sh` stays Claude-only until it retires in P3 (no Codex bot can run before the companion). Extend `tests/test_plane_ids.py` with a Codex case.
- [ ] Task and report doors attach `session_uid` per F1(c), derived from the caller's session-id env (§2.2):
  - `task_operations.accept` and `_assignment_report` (`claudlobby/task_operations.py:445, 473+`; raws built by `_raw()` at `:312-317`) set `events.session_uid`;
  - `encode_report_facts` (`fleet reports submit`) and the send path in `message_operations` set `sender_session_uid`.
  - **Receipts:** per F17, a retried `--request-id` after a `/clear` or restart must not raise `ReceiptConflict`. Under F17(a), `RequestIntent` carries `session_uid` and a replay reuses it (receipt format bump). Test exactly that replay.

  `task show` and `plane lookup` print the session. Tests cover each door.
- [ ] Amend design v2 per §1.1.

clauDNA:
- [ ] `session.opened.data.runtime`: optional, top-level, `choices=("claude","codex")`; when absent it reads as `claude`.
- [ ] `session.json` gains `runtime`: tag `claudna.session/2`, `claudna.session/1` added to `OLDER_PROJECTIONS`, and the stale-projection sweep taught about `session.json`. Mixed 0.26/0.27 readers then converge instead of rewriting each other's file.
- [ ] `claudna.export/1` items gain `runtime` (additive). Document it in spec §8.
- [ ] Harvest provenance per F9. Add a case to `tests/test_claudron_live.py`.
- [ ] Amend spec §1.1: rule 1 (the plane no longer records every tool call after P2) and, if F15(a) is ratified, rule 4 (the comparison is waived; the store's summarizer is the single owner; the coverage concern is kept by F6).
- [ ] Release clauDNA (the export and session fields are needed by P3).

### P2 — Local OpenTelemetry pipeline (Claudlobby; gated by F4)

- [ ] A `telemetry` `Switch` row (opt-in). New host service `otel-collector` (`host.jobs`, `unit: service`) running upstream `otelcol-contrib` on `127.0.0.1`, installed by host setup. That means a cold-host onboarding run per the onboarding rule.
  - The composer renders its config from `system.yaml`:
    - an OTLP receiver;
    - a `transform` processor (F5);
    - a `resource` processor;
    - a `file` exporter to `state/otel/` with rotation and a stated retention;
    - an OTLP/HTTP exporter with `encoding: json` to `plane-otel`.
  - Validate the rendered config with `otelcol validate` in tests.
- [ ] Composer emits per-bot telemetry env for **Claude** bots, only when the switch is on:
  - `CLAUDE_CODE_ENABLE_TELEMETRY=1`;
  - `OTEL_LOGS_EXPORTER=otlp` and `OTEL_METRICS_EXPORTER=otlp`;
  - the protocol and an endpoint on `127.0.0.1`;
  - `OTEL_RESOURCE_ATTRIBUTES` with fleet uid, `bot:<fleet>/<bot>` and `agent.runtime=claude`.

  Content gates stay off (v2 §11). The carrier is `bot.conf`, applied at next restart.
- [ ] `plane-otel` intake: its own localhost host service, not the daemon or the view, which are pinned to their scopes. It maps an allowlist into `emit_batch`:
  - per-session cost, tokens and tool-failure counts as `metric_samples` on `subject_kind='session'` (new `METRIC_NAMES`);
  - `api_error` and repeated tool failure as system events (`SYSTEM_EVENT_SEVERITY`).

  Also: a doors-table row in `documentation/architecture/observable-plane.md`, and a launcher in `_runtime_scripts/` with its index line in both `CLAUDE.md`/`AGENTS.md` pairs, within the 32 KiB Codex budget (`tests/test_instruction_budget.py`).
- [ ] Canary: one Claude bot, one host, one week. It passes when C6 holds and the Collector stays inside its stated resource budget.
- [ ] Then stop `bot-vitals.sh` emitting `tool_call`, and remove the `session_event` path (no payload carries that key).
  - It keeps touching `.last-tool-call`.
  - The registry entries stay, and `tool_call` stays in `PRUNABLE_SYSTEM_EVENTS` so existing rows can still be pruned.
  - Update `tests/test_event_type_registry.py:100-101`, `tests/test_plane_cutover_keepalive.py:94-110`, and `tests/test_plane_emit_class.py:170-185`. (`tests/test_system_event_retention.py:100` pins `{"tool_call","wip_uncommitted"}` and stays unchanged.)
  - Update `library/protocols/fleet-observability.md:66`, `documentation/guides/observability.md:83,88`, and the `bot-vitals` index rows (root and `_runtime_scripts` `CLAUDE.md`/`AGENTS.md`).
- [ ] Decide v2 §9b's usage items on the evidence; `claudlobby/transcript_usage.py` retires if OTel answers its questions.

### P3 — Summaries owned by clauDNA; the export consumer

Claudlobby:
- [ ] Per-bot `CLAUDNA_STATE_DIR=$BOT_DIR/data/claudna`, with the cutover per F14.
- [ ] `CLAUDNA_SESSION_SUMMARY` and `CLAUDNA_HARVEST` as fleet-level opt-ins, since they spend model calls. Add `Switch` rows and regenerate the switch tables (`switches.format_markdown` into `fleet-yaml-schema.md`, `system-yaml-schema.md`, `observable-plane.md`; pinned by `tests/test_switches.py`). Add rows to `documentation/environment-variables.md:183`.
- [ ] Bump the Claudron pin `v0.6.1` → `v0.9.0`. Harvest needs ≥0.7.1, and homes ≥0.8. The jump crosses vault migrations (vault format 3 / m003 in 0.7.0, index schema 7 in 0.8.0; Claudron's CHANGELOG says to run `claudron doctor --fix`), so the bump ships with an operator-run step (it writes a git commit to a shared vault — Defaults rule): `claudron doctor --fix --json` once per vault, from one clone, not from every host. Composition then refuses a session loop only for a vault with **pending migrations or an old vault format**, never for unrelated doctor findings (D009/D010 hook warnings, structure warnings). Canary it on one host first. Touches:
  - `pyproject.toml`;
  - `conformance.yml`;
  - `claudlobby/claudron_compat.py` and `tests/test_claudron_compat.py`;
  - `tests/test_claudron_loop.py::TestSnippetParity` (`:453`);
  - `documentation/integrations/claudron-integration.md:7,44,48`.
- [ ] `session-export` fleet job. It invokes the export door at its contracted path (F16):
  - `python3 <entrypoint> export --consumer claudlobby --include-skipped --json`;
  - `<entrypoint>` is read from the file clauDNA's own hook writes into the bot's state dir;
  - the entrypoint file and the invocation are both clauDNA spec §8 contract.

  For each exported segment, including F6's status-only skipped items, it emits the F6 event and then `--ack`s.
- [ ] Retire `transcript-digest.sh`, removing it from:
  - `claudlobby/system.yaml:402-417` and `system.yaml.example:444`;
  - its `Switch` row;
  - `harness/validate-bot-change.sh:3576-3580`;
  - `claudlobby/isolation.py:100`, if only the digest used it;
  - tests: `tests/test_transcript_digest.sh`, `tests/test_transcript_digest_isolation.py`, `tests/test_fleet_claude_bin.py:188`, `tests/test_switches.py`, plus the token set in `tests/test_no_retired_digest_reference.py`;
  - docs: `documentation/environment-variables.md`, `fleet-update-lifecycle.md`, `testing-plane-isolation.md`, `system-yaml-schema.md`, `fleet-yaml-schema.md`, `architecture/module-map.md`, `architecture/observable-plane.md`;
  - the index rows in both `CLAUDE.md`/`AGENTS.md` pairs.

  Repoint the four consumers in F6 to the new event. With the digest gone, `.plane-session` has no reader, so `plane-session-start.sh` and its SessionStart hook retire in the same PR (its bash derivation goes with it; `derive_session_uid` in Python is then the only implementation; §1.1 records the v2 ruling this reverses). Touchpoints: `claudlobby/system.yaml:424` and `system.yaml.example:452` (the hook entry), `tests/test_plane_session_hook.py`, `tests/test_plane_gauntlet_doors.py:27`, `documentation/system-yaml-schema.md:389`, `documentation/architecture/observable-plane.md:69,212`, and the index rows in both `CLAUDE.md`/`AGENTS.md` pairs.

clauDNA:
- [ ] Export gains `--include-skipped`: status-only items for skipped segments (F6). Default output unchanged; spec §8.
- [ ] SessionStart hook writes `<CLAUDNA_STATE_DIR>/entrypoint.json` (F16): the absolute path of the store entrypoint for the running plugin version. Spec §8; tested.
- [ ] Freeze the activity layer per F8, with a spec note.
- [ ] Child guard reads `CLAUDE_CODE_CHILD_SESSION=1` first, keeping the pid and entrypoint checks as fallbacks. This step waits on C10. Update the `SETUP_GUIDE.md` env table (`:309-317`) if any documented semantics change.
- [ ] Release clauDNA.

### P4 — Owner-side runtime adapters (fixture-tested; composed in the companion)

clauDNA:
- [ ] Split `boundaries.py` into the neutral handler plus flat host modules `host_claude.py` and `host_codex.py`. They cover:
  - payload normalization;
  - owner pid (C2, F13);
  - actor kind from the entrypoint;
  - the child guard;
  - the clear-link source.

  The host is selected by the hook command (C9). Add the new modules to `SESSION_STORE_LAYERS` and to `lib/CLAUDE.md` rule 2. The Python 3.9 floor holds.
- [ ] Codex transcript reader (C4) behind the `turn_of`/`read_range` interface, with a redacted golden rollout excerpt and recorded hook payloads (C1) as fixtures.
- [ ] Codex host manifest carrying the store hooks only (F7, F12). **Requires operator approval** under clauDNA's "Marketplace plugin metadata changes" gate. This step also covers:
  - `validate-manifest.py` with n-way version sync;
  - `scripts/release.sh` bumping three manifests;
  - CLAUDE.md "Working on This Repo" step 5 and its repo structure;
  - the plugin root from C8.
- [ ] Release clauDNA (the manifest is the surface the companion composes).

Claudron:
- [ ] A Codex adapter in `hooks.py`:
  - event map, snippet renderer for Codex `hooks.json`, and merge;
  - `hooks install --host codex`;
  - doctor D009/D010 per host;
  - a normative Codex snippet in `docs/CLI_CONTRACT.md` with a parity test;
  - a capability (e.g. `codex-session-loop`) with its gate sentence.

  If C3 shows Codex cannot inject or block, R-capture-prompt is documented as not held on Codex.
- [ ] Release Claudron. The companion bumps Claudlobby's pin again to compose the Codex snippet (R6).

Claudlobby (fixture-tested only; composition and canary in the companion):
- [ ] Payload adapters for the hook scripts so each reads normalized fields:
  - `plane-dispatch-in`;
  - the `bot-vitals` marker.

  Claude-specific parsers are listed for the companion: `plane-rc-relay-out.sh:62-100` (Claude transcript), and `plane-telegram-in/out` (Claude channel tags and the `mcp__plugin_telegram_telegram__reply` matcher).

## 7. Test plan

- Each repo's own check-set: clauDNA `make check`; Claudron `pytest`; Claudlobby against its documented baseline (`documentation/test-suite.md`), comparing failing names and counts.
- Parity gates:
  - `derive_session_uid`: Claude-only bash parity until the bash mirror retires in P3; the Codex case in `tests/test_plane_ids.py`;
  - Claudron's Codex snippet against Claudlobby's composer (companion);
  - the Collector config passes `otelcol validate`.
- Contract tests: clauDNA's live suite covers runtime-qualified provenance and export `runtime`/skipped items, and Claudron's consumers job runs it.
- Fixtures: a recorded Codex hook payload per event (C1) and a Codex rollout excerpt (C4), both redacted.
- Canary-root observation for every runtime-behavior PR in P1–P3. A cold-host onboarding run when the Collector install lands. The P2 canary criteria are recorded in the run log.

## 8. Verification checklist

- [ ] `task show` names the session that worked on a task, for a Claude bot. A Codex bot follows in the companion.
- [ ] One query over `state/otel/` answers "what did session X cost and which tools failed".
- [ ] `tool_call` rows stop arriving, while keepalive busy detection and fleet-pulse idle checks are unchanged.
- [ ] No composed bot carries a `transcript-digest.sh` hook. The F6 event, including skipped sessions, feeds the four consumers.
- [ ] A Codex session fixture segments, summarizes and harvests with a runtime-qualified ref.
- [ ] No fleet feature reads a signal listed in §2.3.

## 9. Risks

- **Collector on the smallest host:** memory and CPU under a full fleet. Covered by the P2 canary budget and by F4 keeping it opt-in.
- **Vendor drift:** Claude's trace schema is in beta and Codex telemetry is young. Only logs and metrics are load-bearing here, and the Collector config absorbs renames in one place.
- **Codex hook details differ** (C1–C4). Each adapter documents what it cannot hold rather than approximating it.
- **Older readers sharing state:** the clauDNA field placement and projection steps in P1 exist for this reason.
- **Codex hosts need Claude for summaries** (F10).
- **Volume moves rather than disappears:** `state/otel/` needs its own retention.

## 10. Complexity and sequencing

| Phase | Size | Repos | Release gates |
|---|---|---|---|
| P0 | S | all | — |
| P1 | M | Claudron (spec, prep), Claudlobby, clauDNA | clauDNA release |
| P2 | L | Claudlobby | — |
| P3 | M | Claudlobby, clauDNA | clauDNA release; Claudron pin bump |
| P4 | L | clauDNA, Claudron, Claudlobby (adapters) | clauDNA and Claudron releases |
| Companion | L | Claudlobby | needs P1, P4 releases |

Suggested PR order:
1. The Claudron boundary-spec PR.
2. The Claudlobby runtime field and derivation.
3. The clauDNA runtime-at-open change, then its release.
4. The Claudlobby session uid on its doors.
5. P2 and P3 in parallel.
6. P4 per repo.

## 11. What NOT to do

- Don't add hosted export or LangChain-family dependencies.
- Don't put an OTel SDK in Claudron (single dependency) or clauDNA (stdlib-only `lib/`). Don't add protobuf to Claudlobby's intake; use JSON encoding.
- Don't build a spawn-lineage tree, and don't depend on any §2.3 signal for fleet behavior.
- Don't stop touching `.last-tool-call`, and don't remove `tool_call` from the prunable set.
- Don't nest `runtime` inside clauDNA's `actor`.
- Don't take a worker's session uid from the bot-global `.plane-session`; derive it from the caller's own session id with the one Python implementation.
- Don't compose or validate a Codex bot before the companion epic.
- Don't store raw prompts or tool bodies in telemetry by default.

## 12. Context

This plan comes from a cross-repo review on 2026-10-03/04 covering:
- Claudron's `contract --json` and consumer CI (v0.9.0);
- clauDNA's vendored contract and floor legs (v0.26.0);
- buy-vs-build research on Claude Code and Codex telemetry;
- the Claudlobby plane map.

An adversarial review of the first draft corrected its evidence and sequencing.

## 13. Disposition

Draft. Next:
1. The operator ratifies F1–F17.
2. P0 runs.
3. An epic issue is opened, with one sub-issue per phase per repo.
