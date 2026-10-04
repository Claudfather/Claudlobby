---
title: "Runtime-neutral observability and memory for mixed Claude/Codex teams — plan (epic)"
type: plan
status: draft
owner: chrisrogers37
created: 2026-10-04
updated: 2026-10-04
issue: "#2145"
spec: documentation/plans/2026-08-18-observable-plane-design-v2.md
repos: [Claudfather/Claudlobby, Claudfather/clauDNA, Claudfather/Claudron]
---

# Runtime-neutral observability and memory for mixed Claude/Codex teams

> **Status:** draft. Nothing here is built. **All decision forks (§3) were ratified by the operator
> on 2026-10-04** (F4 as amended there); the plan is next fleshed out with `/claudna:forge` and
> `/claudna:ironclad`. Line references are to Claudlobby `cd292cb`, clauDNA `e5c413e` (v0.26.0) and
> Claudron `29fdcf3` (v0.9.0). Vendor behavior marked *(doc)* comes from vendor documentation read
> on 2026-10-03 and is verified by the P0 canaries before anything depends on it.
>
> **Anchors (forge, 2026-10-04):** clauDNA `e5c413e` is the pre-squash twin of the v0.26.0 tag (`71f983d`,
> identical tree). Claudron `29fdcf3` is the pre-squash twin of #223 (main `8e895cb`), one CI-only commit
> past the v0.9.0 tag (`6ca2b94`); every Claudron code line cited here is identical at the tag. Claudlobby
> `origin/main` has since moved to `4da926d3` (#2143, #2127), touching nothing cited here. Forge added the
> `#### Spec:` subsections in §6, the per-PR plan index in §10, and §14–§15; it changed no fork decision.

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
into `-p` children, `CLAUDE_CODE_CHILD_SESSION`, `$CLAUDE_PID` (documented for hook and tool subprocesses since Claude Code v2.1.214 *(doc)*), Codex
`PostCompact`.

### 2.4 Telemetry path (local only)

```
claude bot ──OTLP──┐                         ┌─► state/otel/<signal>.jsonl  (raw; content gates off)
codex bot  ──OTLP──┼─► otelcol (127.0.0.1) ───┤
                   │  transform: normalize    └─► plane-otel (OTLP/HTTP, encoding json) ─► daemon socket ─► emit_batch
                   │  resource: fleet/bot/runtime     session-level metric_samples + a few system events
```

The plane stays the domain record and ingests only an allowlist its readers need. The intake uses
OTLP/HTTP with JSON encoding so it stays stdlib (no protobuf dependency). The Collector layout, the
attribute mapping and the exact allowlist are the two `#### Spec:` subsections under §6 P2.

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
- Ratifier: operator. Status: locked (lean), 2026-10-04.

**F2 — Session-uid derivation for new runtimes.**
- Options: (a) `sha256("<runtime>:" + id)` for non-Claude, Claude unchanged; (b) raw id everywhere.
- Lean: **(a)**: ids from two vendors must not collide, and Claude rows must keep joining.
- Ratifier: operator. Status: locked (lean), 2026-10-04.

*Forge note (2026-10-04), F2:* the one Python derivation is also what the plane's identity resolver must
use for `session` subjects — today `identity.resolve` mints a random uid on first sight
(`claudlobby/plane/identity.py:36`), so a session-anchored metric sample would never join. The
derive-not-mint rule is in the P2 Collector-and-intake spec; it changes no part of this fork.

**F3 — Where telemetry lands.**
- Options: (a) Collector → files only; (b) files for raw plus the `plane-otel` intake for an
  allowlist; (c) spans in the plane (new family and migration).
- Lean: **(b)**.
- Ratifier: operator. Status: locked (lean), 2026-10-04.

**F4 — Telemetry configuration and default.**
- Context: the Defaults rule makes anything that deletes data opt-in; raw telemetry needs rotation
  and retention, which deletes. One Collector serves every fleet on a host; telemetry *on* is a
  per-bot choice. Bots configured to export with no Collector running would send to an unbound port.
- Options: (a) ships on; (b) one host-wide switch for both; (c) split by scope: the Collector is a
  host service enrolled in `system.yaml`, the per-bot setting is composed from `fleet.yaml`.
- Decision: **(c)**, opt-in on both sides, permanently (like `plane-prune`):
  - Collector: `Switch` `otel-collector`, scope `HOST_SERVICE`, carrier `ENROLL_HOST`
    (`system.yaml` enroll); the host owns retention (`retention_days`).
  - Bots: `fleet.yaml` `defaults.telemetry` with a `bots.<name>.telemetry` override (same precedence
    as other bot scalars, `_select_bot_scalar`), composed into `bot.conf` (`Switch` `telemetry`,
    scope `GENERATE`, carrier `COMPOSE_BOT`). Fields: `enabled` (default false) and `content`
    (`metadata`, the default, or `full`; `full` is the explicit, disclosed act design v2 §11 asks
    for and maps to the runtime's prompt/tool-detail gates).
  - `config plan` fails when a bot has telemetry enabled on a host whose Collector is not enrolled.
- Ratifier: operator. Status: locked (c), 2026-10-04.

*Forge note (2026-10-04), F4 — three spellings corrected, decision unchanged:* `plane-prune` ships **on**
(`claudlobby/switches.py:326-335`, `polarity=OPT_OUT`); the permanently opt-in precedents are
`plane-prune-system-events` (`:350-381`) and the `ENROLL_HOST` jobs `update-siblings`/`vault-sync`. No
`retention_days` key exists anywhere in Claudlobby; retention rides the job's `script` line
(`system.yaml:88-91`, `plane-prune.sh:39`), so the Collector's is `--retention-days N`. `telemetry` is a
two-field mapping, so it follows the `observability`/`isolation` parsing precedents
(`claudlobby/config.py:1402-1424,1522-1551,1682-1718`), not `_select_bot_scalar`. Spec: §6 P2 "the
`fleet.yaml` telemetry schema".

**F5 — Normalized attribute names.**
- Options: (a) OTel GenAI semantic conventions (`gen_ai.conversation.id`, `gen_ai.tool.name`,
  `gen_ai.usage.*`) plus `agent.runtime`; (b) house names.
- Lean: **(a)**.
- Ratifier: operator. Status: locked (lean), 2026-10-04.

*Forge note (2026-10-04), F5:* the GenAI conventions moved to their own repository with no release tag
and every `gen_ai.*` attribute at `Development`; `gen_ai.system` is deprecated in favour of
`gen_ai.provider.name`, and the token *metric* the §2.4 sketch implied no longer exists. The decision
stands for attribute names; the Collector config pins the semconv commit it was written against, and
the plane's registry names are the stable reader surface. Spec: §6 P2 "the Collector configuration".

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
- Ratifier: operator. Status: locked (lean), 2026-10-04.

*Forge note (2026-10-04), F6 — two facts the context lacked:* (1) the digest rows are invisible to all
four consumers today — `plane-readers.py` `fleet_events` filters `source_ref LIKE 'fleet-events:%'`
(`:1155-1164,1250`) while `transcript-digest.sh:340-342` stamps `session-digest:<sid>`, and the reader
projects the nested `detail.data` shape `emit_fleet_event` writes (`:1226-1230`), so even a matching
row would render `data: {}`; (2) a `system` row cannot carry the `session_uid` stream column
(`contracts.py:124-141`, DDL `0011:150`). The `session_summary` registration precedent is
`registries.py:201-204` (not `:111-121`). Spec: §6 P3 "the `session_summary` plane event".

**F7 — Who ships the Codex hook adapters.**
- Options: (a) each contract owner ships its own (Claudron `hooks.py` per boundary spec Q1; clauDNA
  a Codex host manifest with its store hooks) and Claudlobby composes them with a parity gate, as it
  does for the Claudron loop (`tests/test_claudron_loop.py`); (b) Claudlobby writes every Codex hook.
- Lean: **(a)**.
- Ratifier: operator. Status: locked (lean), 2026-10-04.

**F8 — clauDNA activity layer.**
- Options: (a) freeze: no new kinds, hooks stay wired; (b) unwire the three activity hooks, keep the
  registry and readers; (c) remove.
- Lean: **(a)** now; (b) once interactive users have OTel too.
- Ratifier: operator. Status: locked (lean), 2026-10-04.

**F9 — Harvest provenance for non-Claude sessions.**
- Options: (a) `session:<runtime>/<sid>:<seg>` for non-Claude, unchanged for Claude; (b) always
  qualified.
- Lean: **(a)**, mirroring F2.
- Ratifier: operator. Status: locked (lean), 2026-10-04.

*Forge note (2026-10-04), F9:* provenance is one function, `filing.evidence_ref(sid, index)`
(`lib/claudna/session_store/filing.py:97-99`), and it feeds two Claudron channels — `source_url`
(`claudron/engine.py:160-161`) and the `amend` evidence ref (`claudron/amend.py:37,63,84`) — both of
which treat the string as opaque, so `session:codex/<sid>:<seg>` needs no engine change.

**F10 — Summarizer for Codex sessions.**
- Options: (a) keep `claude -p` (Haiku) for every runtime; (b) a per-runtime runner (`codex exec`).
- Lean: **(a)**. Consequence: a host running Codex bots with summaries on also needs a Claude binary
  and account. A Codex-only host means (b), or summaries off.
- Ratifier: operator. Status: locked (lean), 2026-10-04.

**F11 — Launching Codex bots.**
- Options: (a) in this epic; (b) a companion epic (§5).
- Lean: **(b)**. This epic lands the vocabulary and the owner-side adapters; every step that
  composes or validates a Codex bot lives in the companion, because Claudlobby's mandatory runtime
  validation needs a bot that can run.
- Ratifier: operator. Status: locked (lean), 2026-10-04.

**F12 — clauDNA skills on Codex.** Out of scope (behavior parity is its own epic). The Codex host
manifest ships store hooks only. Ratifier: operator. Status: locked (lean), 2026-10-04.

**F13 — clauDNA's owner-pid field for Codex.**
- Context: `session.opened.claude_pid` (`lib/claudna/session_store/events.py:96`).
- Options: (a) reuse `claude_pid`, documented as "the owning agent process"; (b) a new optional
  `owner_pid`.
- Lean: **(a)** until the next envelope major (no schema change; older readers unaffected); rename
  then.
- Ratifier: operator. Status: locked (lean), 2026-10-04.

*Forge note (2026-10-04), F13:* the recorded pid has three readers, not one — the nested-child guard
(`boundaries.py:287-314`), the clear link (`links/<pid>.json`, `lineage.py:26-27,73-97`) and the
abandoned-session sweep (`unclosed.py:67,101-110`, `os.kill(pid, 0)`). Whatever C2 finds for Codex
must serve all three. Spec: §6 P4 "clauDNA's host-adapter interface".

**F14 — Per-bot clauDNA state directory cutover.**
- Context: bots share `~/.claudna` today; P3 moves each to `$BOT_DIR/data/claudna`, which strands
  open sessions and export cursors in the old root.
- Options: (a) seal every open session in the old root at the switch (`session_store seal`), keep it
  read-only until its retention window passes, then remove; (b) migrate sessions into per-bot roots.
- Lean: **(a)**.
- Ratifier: operator. Status: locked (lean), 2026-10-04.

**F15 — Waive the pre-registered summarizer comparison.**
- Context: clauDNA spec §1.1 rule 4 requires a pre-registered siloed comparison of
  `transcript-digest.sh` and the store's summarizer before one owner is chosen (Claudlobby#1961).
  The operator has decided to retire the digest, which is dormant by default.
- Options: (a) waive the comparison and record the decision in the spec; (b) run the comparison as
  a P3 entry gate.
- Lean: **(a)**, keeping rule 4's coverage concern (skipped sessions) through F6.
- Ratifier: operator. Status: locked (lean), 2026-10-04.

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
- Ratifier: operator. Status: locked (lean), 2026-10-04.

*Forge note (2026-10-04), F16:* clauDNA has two SessionStart hooks. The briefing hook
(`plugin-hooks/session-start.sh`) is what bots turn off (`CLAUDNA_SESSION_BRIEFING=0`) and fires only
for `startup|clear`; the store hook (`session-store.sh SessionStart`, no matcher) is gated by
`CLAUDNA_SESSION_STORE`, the same switch that decides whether there is anything to export. The write
site is therefore the store's Python `_session_start` (`boundaries.py:138-162`), which already
computes the exact entrypoint path (`:174-177`). Spec: §6 P3 "the clauDNA export contract additions".

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
- Ratifier: operator. Status: locked (lean), 2026-10-04.

*Forge note (2026-10-04), F17 — citation corrected, decision unchanged:* `session_uid` enters the hashed
facts through the ingest projection (`claudlobby/plane/ingest.py:256` for task events, `:162` for
`sender_session_uid`), not through `request_facts.py:42-51`, which names no field. The task doors
conflict at `request_receipts.py:542-543`; the message doors conflict earlier, at
`message_operations.py:494-495`. The two format literals (`runtime_versions.py:21`,
`request_receipts.py:25`) are untied today. Spec: §6 P1 "the request-receipt change for F17".

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

### 4.1 Forge additions (2026-10-04)

Claudlobby (`cd292cb`):
- `emit_batch` is `claudlobby/plane/emit_api.py:149-153`, not `ingest.py`; the daemon is its socket front
  (`daemon.py:207-208,667-668`) and the only resident writer. `ContractViolation` refuses a whole batch
  with nothing written or spooled.
- `session` is a legal subject kind everywhere (`contracts.py:86,732-733`, the DDL CHECKs) but no writer
  uses it; `MetricSample` has only the alias form and `identity.resolve` mints a random uid on first sight
  (`identity.py:36`). `PRUNABLE_SYSTEM_EVENTS` lives in `plane/retention.py:88-91`.
- The `session_digest` rows never reach their consumers (F6 note above). `legacy_event_row` is the one
  projection the four consumers read through.
- Receipts: replay equality is whole-dataclass (`request_receipts.py:542-543`); `_decode` back-fills
  `expected_by` for older files (`:416-418`) — the precedent for a new intent field; the readability
  gate is `releases.Compatibility` consumed by `migration_plan._readability_blockers` (`:69-78,425-430`),
  so a strict version bump turns every retained v1 receipt into a blocker.
- `BotPayload` is `_Strict` (`extra="forbid"`, `:184-185`) and validated at ingest (`:851-862`): an emitter
  adding `runtime` against an older daemon is refused. The additive-field precedent is
  `WorkstreamEvent.waiting_on` plus the `wire_additions` declaration (`migration_plan.py:421-424`); no
  payload-schema bump has ever happened (`registry_emit.py:77`).
- `config plan` fails through `validate()` → `report.errors` → `PlanError` (`config_staging.py:174-176`);
  `validator.py` reads `load_host_jobs()` nowhere today. `host setup` installs no third-party binary
  (`commands/setup.py:85-88` refuses on a missing `tmux`/`claude`/`jq`).
- `task show` prints `asdict(task)` (`task_read.py:69-73`) and the reducer drops the `session_uid` column
  it selects (`task_state.py:77-85,200-204`); `plane lookup` is the runtime script
  `_runtime_scripts/plane-lookup.py`, not a CLI command.
- Prior art this plan did not cite: #1997 (a per-bot `agent_cli:` key, and the naming clash — `runtime`
  already names `activation_runtime.py`/`runtime_admission.py`/`runtime_versions.py`, `host update
  runtime` (the Claude Code binary) and `config validate --runtime`; it also records that
  `PROJECT_MISSION.md:114` lists per-bot provider abstraction under "What we choose not to build"),
  #874 (bot-vitals falls back to `$PWD`), #902 (telemetry-loss observability), #930 (ledger noise).

clauDNA (v0.26.0):
- `boundaries.handle` (`:317`) is already the neutral shell; Claude knowledge also sits in `activity.py`
  (`:26-29,93-133`), `transcript.py` (including the byte prefilter `:86-88`), `lineage.py:65`,
  `unclosed.py:67,101-110`, and the `events.py` vocabularies (`:97,117,142`). The layering gate globs flat
  modules (`tests/test_runtime_layout.py:151`) and fails an unranked one (`:154`).
- `CLAUDE_CODE_CHILD_SESSION` is read nowhere; `CLAUDE_CODE_SESSION_ID` only in the guard's docstring
  (`boundaries.py:290` — prose, not code) and the summarizer strip (`summarize.py:84`).
- The export passes over three kinds of segment with no item: skipped (`export.py:113-114`), retired with
  no archive (`:108-109`), settled give-ups (`:115-116`); `SESSION_FIELDS` (`:46-47`) is the item's
  session subset. `session.schema.json` is `additionalProperties: false` with `const claudna.session/1`,
  so `runtime` needs `claudna.session/2` (the `claudna.segment/2` precedent).
- The summarizer gate returns `headless` for `actor.kind in (headless, bot)` (`project.py:282-283`), so
  the actor kind a Codex host derives decides whether a Codex session is ever summarized (F10).
- Prior art not cited: #120 (Codex fetches the marketplace but does not install the plugin), #300 (the
  Codex adapter-layer epic), #306 (deferred Codex orchestration).

Claudron (v0.9.0):
- `SNIPPET_EVENTS` (`hooks.py:235-239`) and `merge_settings`'s `event_cmds` (`:388-392`) are byte-identical
  dicts; the local copy would `KeyError` on an event added only to the first. `hooks install` takes only
  `--write`/`--settings` (`cli.py:1720-1733`); the name-sniff at `hooks.py:62` was removed in 0.4.0.
- `ops.py:33` `_ID_RE = [A-Za-z0-9][A-Za-z0-9._-]{0,127}` admits a UUID and silently skips anything with
  `:` or `/`; nothing pins it to the contract text.
- The parity test `TestSessionProtocolDocParity.test_snippet_shape_matches_the_installer`
  (`tests/test_hooks.py:246-257`) compares the first fenced block of the contract's `## Session-loop
  protocol` section, so a Codex snippet needs its own `##` section or a reader extension. No session-loop
  capability exists yet (only `doctor-settings`); the gate sentence convention is "Gate on `"<cap>" in
  status --json → data.capabilities`".
- The consumer-CI job the plan's §12 names is #223 (main `8e895cb`), one commit past the v0.9.0 tag.
- Prior art not cited: #178 (SessionStart brief re-injected on resume), #179 (a PreCompact block reason
  likely never reaches an unattended model — see C3).

Runtimes *(doc / source, read 2026-10-04)*:
- Claude Code: `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_CHILD_SESSION` and `CLAUDE_PID` are documented
  (`CLAUDE_CODE_ENTRYPOINT` is not); the hook `session_id` "matches" `CLAUDE_CODE_SESSION_ID` and both
  change on `/clear`; `OTEL_*` is scrubbed from every subprocess; there is no default OTLP protocol; metric
  temporality defaults to delta; `OTEL_RESOURCE_ATTRIBUTES` keys land on every datapoint and event;
  `token.usage{type=input}` excludes cache tokens.
- Codex: `[otel].metrics_exporter` defaults to `statsig` (a hosted endpoint); `exporter = "none"` is the
  log default; hooks are on by default and need trust-by-hash; `SessionEnd.reason` is always `other`,
  with a 1 s default / 3 s maximum timeout, and does not run for subagents; subagent hooks reuse the
  parent `session_id`; hook commands receive `PLUGIN_ROOT`/`CLAUDE_PLUGIN_ROOT` (C8); child shells get
  `CODEX_SESSION_ID`/`CODEX_THREAD_ID` (source only, C1); rollouts are
  `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl` with `session_meta`, `response_item`,
  `compacted`, `turn_context`, `event_msg` records (C4).
- Collector: the core `otelcol` distribution has no `transform` processor; contrib v0.162.0 is ~100 MB
  on disk, with no documented baseline RSS (C6 measures). The GenAI conventions carry no release tag.

## 5. Companion plans and blocks

- **Companion — Codex execution adapter** (F11): launching, supervising and validating Codex bots,
  and the Claudlobby-side Codex composition (hooks file, `[otel]`, plane hooks). Blocked by P1 and
  P4 here. Ends with a mixed canary fleet.
- **Companion — clauDNA on Codex** (F12): skills and agents for Codex hosts.
- **Blocks:** any mixed-runtime team feature; the v2 §9b usage decisions.

### 5.1 What P4 and the companions wait on (forge, 2026-10-04)

No per-PR plan is written for these until the canary answers are in the run log:

| Work | Waits on | What the answer changes |
|---|---|---|
| P4 clauDNA — `host_codex.py`, the rollout reader, the Codex manifest | C1 (field names, `session_id` format against `paths._SID_RE`, `transcript_path` nullability), C2 (owner pid or walk), C4 (record types), C8 (plugin root), C9 (`--host` selection) | the `Host` table in the P4 spec; whether the clear link and the sweep work on Codex (F13) |
| P4 Claudron — the Codex adapter in `hooks.py`, the normative snippet, the capability | C1 (event names, `source` values), C3 (stdout injection on `SessionStart(compact)`; whether `PreCompact continue:false` is usable; and, per Claudron#179, whether a block reason reaches an *unattended* Claude model at all) | whether R-capture-prompt is held on Codex, and whether it is held on unattended Claude bots |
| P4 Claudlobby — payload adapters for `plane-dispatch-in` and the `bot-vitals` marker | C1 | field names only |
| Companion #2149 (Codex execution adapter) | C1–C4, C5 (`[otel]` shapes; that `metrics_exporter="none"` + `[analytics] enabled=false` keeps the box silent; a per-bot carrier such as `environment`), C7 (tmux), C8, C9; plus P1 and P4 releases | launcher, `config.toml`, hooks composition, trust-by-hash handling, the `[otel]` block |
| Companion clauDNA#404 (clauDNA on Codex) | C1, C3, C8; plus the P4 manifest | which hooks beyond the store carry over; the manifest's skills scope |

Cross-references the companions should carry: Claudlobby#1997 (the `agent_cli:` proposal and the
`PROJECT_MISSION.md:114` exclusion, §14 Q1–Q2), clauDNA#120/#300/#306 (Codex marketplace install, the
adapter-layer epic, deferred orchestration — #404 overlaps #300 items 1, 2 and 4), Claudron#178/#179.

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
  - Is there a session-id env var for tools and CLIs (the analogue of `CLAUDE_CODE_SESSION_ID`)? Source
    says child shells get `CODEX_SESSION_ID` and `CODEX_THREAD_ID` and hook commands get neither; measure both.
  - Does `session_id` match `paths._SID_RE` (`[A-Za-z0-9][A-Za-z0-9._-]{0,127}`) and Claudron's `ops.py:33`?
  - `SessionEnd`: the docs say `reason` is always `other`, the timeout is 1 s by default (3 s max), and the
    hook does not run for subagents; measure what a 1 s budget leaves the store's `SessionEnd` path.
  - Hooks need trust-by-hash before they run *(doc)*: record the `/hooks` step a composed bot would need.
- [ ] **C2** Codex owner process: is there an env var like `$CLAUDE_PID`, and what does an ancestor walk find?
- [ ] **C3** Codex context injection: is `SessionStart` stdout injected (the docs say plain stdout is "added as
  extra developer context", and `source: compact` hooks run "before the next model request")? Can
  `PreCompact` block (`continue: false`) or add an instruction (the docs say its stdout is ignored)? Also,
  per Claudron#179, run the same test on **Claude**: does a PreCompact `decision: block` reason ever reach an
  unattended model, or only `SessionStart(compact)` stdout? This decides where R-capture-prompt lives on both.
- [ ] **C4** Codex rollout files: are they append-only and byte-addressable? Which record types carry user and assistant text?
- [ ] **C5** Codex OTel:
  - the actual event names and attributes;
  - whether `conversation.id` equals the hook `session_id`;
  - whether any traces exist;
  - whether it supports OTLP/HTTP JSON and resource attributes (source: `protocol = "json"`; the
    endpoint is the full `/v1/logs` URL; `environment` is the only free per-config string — is it usable as
    the per-bot carrier?);
  - that `metrics_exporter = "none"` plus `[analytics] enabled = false` leaves no outbound connection
    (the default `statsig` exporter is hosted) — capture the socket table while a session runs.
- [ ] **C6** Claude OTel on a bot:
  - does `session.id` equal the hook `session_id`?
  - event volume per bot-hour;
  - Collector RSS and CPU on the smallest host class;
  - does it cover every `tool_call` the plane records today?
  - do the `OTEL_RESOURCE_ATTRIBUTES` keys land on every datapoint and event, as documented?
  - is temporality delta, and does the 60 s metric / 5 s log export interval hold under a busy bot?
  - the budget the P2 canary is judged against: Collector RSS and CPU, intake RSS, `state/otel/` growth per
    bot-hour (the proposed figures are in the P2 Collector-and-intake spec; C6 replaces them with actuals).
- [ ] **C7** Codex in tmux: prompt glyph, slash-command injection, restart behavior. Feeds the companion.
- [ ] **C8** Codex plugin-manifest hooks: how does a hook command resolve its plugin root (clauDNA uses
  `${CLAUDE_PLUGIN_ROOT}`)? Source says Codex sets `PLUGIN_ROOT`, `PLUGIN_DATA`, `CLAUDE_PLUGIN_ROOT` and
  `CLAUDE_PLUGIN_DATA` for plugin hook commands; confirm, and record whether a marketplace-installed clauDNA
  loads at all (clauDNA#120 saw the marketplace fetched but the plugin not installed).
- [ ] **C9** Host identification: confirm the adapter can be selected by the hook command itself (`… hook <event> --host codex`) rather than sniffed.
- [ ] **C10** `CLAUDE_CODE_CHILD_SESSION` leak: when a bot's tmux server is started from inside another Claude session, does the variable reach the bot? `start-bot.sh` does not scrub env. If it does, P3's guard change needs a scrub first.
- [ ] **C11** Claude session id in tools: in a tmux-hosted bot, does the Bash tool's `CLAUDE_CODE_SESSION_ID` equal the SessionStart payload's `session_id`, from the main thread and from an Agent subagent (whose shells carry `CLAUDE_CODE_CHILD_SESSION=1`)? If a subagent's id differs, F1(c) doors running under `CLAUDE_CODE_CHILD_SESSION=1` record no uid. (The
  env-vars reference now says the variable "matches the `session_id` field in the hook JSON input" for Bash
  and hook subprocesses; the canary still measures it, from a subagent in particular.)
  - Also record the **hook process's own env** for a subagent's `PostToolUse`/`PostToolUseFailure` (a 3-line
    scratch hook appending `env | grep ^CLAUDE_CODE_` to a file): does it carry `CLAUDE_CODE_CHILD_SESSION=1`
    with the parent's `session_id`? If it does, P3's clauDNA guard restricts the marker check to the lifecycle
    events so the parent keeps its subagents' `skill.invoked`/`tool.failed` (the P3 clauDNA plan's canary table).
- [x] Ratify F1–F17 (operator, 2026-10-04).

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
  - **Key name (§14 Q1):** #1997 proposes `agent_cli:` because `runtime` already names release activation,
    the Claude Code binary update and the composed-output audit. Forge's lean is to keep `runtime:` (it is
    the cross-repo vocabulary F2/F9 already use, and `host update runtime` already means the agent binary)
    and to document the distinction from `runtime/`; the operator decides before this PR opens.
  - **Mission (§14 Q2):** `PROJECT_MISSION.md:114` excludes "per-bot LLM provider abstraction"; the PR
    amends that line to what F11 ratified, or the operator records that the ratification supersedes it.
  - The validator rejects unknown values.
  - A `codex` bot fails validation with "execution adapter not shipped" until the companion lands.
  - Docs: `documentation/fleet-yaml-schema.md`, `fleet.yaml.example`, `config_explain`.
- [ ] Composed `CLAUDLOBBY_RUNTIME` in `bot.conf`, so the doors know the runtime for `derive_session_uid(…, runtime)`. Add a row in `documentation/environment-variables.md` and a composer test.
- [ ] `BotPayload.runtime` in the registry keyframe (`contracts.py:670-688`, `extra="forbid"`), with a payload schema bump and `registry_emit.bot_payload`.
  - Decided in the per-PR plan: an **additive optional field with a `wire_additions` declaration, no
    payload-schema bump** (the `WorkstreamEvent.waiting_on` precedent, `migration_plan.py:421-424`), written
    only when it carries information — `registry_emit.py:170-173`'s rule, so a keyframe drained by an older
    daemon keeps the shape every daemon accepts. In P1 every bot is `claude`, so no keyframe carries the key
    until the companion ships on a release whose floor includes this contract.
- [ ] `derive_session_uid(platform_session_id, runtime="claude")` per F2, in Python (the doors' only derivation). The bash mirror in `plane-session-start.sh` stays Claude-only until it retires in P3 (no Codex bot can run before the companion). Extend `tests/test_plane_ids.py` with a Codex case.
- [ ] Task and report doors attach `session_uid` per F1(c), derived from the caller's session-id env (§2.2):
  - `task_operations.accept` and `_assignment_report` (`claudlobby/task_operations.py:445, 473+`; raws built by `_raw()` at `:312-317`) set `events.session_uid`;
  - `encode_report_facts` (`fleet reports submit`) and the send path in `message_operations` set `sender_session_uid`.
  - **Receipts:** per F17, a retried `--request-id` after a `/clear` or restart must not raise `ReceiptConflict`. Under F17(a), `RequestIntent` carries `session_uid` and a replay reuses it (receipt format bump). Test exactly that replay.

  `task show` (the reducer must keep the `session_uid` column it already selects, `task_state.py:77-85,200-204`)
  and `_runtime_scripts/plane-lookup.py` print the session. Tests cover each door.
- [ ] Amend design v2 per §1.1.

clauDNA:
- [ ] `session.opened.data.runtime`: optional, top-level, `choices=("claude","codex")`; when absent it reads as `claude`.
- [ ] `session.json` gains `runtime`: tag `claudna.session/2`, `claudna.session/1` added to `OLDER_PROJECTIONS`, and the stale-projection sweep taught about `session.json`. Mixed 0.26/0.27 **readers** then converge instead of
  rewriting each other's file (a 0.26 reader folds a `/2` file in memory and writes nothing). A 0.26 **writer** —
  a bot not yet restarted onto 0.27, or its detached summarizer/harvest/sweep — still rebuilds the file to `/1`
  on its own writes and 0.27 rebuilds it forward: one rebuild each, lossless (the log holds `runtime`), ending
  when the last 0.26 process restarts. The per-PR plan pins both halves with tests.
- [ ] `claudna.export/1` items gain `runtime` (additive). Document it in spec §8.
- [ ] Harvest provenance per F9. Add a case to `tests/test_claudron_live.py`.
- [ ] Amend spec §1.1: rule 1 (the plane no longer records every tool call after P2) and, if F15(a) is ratified, rule 4 (the comparison is waived; the store's summarizer is the single owner; the coverage concern is kept by F6).
- [ ] Release clauDNA (the export and session fields are needed by P3).

#### Spec: the request-receipt change for F17 (`RequestIntent.session_uid`, the format bump, the replay path)

*Added by `/claudna:forge`, 2026-10-04. Grounded in Claudlobby `cd292cb`. Implements F17(a) as ratified.
One citation in F17's context is corrected: `session_uid` enters the hashed facts through the ingest
projection, not through `request_facts.py:42-51`.*

**Why a retry conflicts today.** A receipt stores no field values, only hashes and field names
(`claudlobby/request_facts.py:3-4`): the hashed set is the envelope minus the ingest-stamped columns plus
every family column (`:46-51`). `session_uid` is among those columns because `ingest._family_values`
always emits it — `"session_uid": payload.session_uid` for task events (`claudlobby/plane/ingest.py:256`)
and `"sender_session_uid"` for communications (`:162`) — so today its `null` is already inside every
`projection_sha256`. Once P1's doors fill it, a retried `--request-id` after a `/clear` or restart rebuilds
the facts with the *new* session's uid, the hash differs, and:

- task doors conflict at `store.prepare`, `claudlobby/request_receipts.py:542-543` (`previous.intent != intent`,
  whole-dataclass equality through `stages → facts → projection_sha256`), reached from
  `task_operations._prepare` (`claudlobby/task_operations.py:320-331`), after `_existing` (`:245-260`,
  which compares operation, version, host, fleet, caller, recipient, semantic digest, stage kinds, fact
  count and route — never the fact hashes) and `_replayed` (`:292-309`) have passed;
- message doors (`message send|reply`, `fleet reports submit`) conflict earlier, at
  `claudlobby/message_operations.py:494-495` (`existing.intent.stages[0].facts != facts` →
  `ReceiptConflict("capture policy or communication projection changed")`) and `:667-669`
  (`record_reply_to_human`).

**The change: the intent carries the uid, and a replay rebuilds its facts from the frozen one.**

```python
# claudlobby/request_receipts.py:128-142
@dataclass(frozen=True)
class RequestIntent:
    ...
    expected_by: str | None = None   # task routing's frozen fleet-default deadline only
    session_uid: str | None = None   # F17: the caller's session uid at the first attempt; a replay reuses it
```

- **Resolution, once per invocation.** `operation_context` (`claudlobby/operation_context.py:182-210`, where
  the caller selectors are read) resolves `session_uid = derive_session_uid(os.environ.get("CLAUDE_CODE_SESSION_ID"), runtime=os.environ.get("CLAUDLOBBY_RUNTIME", "claude"))`
  when the id is present and `None` otherwise (§2.2: "with no session-id env, the door records no uid rather
  than a possibly wrong one"), and `TaskOperationContext` (`claudlobby/task_operations.py:46-66`) carries it.
  Canary C11 decides whether a subagent's shell (`CLAUDE_CODE_CHILD_SESSION=1`) sees the same id; if it does
  not, the resolver returns `None` under that variable rather than another session's uid.
- **Task doors.** `_prepare(...)` (`:320-331`) gains `session_uid`; its callers pass
  `previous.intent.session_uid if previous is not None else ctx.session_uid` — the pattern `_assignment_report`
  already uses to reuse `message_id` and `event_ids` from `previous` (`:511-513`) and `expected_by`
  (`:267-278`). The payload dicts handed to `_raw` (`:312-317`) set `"session_uid"` (the accept payload at
  `:460-461`; the report's linked task detail via `encode_report_facts`, `claudlobby/report_payload.py:198-199`).
  `_existing` is unchanged: a different uid is not "different semantics" — it is the same request, retried.
- **Message doors.** In `send_message`'s `run(store)` (`message_operations.py:449-466`), when `existing` is
  not `None`: `session_uid = old.session_uid` (beside `message_id = old.message_id`); the communication dict
  (`encode_communication`, `claudlobby/message_payload.py:145-152`; `encode_report_facts`,
  `report_payload.py:189-190`) sets `sender_session_uid` from it, so the recomputed facts equal the stored
  ones and `:494-495` no longer fires; a new receipt is built with `RequestIntent(..., session_uid=…)`.
  The unlinked report's marker is a `system` row with no session slot (`contracts.py:122-125`); it is unchanged.
- **Validation and decoding.** `_validate` (`:299-322`) checks `session_uid` with `_id(value, "session")`
  (`ID_PATTERNS["session"]`, `^sess_[0-9a-f]{32}$`, `claudlobby/plane/ids.py:34-40`) when not `None`;
  `_decode` back-fills `"session_uid": None` for files that lack it, exactly as `expected_by` is back-filled
  (`:416-418`), so the round-trip check at `:446-447` keeps passing for every receipt already on disk.

**The format bump — two literals, one gate.** `request_receipts.FORMAT_VERSION = 1` (`:25`) and
`runtime_versions.RECEIPT_FORMAT_VERSION = 1` (`claudlobby/runtime_versions.py:21`) are untied today (no
import, no test); the release "gate" is the readability declaration consumed by
`releases.Compatibility` / `SupportedVersions.supports` (`claudlobby/releases.py:106-160`) and
`migration_plan._readability_blockers` (`claudlobby/migration_plan.py:69-78,425-430`), which turns any
retained receipt the target cannot read into `target cannot read retained receipt_format: N`. Therefore:

| Where | Today | After |
|---|---|---|
| `runtime_versions.py:21-22` | `RECEIPT_FORMAT_VERSION = 1`; `SUPPORTED = {0, 1}` | `= 2`; `SUPPORTED = {0, 1, 2}` — v1 stays readable, per the module rule "Add readable versions only alongside their implemented decoders" (`:10`) |
| `request_receipts.py:25` | `FORMAT_VERSION = 1` | `from .runtime_versions import RECEIPT_FORMAT_VERSION as FORMAT_VERSION, SUPPORTED_RECEIPT_FORMAT_VERSIONS` — the two literals become one |
| `request_receipts.py:300-301` `_validate` | `format_version != FORMAT_VERSION` → error | `format_version not in SUPPORTED_RECEIPT_FORMAT_VERSIONS - {0}` → error (0 is "absent", never a file) |
| `_save` (`:518-533`) | stamps the dataclass default | a **new** receipt is written at 2; a loaded v1 receipt keeps `format_version: 1` through `replace(...)` and is never restamped (its intent has `session_uid: None` by back-fill, which is also what a v2 writer would have recorded for a session-less caller) |

Tests this touches, by pin: `tests/test_request_receipts.py:127-131` (format 2 is now valid, 3 is refused;
a v1 file loads with `session_uid is None`), `tests/test_releases.py:93-96` (`"receipt_format": 2`),
`tests/test_migration_plan.py:229-233,356-366,416-421` (the `[1]` legs gain a v1-retained → v2-target case
with **no** blocker and a v2-retained → v1-target case with the blocker), `tests/test_release_install.py:118`
(the sealed manifest picks the declaration up unchanged). **The F17 test itself:** accept an assignment from
session A (uid A recorded on the task event); `/clear`; retry the same `--request-id` from session B →
`status: committed`, no `ReceiptConflict`, the recorded row still carries uid A, and the receipt on disk
is unchanged; a *new* request from session B carries uid B. The same pair for `message send` and
`fleet reports submit` (`sender_session_uid`).

### P2 — Local OpenTelemetry pipeline (Claudlobby; gated by F4)

- [ ] The `otel-collector` host service (F4): a `Switch` row (`HOST_SERVICE`, `ENROLL_HOST`, opt-in) and `host.jobs.otel-collector` (`unit: service`) running upstream `otelcol-contrib` on `127.0.0.1`. The binary is operator-installed (Claudlobby installs
  no third-party binaries; `host setup` refuses when the job is enrolled and the binary is missing, as it
  does for `tmux`/`claude`/`jq`, and `host doctor` gains a rung). The install instruction changes what a
  new user is told to run, so a cold-host onboarding run applies per the onboarding rule.
  - The composer renders its config from `system.yaml`:
    - an OTLP receiver;
    - a `transform` processor (F5);
    - a `resource` processor;
    - a `file` exporter to `state/otel/` with rotation; the host's retention is `--retention-days N` on the
      job's script line (the `plane-prune --days` convention), rendered into the exporter's `max_days`;
    - an OTLP/HTTP exporter with `encoding: json` to `plane-otel`.
  - Validate the rendered config with `otelcol validate` in tests.
- [ ] Per-bot telemetry from `fleet.yaml` (F4): `defaults.telemetry` and `bots.<name>.telemetry` (`enabled`, `content`) parsed into `BotConfig`, with a `Switch` row (`GENERATE`, `COMPOSE_BOT`, opt-in), docs in `documentation/fleet-yaml-schema.md` and `fleet.yaml.example`, and regenerated switch tables. For an enabled **Claude** bot the composer writes into `bot.conf`:
  - `CLAUDE_CODE_ENABLE_TELEMETRY=1`;
  - `OTEL_LOGS_EXPORTER=otlp` and `OTEL_METRICS_EXPORTER=otlp`;
  - the protocol and an endpoint on `127.0.0.1`;
  - `OTEL_RESOURCE_ATTRIBUTES` with fleet uid, `bot:<fleet>/<bot>` and `agent.runtime=claude`;
  - the content gates (`OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_TOOL_DETAILS`) only when `content: full`.

  Applied at the bot's next restart. `config plan` fails when an enabled bot's host has no enrolled Collector; test it.
- [ ] `plane-otel` intake: its own localhost host service, not the daemon or the view, which are pinned to their scopes. It maps an allowlist onto the daemon's socket (`emit_batch` behind it; the daemon stays the single writer):
  - per-session cost, tokens, tool calls/failures, API requests/errors and active time as `metric_samples`
    on `subject_kind='session'` (five new `METRIC_NAMES`), with the derive-not-mint rule for session
    subjects so the sample's `subject_uid` is the F2 uid;
  - `api_error` and repeated tool failure as system events (`SYSTEM_EVENT_SEVERITY`).

  Also: a doors-table row in `documentation/architecture/observable-plane.md`, and a launcher in `_runtime_scripts/` with its index line in both `CLAUDE.md`/`AGENTS.md` pairs, within the 32 KiB Codex budget (`tests/test_instruction_budget.py`).
- [ ] Canary: one Claude bot, one host, one week. It passes when C6 holds and the Collector stays inside its stated resource budget.
- [ ] Then stop `bot-vitals.sh` emitting `tool_call`, and remove the `session_event` path (no payload carries that key).
  - It keeps touching `.last-tool-call`.
  - The registry entries stay, and `tool_call` stays in `PRUNABLE_SYSTEM_EVENTS` so existing rows can still be pruned.
  - Update `tests/test_event_type_registry.py:100-101`, `tests/test_plane_cutover_keepalive.py:94-110`, and `tests/test_plane_emit_class.py:170-185`. (`tests/test_system_event_retention.py:100` pins `{"tool_call","wip_uncommitted"}` and stays unchanged.)
  - Update `library/protocols/fleet-observability.md:66`, `documentation/guides/observability.md:83,88`, and the `bot-vitals` index rows (root and `_runtime_scripts` `CLAUDE.md`/`AGENTS.md`).
- [ ] Decide v2 §9b's usage items on the evidence; `claudlobby/transcript_usage.py` retires if OTel answers its questions.

#### Spec: the `fleet.yaml` telemetry schema, the `system.yaml` Collector entry, and the `config plan` check (F4)

*Added by `/claudna:forge`, 2026-10-04. Grounded in Claudlobby `cd292cb`. Implements F4(c) as
ratified; two spellings in F4's text are corrected below and listed in the forge change log.*

**`fleet.yaml`.** A two-field mapping, settable under `defaults` and per bot, merged field-wise with the
bot winning (the `observability` precedent, `claudlobby/config.py:1402-1424,1522-1551`), unknown keys
refused (the `isolation` precedent, `:1682-1718`):

```yaml
defaults:
  telemetry:
    enabled: false        # STRICT bool (config._strict_bool, config.py:1652-1663): a typo string is a parse error, never an arming
    content: metadata     # metadata | full  (config._parse_enum, :1721-1731)
bots:
  canary-1:
    telemetry:
      enabled: true       # one bot first, on an independent canary root
```

```python
# claudlobby/config.py — beside IsolationConfig / ObservabilityConfig
@dataclass(frozen=True)
class TelemetryConfig:
    enabled: bool = False
    content: str = "metadata"        # "metadata" | "full"

TELEMETRY_CONTENT = ("metadata", "full")

def _parse_telemetry(raw: object, where: str) -> TelemetryConfig: ...   # unknown key → ValueError(f"{where}: telemetry: unknown key(s) …")
def _merge_telemetry(default: TelemetryConfig | None, bot: TelemetryConfig | None) -> TelemetryConfig: ...   # field-wise, bot over defaults, built-in last

# BotConfig gains:
    telemetry: TelemetryConfig = TelemetryConfig()
```

`scalar_config_origin` (`config.py:2262-2297`) learns the mapping so `config explain` reports where each
field came from (`bot` / `fleet.defaults` / `built_in`). `_select_bot_scalar` (`:1735-1741`) is not used:
it selects whole values by presence, and a bot that sets only `enabled: true` must still inherit
`content` from `defaults`. *(Correction to F4's "same precedence as other bot scalars,
`_select_bot_scalar`": the precedence is the same; the helper is the mapping one.)*

**`content: full` is the disclosed act** design v2 §11 asks for ("native OTel content gates … **off by
default**, enabling them is an explicit disclosed act"). It is the only thing that composes the runtime's
prompt and tool-detail gates (below). Nothing in `metadata` mode carries prompt or tool text.

**The switch row** (`claudlobby/switches.py`, beside `mcp-direct-launch` `:604-626`, the restart-gated
`COMPOSE_BOT` template):

```python
    Switch(
        key="telemetry",
        scope=GENERATE,
        polarity=OPT_IN,
        carrier=COMPOSE_BOT,
        config="telemetry.enabled",          # _bot_config_value walks this path; the leaf must be a bool (:853-860)
        why_opt_in="spends host resources (a resident Collector) and, with content: full, records "
                   "prompt and tool text — the operator decides per bot, one canary first (#2145 F4)",
        what="export the bot's native OpenTelemetry metrics and events to the host's local Collector "
             "(127.0.0.1 only; raw files under state/otel/, an allowlist into the plane)",
        compose_steps=("config plan --release RELEASE_ID, config diff PLAN_ID, host activate PLAN_ID",),
        takes_effect=("it takes effect when that bot next restarts: bot.conf is read at session start",),
        takes_effect_off="exports stop at the bot's next restart",
    )
```

`tests/test_switches.py:98-180` (the opt-in allowlist) gains `"telemetry"` and `"otel-collector"` with
their reasons; `claudlobby host doctor --switches --markdown` regenerates the three pinned tables
(`DOC_BLOCKS`, `switches.py:1143-1147`; pinned by `:957-969`).

**What the composer writes** (`compose_bot_conf`, `claudlobby/composer.py:977`; the block goes beside
`# Observability`, `:1177-1225`, and follows its shell-boolean rule — `'1'`/`'0'`, never `_shq(bool)`),
only when `bot.telemetry.enabled` and only for `runtime: claude` (a Codex bot's `[otel]` block is the
companion's, F11):

```bash
# Telemetry (#2145 F4) — read by the claude process at its next start (start-bot.sh sources bot.conf under set -a, :224-226)
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
export OTEL_RESOURCE_ATTRIBUTES="claudlobby.fleet=<fleet>,claudlobby.bot=bot:<fleet>/<bot>,claudlobby.content=metadata,agent.runtime=claude"
# content: full only — the disclosed act (design v2 §11); claudlobby.content=full above, and:
export OTEL_LOG_USER_PROMPTS=1
export OTEL_LOG_TOOL_DETAILS=1
```

`bot:<fleet>/<bot>` is the composed alias every bash door already stamps (design v2 §3: "ingest resolves
alias→uid against the registry at write time"), so no uid needs to be on the wire; Claude Code copies the
`OTEL_RESOURCE_ATTRIBUTES` keys onto every datapoint and event *(doc)*, and the Collector's transform keys
its content gates on `claudlobby.content` (the Collector-and-intake spec), so a bot composed in `metadata`
mode cannot leak prompt text into the plane even if a runtime update changed a default. The exact env names and the `http/json` protocol
value are vendor facts *(doc)* that C6 confirms; the composer test pins whatever C6 records. The
validator's `RC_KILLING_ENV_VARS` check (`validator.py:1302-1319`) is unaffected: `CLAUDE_CODE_ENABLE_TELEMETRY`
is the opposite switch from `DISABLE_TELEMETRY` (`known_values.py:115-118`).

**`system.yaml` — the Collector and the intake are two resident host services**, dormant unless
`enroll` is exactly `true` (`composer.py:5291-5306`, `switches.py:893-898`):

```yaml
    otel-collector:                 # #2145 F4 — the host's local OpenTelemetry Collector; dormant until armed
      enroll: false                 # arm in ~/.config/claudlobby/system.yaml (host jobs bypass the fleet merge)
      unit: service
      script: "$CLAUDLOBBY_NATIVE_DIR/otel-collector.sh --retention-days 14"
    plane-otel:                     # #2145 F3(b) — the OTLP/HTTP (JSON) intake that maps the allowlist into the plane
      enroll: false
      unit: service
      script: "$CLAUDLOBBY_NATIVE_DIR/plane-otel.sh --port 4319"
```

- *(Correction to F4's "`retention_days`"):* no structured per-job knob exists in Claudlobby; retention is
  carried on the job's `script` line (`plane-prune`: `… prune --days 90`, `system.yaml:88-91`,
  `plane-prune.sh:39`; `data-sweep`: `… --purge --days 60`, `:507-508`). The Collector follows that
  convention: `--retention-days N` renders into the file exporter's `rotation.max_days`. The host still
  owns retention, as F4 decides; only the spelling follows the repo.
- *(Correction to F4's "(like `plane-prune`)"):* `plane-prune` ships **on** (`polarity=OPT_OUT`,
  `switches.py:326-335`). The permanently-opt-in precedents are `plane-prune-system-events` (`:350-381`)
  and the `ENROLL_HOST` jobs `update-siblings` / `vault-sync` (`:448-472`).
- The `otel-collector` switch row: `Switch(key="otel-collector", scope=HOST_SERVICE, polarity=OPT_IN,
  carrier=ENROLL_HOST, job="otel-collector", why_opt_in="a resident process on the smallest host class, "
  "and its file rotation deletes raw telemetry", what="run the host's local OpenTelemetry Collector on "
  "127.0.0.1 (receives the bots' OTLP, writes rotated raw files under state/otel/, forwards an allowlist "
  "to plane-otel)")`. `plane-otel` rides the same row's arming recipe; `_validate_timers`
  (`validator.py:2094-2103` pattern) warns `job-inert` when exactly one of the two is enrolled.
- The binary: Claudlobby installs no third-party binaries (`commands/setup.py:85-88` refuses on a
  missing `tmux`/`claude`/`jq`; the one installer is the `claude-update` host job via `npm`,
  `update-claude-code.sh:400`). `otelcol-contrib` is **operator-installed** (Homebrew
  `opentelemetry-collector-contrib`, Debian package, or the upstream release tarball for
  `linux/arm64`); `host setup` adds `otelcol-contrib` to its refusal list **only when `otel-collector`
  is enrolled**, and `host doctor` gains a rung that names the resolved binary and its version. *(The
  epic's "installed by host setup" is corrected to this; it is not a fork.)*
- The launcher `_runtime_scripts/otel-collector.sh` is thin like `plane-daemon.sh:1-27`: sources
  `cli-context.sh`, requires the root, resolves `OTELCOL_BIN` (`$CLAUDLOBBY_OTELCOL_BIN`, else
  `otelcol-contrib` on PATH), and `exec`s it with `--config "$CLAUDLOBBY_ROOT/runtime/_host/otel/collector.yaml"`
  — a file the composer renders at compose time from the entry above (the Collector-config spec, next).
  `plane-otel.sh` execs `claudlobby --root "$CLAUDLOBBY_ROOT" plane otel-intake --host 127.0.0.1 --port 4319`.
- Documentation: `system-yaml-schema.md` roster (`:203-216`) and the `unit: service` section
  (`:223-250`, whose "only tenant" sentence is already stale — `plane-view` is the second); the
  dormancy table (`:322-332`); `fleet-yaml-schema.md` gains `### bots.<name>.telemetry /
  fleet.defaults.telemetry` in the paired-heading form of `heavy_slot` (`:547-564`), and
  `fleet.yaml.example:63`'s wrong comment (it claims `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` is
  emitted; `composer.py:1082-1090` deliberately does not) is fixed in the same PR;
  `environment-variables.md` "Ecosystem" table (`:181-187`) gains the `OTEL_*` /
  `CLAUDE_CODE_ENABLE_TELEMETRY` rows with source `bots.<name>.telemetry`.

**The `config plan` check** — a new rung in `_validate_bots` (`claudlobby/validator.py:629-634`), host
state probed once like `claudron_on_path` (`:642`), appended to `report.errors` so
`stage_configuration` raises `PlanError` (`config_staging.py:174-176`). The same `validate()` backs
`config validate` and `doctor`'s `check_fleet_validation` (`config_validation.py:124`, `doctor.py:1373`),
so all three refuse together — intended:

```python
    # #2145 F4: a bot exporting OTLP with no Collector enrolled would send to an unbound port.
    try:
        _otel = load_host_jobs().get("otel-collector") or {}
        otel_enrolled = _otel.get("enroll") is True          # the service rule, composer.py:5291-5306
        otel_error = None
    except RuntimeError as exc:                               # a malformed host override, config.py:2142-2191
        otel_enrolled, otel_error = False, str(exc)
    ...
        if bot.telemetry.enabled and not otel_enrolled:
            report.errors.append(
                f"bot '{bot_name}': telemetry.enabled is true but this host's otel-collector service is "
                f"not enrolled{f' ({otel_error})' if otel_error else ''} — the bot would export OTLP to an "
                f"unbound port. Set host.jobs.otel-collector.enroll: true in ~/.config/claudlobby/system.yaml "
                f"and plan again, or set telemetry.enabled: false."
            )
```

`validator.py` reads `load_host_jobs()` nowhere today; this is its first host-enrollment read, which is
why the `RuntimeError` branch exists. Tests: a fleet with one enabled bot fails `validate()` with the
message above when the host override is absent, passes when it enrolls the Collector, and names the
override error when the override is malformed; `config plan` on the same fixtures raises `PlanError`
exactly when `validate()` errors.

#### Spec: the Collector configuration (F5) and the `plane-otel` intake allowlist (F3)

*Added by `/claudna:forge`, 2026-10-04. Vendor facts are from the documentation and source read on
2026-10-04 (recorded in the run log's evidence); everything marked *(doc)* is confirmed by C5/C6 before
P2 depends on it. Claudlobby citations are at `cd292cb`.*

**Three things the fork context did not have.**

1. The GenAI semantic conventions moved to their own repository, carry no release tag yet ("Schema URL:
   TODO"), and every `gen_ai.*` attribute is `Development`. `gen_ai.system` is deprecated in favour of
   `gen_ai.provider.name`; the token metric the plan's §2.4 implied (`gen_ai.client.token.usage`) no longer
   exists (replaced by `gen_ai.client.inference.usage.*` counters). F5 stands — the *attribute names* are
   normalized onto semconv — but the Collector config **pins the semconv commit it was written against** in
   a header comment, and the plane's own registry names (below) are the stable surface readers use.
2. **Semconv input tokens include cached tokens; Claude Code's do not.** `gen_ai.usage.input_tokens` "SHOULD
   include all types of input tokens, including cached tokens"; `claude_code.api_request.input_tokens` and
   `claude_code.token.usage{type="input"}` exclude "tokens read from or written to the prompt cache", while
   Codex's `input_token_count` follows OpenAI's inclusive convention. The Collector produces the semconv
   value for Claude by addition; the plane stores **uncached input** separately so both runtimes compare.
3. **Codex's default metrics exporter is hosted.** `[otel].metrics_exporter` defaults to `statsig`, which
   resolves to `https://ab.chatgpt.com/otlp/v1/metrics` *(source)*; keeping telemetry local requires
   `metrics_exporter = "none"` (or the local Collector) **and** `[analytics] enabled = false`. This is a
   companion-epic composition fact (F11) and a §11 "what not to do" line.

**The Collector.** `otelcol-contrib` (the core `otelcol` distribution ships no `transform` processor;
contrib v0.162.0 is 102 MB for `linux_arm64`, 97 MB for `darwin_arm64`). Rendered by the composer to
`runtime/_host/otel/collector.yaml` from the `system.yaml` entry (F4 spec), validated in tests with
`otelcol-contrib validate --config=<file>`:

```yaml
# Rendered by claudlobby from host.jobs.otel-collector — do not hand-edit. Semconv pin: <commit>.
receivers:
  otlp:
    protocols:
      grpc: { endpoint: 127.0.0.1:4317 }
      http: { endpoint: 127.0.0.1:4318 }
processors:
  memory_limiter: { check_interval: 1s, limit_mib: 96, spike_limit_mib: 24 }   # first in every pipeline; the C6 budget
  transform/normalize:
    error_mode: ignore
    log_statements:
      - context: log
        statements:
          # the runtime: Claude bots carry agent.runtime in OTEL_RESOURCE_ATTRIBUTES; Codex is recognised by its originator
          - set(resource.attributes["agent.runtime"], "codex") where resource.attributes["agent.runtime"] == nil and resource.attributes["service.name"] == "codex_cli_rs"
          - set(resource.attributes["gen_ai.provider.name"], "anthropic") where resource.attributes["agent.runtime"] == "claude"
          - set(resource.attributes["gen_ai.provider.name"], "openai") where resource.attributes["agent.runtime"] == "codex"
          # the session (F5): one attribute for both ids
          - set(attributes["gen_ai.conversation.id"], attributes["session.id"]) where attributes["session.id"] != nil
          - set(attributes["gen_ai.conversation.id"], attributes["conversation.id"]) where attributes["conversation.id"] != nil
          # model and tools
          - set(attributes["gen_ai.request.model"], attributes["model"]) where attributes["model"] != nil
          - set(attributes["gen_ai.tool.name"], attributes["tool_name"]) where attributes["tool_name"] != nil
          - set(attributes["gen_ai.tool.call.id"], attributes["tool_use_id"]) where attributes["tool_use_id"] != nil
          - set(attributes["gen_ai.tool.call.id"], attributes["call_id"]) where attributes["call_id"] != nil
          # tokens on Claude api_request (exclusive input → semconv inclusive input)
          - set(attributes["gen_ai.usage.cache_read.input_tokens"], Int(attributes["cache_read_tokens"])) where attributes["cache_read_tokens"] != nil
          - set(attributes["gen_ai.usage.cache_write.input_tokens"], Int(attributes["cache_creation_tokens"])) where attributes["cache_creation_tokens"] != nil
          - set(attributes["gen_ai.usage.output_tokens"], Int(attributes["output_tokens"])) where attributes["output_tokens"] != nil
          - set(attributes["gen_ai.usage.input_tokens"], Int(attributes["input_tokens"]) + Int(attributes["cache_read_tokens"]) + Int(attributes["cache_creation_tokens"])) where attributes["input_tokens"] != nil and attributes["cache_read_tokens"] != nil
          # tokens on Codex sse_event response.completed (inclusive input already)
          - set(attributes["gen_ai.usage.input_tokens"], Int(attributes["input_token_count"])) where attributes["input_token_count"] != nil
          - set(attributes["gen_ai.usage.output_tokens"], Int(attributes["output_token_count"])) where attributes["output_token_count"] != nil
          - set(attributes["gen_ai.usage.cache_read.input_tokens"], Int(attributes["cached_token_count"])) where attributes["cached_token_count"] != nil
          - set(attributes["gen_ai.usage.cache_write.input_tokens"], Int(attributes["cache_write_token_count"])) where attributes["cache_write_token_count"] != nil
          # cost has no convention: a house attribute
          - set(attributes["claudlobby.cost_usd"], Double(attributes["cost_usd"])) where attributes["cost_usd"] != nil
          - set(attributes["claudlobby.cost_usd"], Double(attributes["usage.estimated_usd"])) where attributes["usage.estimated_usd"] != nil
          # content gates, belt and braces: nothing content-bearing passes unless the bot was composed with content: full
          - delete_key(attributes, "prompt") where resource.attributes["claudlobby.content"] != "full"
          - delete_key(attributes, "prompt_text") where resource.attributes["claudlobby.content"] != "full"
          - delete_key(attributes, "response") where resource.attributes["claudlobby.content"] != "full"
          - delete_key(attributes, "tool_input") where resource.attributes["claudlobby.content"] != "full"
          - delete_key(attributes, "tool_parameters") where resource.attributes["claudlobby.content"] != "full"
          - delete_key(attributes, "arguments") where resource.attributes["claudlobby.content"] != "full"
          - delete_key(attributes, "output") where resource.attributes["claudlobby.content"] != "full"
          - delete_key(attributes, "body") where resource.attributes["claudlobby.content"] != "full"
    metric_statements:
      - context: datapoint
        statements:
          - set(resource.attributes["agent.runtime"], "codex") where resource.attributes["agent.runtime"] == nil and resource.attributes["service.name"] == "codex_cli_rs"
          - set(attributes["gen_ai.conversation.id"], attributes["session.id"]) where attributes["session.id"] != nil
          - set(attributes["gen_ai.conversation.id"], attributes["conversation.id"]) where attributes["conversation.id"] != nil
          - set(attributes["gen_ai.request.model"], attributes["model"]) where attributes["model"] != nil
  batch: { timeout: 5s, send_batch_size: 512 }
exporters:
  file/logs:
    path: <CLAUDLOBBY_ROOT>/state/otel/logs.jsonl
    format: json
    create_directory: true
    rotation: { max_megabytes: 64, max_days: <retention-days>, max_backups: 30, localtime: false }
  file/metrics:
    path: <CLAUDLOBBY_ROOT>/state/otel/metrics.jsonl
    format: json
    create_directory: true
    rotation: { max_megabytes: 64, max_days: <retention-days>, max_backups: 30, localtime: false }
  otlphttp/plane:
    endpoint: http://127.0.0.1:4319        # plane-otel; /v1/logs and /v1/metrics are appended by the exporter
    encoding: json
    compression: none
    timeout: 5s
    retry_on_failure: { enabled: true, max_elapsed_time: 30s }
    sending_queue: { enabled: true, queue_size: 256 }    # bounded; beyond it the plane copy is dropped, the file copy is not
service:
  telemetry: { metrics: { level: none } }                # the Collector's own metrics stay off: no :8888 listener
  pipelines:
    logs:    { receivers: [otlp], processors: [memory_limiter, transform/normalize, batch], exporters: [file/logs, otlphttp/plane] }
    metrics: { receivers: [otlp], processors: [memory_limiter, transform/normalize, batch], exporters: [file/metrics, otlphttp/plane] }
```

- Metric **names** stay vendor-prefixed in the raw files (`claude_code.cost.usage`, `codex.turn.token_usage`,
  …): F5 normalizes attributes, and re-shaping Claude's single `token.usage{type=…}` counter into the
  semconv counter family is aggregation the intake does anyway. No traces pipeline: traces are beta on
  Claude Code and undocumented on Codex (§2.3), and neither bot is composed to export them.
- `claudlobby.content` joins `agent.runtime`, `claudlobby.fleet` and `claudlobby.bot` in the Claude bot's
  `OTEL_RESOURCE_ATTRIBUTES` (F4 spec); Claude Code also copies these keys onto every datapoint and event
  *(doc)*, which is what lets the intake attribute a session to its bot. For a Codex bot the companion's
  lean is `[otel] environment = "bot:<fleet>/<bot>"` (a free string stamped as `env` on every event
  *(doc)*), checked by C5; until then Codex sessions carry no bot and the intake records them with
  `fleet = "_host"` and no parent.
- The `file` exporter is alpha; its rotation keys are the retention knob (`--retention-days N` →
  `max_days`). `otlphttp` and the `otlp` receiver are stable. The raw files are the durable record and
  the only place full event detail lives; the plane never holds a copy.
- Budget for the one-host P2 canary (C6 records the actuals; v2's decision-framework ruling: hardware
  informs budgets, never model shape): Collector RSS ≤ 192 MiB and ≤ 3 % of one core, averaged over the
  week on the smallest host class; the intake ≤ 64 MiB. A miss opens a defect investigation, not a
  schema change.

**The `plane-otel` intake** (`claudlobby plane otel-intake --host 127.0.0.1 --port 4319`, launched by
`_runtime_scripts/plane-otel.sh`; stdlib only — `http.server` + `json`, no protobuf, per §11). It accepts
`POST /v1/logs` and `/v1/metrics` with OTLP/JSON bodies (`resourceLogs[].scopeLogs[].logRecords[]`,
`resourceMetrics[].scopeMetrics[].metrics[].sum.dataPoints[]`), always answers `200` (it never back-pressures
a bot's exporter), and keys everything on `(agent.runtime, gen_ai.conversation.id)` from the normalized
attributes. It is a **translator, not a writer**: it posts `{"events": […]}` batches to the daemon's
socket (`<root>/state/plane/ingest.sock`, `daemon.py:207-208`; protocol `:16-24`), so the daemon's held
`PlaneWriter` stays the plane's single recorder; a socket miss drops the batch and increments a counter
in the intake's own log (the raw files already hold the data; staging would duplicate the Collector's queue).

*The allowlist — everything else never reaches the plane.*

| Plane name | Kind | Value | Claude source *(doc)* | Codex source *(doc/source)* |
|---|---|---|---|---|
| `session.cost_usd` | metric | number (USD, **delta per window**) | `claude_code.cost.usage` datapoints | `codex.turn.cost_microusd` / 1e6, else `codex.turn_cost.usage.estimated_usd` |
| `session.tokens` | metric | `{input, output, cache_read, cache_write}` (delta; `input` is **uncached**) | `claude_code.token.usage` by `type`: `input`→input, `output`→output, `cacheRead`→cache_read, `cacheCreation`→cache_write | `codex.turn.token_usage` by `token_type`: `input − cached_input`→input, `cached_input`→cache_read, `output`→output; cache_write from `sse_event.cache_write_token_count` |
| `session.tool_calls` | metric | `{calls, failures}` (delta) | count of `tool_result` events; `success == "false"` → failures | count of `codex.tool_result` events; `success == false` |
| `session.api_requests` | metric | `{requests, errors}` (delta) | `api_request` events; `api_error` + `api_retries_exhausted` → errors | `codex.api_request` events; `error.message != nil` or `http.response.status_code ≥ 400` → errors |
| `session.active_time_s` | metric | seconds (delta) | `claude_code.active_time.total` | — (absent) |
| `api_error` | system event, `notice` | `data.data = {runtime, session_id, session_uid, model, status_code, attempt, exhausted, count}` | one per `api_error` / `api_retries_exhausted` (`exhausted: true`), debounced to one event per session per 5-minute window with `count` | `codex.api_request` with an error |
| `tool_failure_streak` | system event, `notice` | `data.data = {runtime, session_id, session_uid, tool, count}` | ≥ 3 consecutive `tool_result success=="false"` for one `gen_ai.tool.name` in a session; reset on success | same, from `codex.tool_result` |

- **Window.** Metric samples are emitted once per `(runtime, session)` per 60-second window (Claude's
  metric export interval *(doc)*; Codex's exporter batches asynchronously), carrying the window's **delta**
  (Claude's default temporality is delta *(doc)*; the intake sums datapoints and event counts inside the
  window). "What did session X cost" is `SUM(value)` over its samples — the intake keeps no cross-window
  state, so a restart loses at most one window and never double-counts. Each `METRIC_NAMES` description
  says so (`registries.py:296-337` shape `name → {unit, description}`; object values follow the
  `host.swap_pages` precedent).
- **Subject and identity (the derive-not-mint rule).** Samples are `metric_sample` requests with
  `subject_kind = "session"` (legal everywhere: `contracts.py:86,732-733`, `samples.py:24`, the DDL
  CHECKs) and `subject = <F2 input>` — the raw id for `claude`, `"codex:" + id` otherwise. Today
  `MetricSample` has only the alias form and `identity.resolve` mints a **random** uid on first sight
  (`identity.py:36`, via `ingest._batch_resolver.entity`, `ingest.py:136-149`), which would break the
  §2.2 join. P2 adds one rule to `_batch_resolver.entity`: for `kind == "session"`, the uid is
  `"sess_" + sha256(alias)[:32]` — by construction `derive_session_uid(id, runtime)` (`ids.py:79-89`
  extended per F2) — inserted `INSERT OR IGNORE` with `provisional = 0` and `parent_uid` = the bot's
  actor uid when `claudlobby.bot` is present. `MetricSample` has no slot to name that parent, so the P2
  plan adds an additive `subject_parent` alias, legal only for `session` subjects and declared in
  `migration_plan.py`'s `wire_additions` (the `waiting_on` precedent). `samples.py:35-43` infers the kind from the `session.`
  prefix as it does for `host.`. The `session_uid` the P1 doors write on task events and the
  `subject_uid` on these samples are then the same string.
- **Row shape for the two system events:** the fleet-event shape (`source_ref = "fleet-events:otel:<session_uid>:<window>"`,
  `data = {"source": "plane-otel", "legacy_ts": …, "data": {…}}`), subject `actor bot:<fleet>/<bot>` when the
  bot is known and the anchor pair `session`/`sess_…` otherwise, so `claudlobby event list --type api_error`
  renders them (`plane-readers.py:1155-1164,1226-1230`). `event_id` is derived (`derive_uid("ev", …)`
  over runtime, session, window and type) so a replayed Collector queue is a `duplicate`, not a second row.
- **Registry and docs.** `METRIC_NAMES` gains the five names; `SYSTEM_EVENT_SEVERITY` gains `api_error`
  and `tool_failure_streak` at `notice` (the rule at `registries.py:100-104`; `critical` would also
  require fleet-pulse's lists and `tests/test_service_is_crash_looping.py` to agree). `tests/test_event_type_registry.py`
  gate (d) binds the protocol/guide tables to the registry, so `library/protocols/fleet-observability.md`
  and `documentation/guides/observability.md` gain the two rows in the same PR. `observable-plane.md`'s
  doors table (`:200-220`) gains the `plane-otel` row and its "Where it lives" table (`:18-26`) gains
  `state/otel/` and port 4319; `environment-variables.md` documents nothing new (the intake reads no env).
- **Tests.** OTLP/JSON fixtures recorded from the C5/C6 canaries (Claudlobby's rule: a fixture for an
  externally produced shape comes from a live capture, never the producer's source; identifiers scrubbed):
  one window of a Claude session and one of a Codex session produce exactly the allowlisted samples with
  the expected deltas; a `tool_result` streak produces one `tool_failure_streak`; an `api_error` burst
  produces one debounced event; a Codex `input/cached_input` pair produces uncached `input`; a payload
  with prompt text in `metadata` mode reaches the plane with no content field; a socket miss drops and
  counts; `derive_session_uid("abc", "codex") == entity("session", "codex:abc")`; an unknown metric name
  in the payload never reaches `emit`.

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

  For each exported segment, including F6's status-only skipped items, it emits the F6 event (deterministic
  `event_id`, so emit-then-ack is safe to re-run) and then `--ack`s. The export item also needs the
  segment's `sealed_at`/`sealed_by`/`counts` (additive, the clauDNA P3 PR).
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
- [ ] The store's own SessionStart handling (`boundaries._session_start`, not the briefing hook) writes
  `<CLAUDNA_STATE_DIR>/entrypoint.json` (F16): the absolute path of the store entrypoint for the running
  plugin version. Spec §8; tested.
- [ ] Freeze the activity layer per F8, with a spec note.
- [ ] Child guard reads `CLAUDE_CODE_CHILD_SESSION=1` first, keeping the pid and entrypoint checks as fallbacks. This step waits on C10. Update the `SETUP_GUIDE.md` env table (`:309-317`) if any documented semantics change.
- [ ] Release clauDNA.

#### Spec: the clauDNA export contract additions (`--include-skipped`, `runtime`, `entrypoint.json`)

*Added by `/claudna:forge`, 2026-10-04. Grounded in clauDNA v0.26.0 (`71f983d`). These are spec §8
contract changes (Claudron register rule R3 applies: Claudlobby conforms to this text).*

**Item fields (naming what §8 never named).** `claudna.export/1` items are
`{"sid": str, "seg": int, "session": {...}, "summary": {...}}` (`export.py:124-128`); `session` is the
`session.json` subset `SESSION_FIELDS = ("sid", "status", "opened_at", "closed_at", "close_reason",
"chain_id", "parent_sid", "actor", "origin")` (`:46-47`), taken from the first `session.opened`
(`project.py:364-382`). P1 adds `"runtime"` to `SESSION_FIELDS` once `session.json` carries it
(`claudna.session/2`); absent means `claude`. The envelope keeps `schema: "claudna.export/1"` — both
changes are additive and a consumer that ignores unknown keys is unaffected.

**`--include-skipped` (F6).** Today the cursor advances past three kinds of segment with no item: a
skipped summary (`export.py:113-114`, reason in `summary.skipped.reason`), a retired segment with no
archived summary (`:108-109`), and a settled give-up (`:115-116`); private sessions never export at all
(`:98-99`). With the flag, each of those emits a *status item*:

```json
{"sid": "…", "seg": 3, "session": {…}, "summary": null,
 "skipped": {"reason": "disabled|trivial|headless|no_transcript|gave_up|retired"}}
```

`reason` is the `summary.skipped.reason` value (`events.py:174`, minus `private`), or `gave_up`, or
`retired`. Without the flag the envelope is byte-identical to today's. The flag attaches to the `export`
subparser (`cli.py:364-372`) and threads as `export(store, consumer, *, since_seg=None, limit=100,
now=None, include_skipped=False)` (`export.py:81-82`); `--ack` semantics are unchanged (`:135-149`:
`through` never moves back, `store.py:385`). The status items are what carries the fleet monitor's
coverage concern (spec §1.1 rule 4) once `transcript-digest.sh`'s `status: skipped` rows stop (P3).

**`entrypoint.json` (F16).** Written by the store's own SessionStart handling — `boundaries._session_start`
(`boundaries.py:138-162`) on every opening `SessionStart` — not by the briefing hook `session-start.sh`,
which bots turn off (`CLAUDNA_SESSION_BRIEFING=0`, `session-start.sh:26-29`) and which fires only for
`startup|clear` (`hooks.json:79-88`). The store hook has no matcher (`hooks.json:89-96`) and is gated by
`CLAUDNA_SESSION_STORE` — the switch that also decides whether there is anything to export.

```json
{"schema": "claudna.entrypoint/1",
 "entrypoint": "/abs/path/to/lib/claudna/session_store",
 "plugin_root": "/abs/path/to/plugin",
 "plugin_version": "0.27.0",
 "python": "/usr/bin/python3",
 "host": "claude",
 "written_at": "2026-10-04T12:00:00Z"}
```

- Path `<CLAUDNA_STATE_DIR>/entrypoint.json` (`paths.state_root`, `paths.py:51-58`), mode 0600, written
  by atomic replace; rewritten on each opening SessionStart (one small write).
- `entrypoint` is `str(Path(__file__).resolve().parent)` — exactly the package path `spawn_worker` already
  execs as `[sys.executable, "-S", str(package), …]` (`boundaries.py:174-177`); `plugin_root` is
  `package.parents[2]` (cf. `__main__.py:16`); `plugin_version` is read from
  `<plugin_root>/.claude-plugin/plugin.json` (the first time `lib/` reads that file; a missing or
  unparsable manifest leaves the key `null`, never an error); `host` is the selected host's name.
- The consumer invocation is `python3 -S <entrypoint> export --consumer <name> [--include-skipped] --json`
  and `… --ack --sid <sid> --through <seg>`. A consumer that finds `entrypoint` missing on disk treats the
  record as **stale** (a plugin version replaced underneath a running session, `reload-fleet.sh`) and
  skips the bot until the next SessionStart rewrites the file; it never guesses another path.
- Spec §8 gains this record as the second contract surface beside the export envelope; SETUP_GUIDE's env
  table (`:309-317`) gains no row (nothing to set).

#### Spec: the `session_summary` plane event (F6)

*Added by `/claudna:forge`, 2026-10-04. Grounded in Claudlobby `cd292cb` and clauDNA v0.26.0. Implements
F6(b) as ratified. Two facts the fork's context did not have are recorded here first, because they shape
the payload.*

**What the digest's consumers can actually read today.** The four F6 consumers reach digest rows through
one door, `claudlobby event list --type session_digest` (`fleet-digest/SKILL.md:58-69`;
`fleet-monitoring.md:98`), which `plane-readers.py`'s `fleet_events` serves (`:1238-1280`). That reader
(1) filters `e.source_ref LIKE 'fleet-events:%'` (`:1155-1164,1250`), while `transcript-digest.sh` stamps
`source_ref = "session-digest:<sid>"` (`:340-342`); and (2) projects `data` from the nested
`detail.data` that `emit_fleet_event` writes (`legacy_event_row`, `:1226-1230`; `lib-common.sh:2163-2165`),
while the digest writes a flat `data`. So a `session_digest` row never reaches `fleet-digest` today, and
would render as `data: {}` if it did. Neither side is tested (`tests/test_transcript_digest.sh` asserts on
the staged payload only). Consequence: the replacement event is shaped for that reader, and P3's "repoint
the four consumers" is a re-pointing to a door that works for the first time.

**A `system` row cannot carry the session in its stream column.** `KIND_MANIFEST` allows `session_uid`
only on `kind=task` (`claudlobby/plane/contracts.py:112-116,124-141`; DDL `0011:150`), so the join key
rides inside `data` (queryable with `json_extract`) and the row's subject stays the **actor**
`bot:<fleet>/<bot>` — which is also what gives the consumers their `bot` column. The anchor pair
`subject_kind="session", subject_uid=sess_…` is legal (`contracts.py:464-465`) but would cost the
consumers `bot`; the session-anchored rows are P2's metric samples, not this event.

**The event.**

| Envelope / row | Value |
|---|---|
| `event_type` | `system`; `payload.event = "session_summary"` |
| `SYSTEM_EVENT_SEVERITY["session_summary"]` | `"notice"` (the registry rule, `registries.py:100-104`: "recorded directly by a writer, notice"); `session_digest` stays registered at `:204` so history classifies (the `shadow_parity_*` precedent, `:114-115`) |
| `emitter` | `session-export` (the P3 fleet job) |
| `source_ref` | `fleet-events:session-summary:<sid>/<seg>` — the `fleet-events:` prefix is what the reader selects on |
| `event_id` | `derive_uid("ev", f"session_summary:{fleet}:{sid}:{seg}")` (`claudlobby/plane/ids.py:57`) — deterministic, so a job run that re-emits before its `--ack` landed is a `duplicate` success, never a second row |
| `fleet` | the bot's fleet (alias) |
| `payload.subject_kind` / `payload.subject` | `actor` / `bot:<fleet>/<bot>` (alias form, resolved at ingest, `ingest.py:313-322`) |
| `payload.data` | `{"source": "session-export", "legacy_ts": <occurred_at>, "data": {<summary, below>}}` — the nested shape `legacy_event_row` projects |
| `occurred_at` | the segment's `sealed_at` (the fact's time), `observed_at` the export time |

**`data.data` — the summary record** (every field bounded by the emitter; the whole `data` object stays
under 12 KiB so ingest's 16 KiB DIAGNOSTIC cap, `registries.py:72`, never truncates it into an unparseable
prefix):

```json
{"schema": "session_summary/1",
 "status": "ok",                                 "skipped_reason": null,
 "runtime": "claude",                            "session_id": "<raw id>",   "session_uid": "sess_<32 hex>",
 "seg": 3,                                       "sealed_at": "…",           "sealed_by": "precompact",
 "opened_at": "…",  "closed_at": "…",  "close_reason": "clear",   "actor_kind": "bot",
 "turns": 41,  "transcript_bytes": 183004,
 "prompts": 7,  "skills": 2,  "failures": 1,  "interrupts": 0,
 "journey": {"title": "…", "intent": "…", "outcome": "…", "arc": "<≤ 2,000 chars>",
             "done": ["…"], "in_progress": ["…"], "next": ["…"]},
 "blocks": {"count": 4, "kinds": {"entity": 2, "decision": 1, "practice": 1}},
 "procedures": 1,
 "producer": {"model": "haiku", "prompt_version": "segment-summary/2", "duration_ms": 18400, "cost_usd": 0.021}}
```

Sources (clauDNA): `status`/`skipped_reason` from the export item (`summary` present → `ok`; a status item
→ `skipped` with its `skipped.reason`, see the export-contract spec); `runtime`, `session_id` (= `sid`),
`opened_at`, `closed_at`, `close_reason` and `actor_kind` (`actor.kind`) from the item's `session` subset
(`export.py:46-47`); `session_uid = derive_session_uid(session_id, runtime)` computed by the job (F2);
`turns` from `summary.input.turns`, `transcript_bytes = input.range.end - input.range.start`
(`claudna.segment-summary/1`: `input {transcript_path, range{start,end}, sha256, turns}`);
`sealed_at`/`sealed_by` and `prompts`/`skills`/`failures`/`interrupts` from the segment projection
(`claudna.segment/2` `counts`) — **the export item does not carry them today**; P3's clauDNA PR adds a
`segment: {sealed_at, sealed_by, counts}` object to the item (additive, like `runtime`); `journey`,
`blocks` (count and per-kind tally only — the block text is Claudron's through harvest, never the plane's),
`procedures` (count) and `producer` from the summary document. A skipped item carries the identity, status,
volume and segment fields with `journey`/`blocks`/`procedures`/`producer` as `null`.

**Re-pointing the four consumers** (`fleet-digest/SKILL.md`, `fleet-observe/SKILL.md`,
`fleet-monitoring.md:102-115`, `ai-platform-monitor.md:22`), field by field:

| Digest field the consumers read | `session_summary` field |
|---|---|
| `status` (`ok · skipped · error`) | `status` (`ok · skipped`; a summarizer that gave up is `skipped` with `skipped_reason: gave_up` — there is no separate `error` class) |
| `session_id`, `bot`, `fleet`, `ts` | `session_id` (+ `session_uid`, `runtime`); `bot`/`fleet`/`ts` on the row as before |
| `turns`, `transcript_bytes` | same names |
| `tool_calls` | **gone** (clauDNA records no generic tool-call event, spec §1.1 rule 1); `failures` is the tool-failure count; per-session tool totals are P2's `session.tool_calls` metric sample |
| `digest_chars`, `model` | `producer.duration_ms` / `producer.cost_usd` / `producer.model` |
| `context` | `journey.title` + `journey.intent` |
| `worked` | `journey.done` (+ `journey.outcome`) |
| `failed` | `journey.outcome` when not `completed`, with `failures` |
| `would_change` | `journey.next` (the nearest field; the digest's "what the operator would change" has no direct successor — the monitor's prompt asks for it from `next` + `in_progress`) |
| `reusable` | `blocks.count` / `blocks.kinds` (what harvest will offer Claudron) |
| `error` | `skipped_reason` |

`fleet-monitoring.md`'s "Digest row contract" block (`:102-115`) is rewritten to this table; `fleet-digest`'s
jq (`:64,98-133`) and output template (`:187-192`) follow; `observable-plane.md:214`'s doors-table row and
`_runtime_scripts/CLAUDE.md:69`/`AGENTS.md:69` index rows move from `transcript-digest.sh` to the
`session-export` job. The registry gate's document list (`tests/test_event_type_registry.py:404-408`) does
not include `fleet-monitoring.md`; what binds the protocol rewrite to the registry entry is
`tests/test_no_retired_digest_reference.py` (once `session_digest` joins its tokens) and the Python-writer
positive control at `:323`, so both land in one PR (P3 Claudlobby).

**Idempotency and ack.** The job emits one event per exported item, then `--ack --sid <sid> --through
<seg>` (`export.py:135-149`; the cursor never moves back, `store.py:385`). The deterministic `event_id`
makes the emit-then-ack pair safe to re-run: a crash between them re-emits a `duplicate` and acks. Emits go
through `emit_batch` in-process (`claudlobby/plane/emit_api.py:149-153`, the Python-door precedent), one
batch per bot per run, `require_commit=False` so a daemon outage spools rather than fails the job.

### P4 — Owner-side runtime adapters (fixture-tested; composed in the companion)

clauDNA:
- [ ] Split `boundaries.py` into the neutral handler plus flat host modules `host_claude.py` and `host_codex.py`. They cover:
  - payload normalization;
  - owner pid (C2, F13);
  - actor kind from the entrypoint;
  - the child guard;
  - the clear-link source.

  The host is selected by the hook command (C9). Add the new modules to `SESSION_STORE_LAYERS` and to
  `lib/CLAUDE.md` rule 2. The Python 3.9 floor holds. The split also covers `activity.py`'s payload reads
  (`:26-29,93-133`), the `events.py` `source`/`reason`/`trigger` vocabularies (`:97,117,142`), the walk
  predicate (`lineage.py:65`) and the transcript prefilter (`transcript.py:86-88`) — the Claude knowledge
  outside `boundaries.py`.
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

  If C3 shows Codex cannot inject or block, R-capture-prompt is documented as not held on Codex; if C3's
  Claude leg shows a PreCompact block never reaches an unattended model (Claudron#179), the prompt moves to
  `SessionStart(compact)` stdout for both hosts — a contract change that PRs Claudron first (R4). The parity
  test reads the first fenced block of a `##` section (`tests/test_hooks.py:246-257`), so the Codex snippet
  gets its own `##` section in `docs/CLI_CONTRACT.md`.
- [ ] Release Claudron. The companion bumps Claudlobby's pin again to compose the Codex snippet (R6).

Claudlobby (fixture-tested only; composition and canary in the companion):
- [ ] Payload adapters for the hook scripts so each reads normalized fields:
  - `plane-dispatch-in`;
  - the `bot-vitals` marker.

  Claude-specific parsers are listed for the companion: `plane-rc-relay-out.sh:62-100` (Claude transcript), and `plane-telegram-in/out` (Claude channel tags and the `mcp__plugin_telegram_telegram__reply` matcher).

#### Spec: clauDNA's host-adapter interface (`host_claude.py` / `host_codex.py`)

*Added by `/claudna:forge`, 2026-10-04. Grounded in clauDNA v0.26.0 (`71f983d`).*

**What exists.** `boundaries.handle(event, payload, *, store, env, spawn=None, find_pid=None) -> str`
(`lib/claudna/session_store/boundaries.py:317`) is already the neutral shell: it validates the id
(`paths._SID_RE`, `paths.py:32,46`), runs the child guard, and drives the store's verbs
(`open_session` `store.py:206-216`, `open_segment` `:242`, `seal_segment` `:289-296`, `close_session`
`:319`, `append` `:138`). The Claude Code knowledge is not in one module, whatever the docstring says
(`boundaries.py:3`): payload field names and `$CLAUDE_PID` (`boundaries.py:111-117,125-162,317-365`), the
entrypoint allowlist (`:49`) and `actor_from_env` (`:52-75`), the guard `inherited` (`:287-314`), the
activity payload fields (`activity.py:26-29,93-133`), the `"claude"` process-name walk (`lineage.py:65`),
the Claude JSONL reader (`transcript.py:50-95`, including the byte prefilter `:86-88`), and the
`source`/`reason`/`trigger` vocabularies (`events.py:97,117,142`). The summarizer runner (`summarize.py:73-84`)
stays as it is (F10).

**Modules.** Flat files, because the layering gate globs `session_store/*.py`
(`tests/test_runtime_layout.py:151`) and fails any module missing from `SESSION_STORE_LAYERS` (`:33-57,154`):

| Module | Rank | Holds |
|---|---|---|
| `host_claude.py` (new) | 6 | today's Claude pieces, moved: `actor_from_env`, `claude_pid_of`, `inherited`, the payload reads, the walk predicate, the `transcript.read_range` binding |
| `host_codex.py` (new) | 6 | the Codex pieces, written from the C1/C2/C4/C8/C9 fixtures |
| `boundaries.py` | 7 | the neutral handler only |

Both hosts import `project` (3, `SessionFacts`), `lineage` (2), `activity` (1), `transcript` (1); only
`boundaries` (7) and `cli` (8) import them — rank 6, beside `harvest`. `lib/CLAUDE.md` rule 2's prose
order gains `host_claude/host_codex` between `harvest` and `boundaries`. `boundaries` keeps re-exporting
`claude_pid_of` and `inherited` because `scripts/session_canary.py:44` imports them from there.

**The interface.** One frozen dataclass instance per host, so the handler never branches on a string:

```python
# host_claude.py — Python 3.9, stdlib only (lib/CLAUDE.md rule 1)
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

@dataclass(frozen=True)
class Normalized:
    """One hook payload in the store's vocabulary; every field Optional because hosts differ."""
    session_id: Optional[str]
    source: Optional[str]           # startup | clear | resume | fork   (session.opened choices, events.py:97)
    transcript_path: Optional[str]
    cwd: Optional[str]
    trigger: Optional[str]          # manual | auto                     (segment.sealed choices, events.py:142)
    reason: Optional[str]           # one of HOOK_CLOSE_REASONS, else "other" (events.py:117)
    activity: dict                  # the fields activity.event_for reads (activity.py:26-29), already renamed

@dataclass(frozen=True)
class Host:
    name: str                                                    # "claude" | "codex" — the session.opened.runtime value (P1)
    events: Mapping[str, Optional[str]]                          # host event name → store event, or None = ignore
    normalize: Callable[[str, dict], Normalized]                 # (host event, raw payload) → Normalized
    owner_pid: Callable[[Mapping[str, str]], Optional[int]]      # env → pid of the owning agent process (F13: stored as claude_pid)
    walk_matches: Callable[[str], bool]                          # process-name predicate for lineage.claude_pid's fallback walk
    actor: Callable[[Mapping[str, str]], dict]                   # env → actor; must satisfy actor_or_null (session.schema.json:55-67)
    inherited: Callable[[str, Normalized, "SessionFacts", Mapping[str, str]], bool]   # the nested-child guard
    read_range: Callable[[Path, int, Optional[int]], list]       # transcript reader (path, start, end) → list[Turn]

HOST = Host(name="claude", events={e: e for e in ("SessionStart", "PreCompact", "SessionEnd",
            "UserPromptSubmit", "PostToolUse", "PostToolUseFailure")}, ...)
```

Store events are the six `handle` accepts today (`boundaries.py:327`, `activity.EVENTS` `activity.py:143`).
`lineage.claude_pid(start=None)` (`lineage.py:52-70`) gains a `matches` predicate (default: today's
`"claude" in name`, `:65`); `transcript.Turn` (`:35-38`) is the shared output type.

**What the neutral handler does with a host**, in `boundaries.handle(event, payload, *, store, env,
host=host_claude.HOST, spawn=None, find_pid=None)`:

1. `store_event = host.events.get(event)`; `None` → `"ignored: event …"` (as `:327` does today).
2. `n = host.normalize(event, payload)`; then the id check `store.session(n.session_id)` (unchanged).
3. `host.inherited(store_event, n, facts, env)` → `"ignored: nested child …"` (unchanged text).
4. On an opening `SessionStart`: `handle.open_session(n.source, actor=host.actor(env),
   origin=origin_from_cwd(n.cwd or os.getcwd()), transcript_path=n.transcript_path,
   claude_pid=host.owner_pid(env), harvest=harvest_choice(env), runtime=host.name, parent_sid=…,
   chain_id=…)` (`store.py:206-216`; `runtime` is P1's additive kwarg). The clear-link `find_pid` default
   (`:343`) becomes `lambda: host.owner_pid(env) or lineage.claude_pid(matches=host.walk_matches)`.
5. `_seal` (`:247-254`) reads the transcript through `host.read_range`; `_record_activity` (`:257-275`)
   passes `n.activity` to `activity.event_for`.

The owner pid has three readers that all go through the recorded `claude_pid` (F13): the guard, the clear
link (`links/<pid>.json`, `lineage.py:26-27,73-97`) and the abandoned-session sweep (`unclosed.py:67,101-110`,
`os.kill(pid, 0)`). Whatever C2 finds for Codex must serve all three.

**Selection (C9): by the hook command, never by sniffing.**

- `plugin-hooks/session-store.sh` accepts `--host <name>` ahead of the event
  (`session-store.sh --host codex SessionStart`); the Claude manifest's entries stay as they are.
- `cli.main`'s hot path (`cli.py:316`, `argv[:1] == ["hook"] and len(argv) == 2`) also accepts
  `["hook", "--host", NAME, EVENT]`; the argparse fallback (`:391-392`) gains `--host`,
  `choices=("claude", "codex")`, default `"claude"`.
- `run_hook(event, raw, env=None, *, host="claude")` (`:177`) resolves `HOSTS[host]` and passes it to
  `handle`. An unknown host logs one `hooks/errors.log` line and returns `"ignored: unknown host …"`;
  the process still exits 0 (a hook never fails the session).

**The Codex host — what the P0 fixtures settle.** Everything below marked *(doc)* is vendor
documentation until C1–C4/C8 record it:

| `Host` field | Codex value | Settled by |
|---|---|---|
| `events` | `SessionStart`, `SessionEnd`, `PreCompact`, `UserPromptSubmit`, `PostToolUse`, `PostToolUseFailure` map to themselves; `PostCompact`, `Stop`, `SubagentStart`, `SubagentStop` → `None` (§2.3: enrichment only) *(doc)* | C1 |
| `normalize` | reads the C1 field names; Codex `SessionStart.source` `startup\|resume\|clear\|compact` *(doc)* maps one-to-one (Codex documents no `fork`); `reason`/`trigger` values are allowlisted against `events.py:117,142` or fall to `"other"`/`None` exactly as the Claude path does (`boundaries.py:353,357`) | C1 |
| `owner_pid` | the env var C2 finds, else `None` so the walk runs | C2 |
| `walk_matches` | `lambda name: "codex" in name` | C2 |
| `actor` | `kind` is `bot` when `BOT_ID` is set, `interactive` otherwise (`headless` only if C1/C2 expose a non-interactive signal, e.g. `codex exec`); `entrypoint` is `"codex"`; `fleet`/`bot_id`/`bot_name`/`model` from the same Claudlobby variables `actor_from_env` reads (`boundaries.py:61-72`). All six keys present (`actor_or_null`). | C1, C2 |
| `inherited` | the owner-pid check and the fresh-start check (`startup\|clear` naming a known session); the entrypoint check is moot (one entrypoint) | C1, C2 |
| `read_range` | the rollout reader over the C4 record types, replacing both `turn_of` and the byte prefilter | C4 |

A Codex session id must match `paths._SID_RE` (`[A-Za-z0-9][A-Za-z0-9._-]{0,127}`); C1 checks the format.

**Fixtures and tests.** Recorded C1 payloads (one per event, identifiers redacted) and a redacted rollout
excerpt (C4) under `tests/fixtures/codex/`; `tests/test_session_store_hook.py` is parametrized over
`(host_claude.HOST, host_codex.HOST)` with the fixture payloads, so every existing hook assertion runs once
per host; a layout test asserts both new modules are ranked. Claudlobby grounds its own Codex payload
adapters (P4) in the same C1 captures, per its "live capture, never the producer's source" rule.

## 7. Test plan

- Each repo's own check-set: clauDNA `make check`; Claudron `pytest`; Claudlobby against its documented baseline (`documentation/test-suite.md`), comparing failing names and counts.
- Parity gates:
  - `derive_session_uid`: Claude-only bash parity until the bash mirror retires in P3; the Codex case in `tests/test_plane_ids.py`;
  - Claudron's Codex snippet against Claudlobby's composer (companion);
  - the Collector config passes `otelcol validate`.
- Contract tests: clauDNA's live suite covers runtime-qualified provenance and export `runtime`/skipped items, and Claudron's consumers job runs it.
- Fixtures: a recorded Codex hook payload per event (C1) and a Codex rollout excerpt (C4), both redacted;
  one OTLP/JSON window per runtime from C5/C6 for the intake. Every externally produced shape is grounded in
  a live capture, never in the producer's source (Claudlobby's fixture rule).
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

### 10.1 Per-PR plans (forge, 2026-10-04)

One plan per PR beside this file, each with `epic:` pointing here. P4 and the companions get theirs when
§5.1's canary answers are in the run log.

| Order | Plan | Repo | Size |
|---|---|---|---|
| 1 | `2026-10-04-runtime-neutral-observability-p1-claudron-boundary-spec.md` | Claudron | S |
| 2 | `2026-10-04-runtime-neutral-observability-p1-claudlobby-runtime-and-join-key.md` | Claudlobby | M |
| 3 | `2026-10-04-runtime-neutral-observability-p1-claudna-runtime-at-open.md` | clauDNA | M |
| 4 | `2026-10-04-runtime-neutral-observability-p2-otel-pipeline.md` | Claudlobby | L |
| 5 | `2026-10-04-runtime-neutral-observability-p3-claudna-export-contract.md` | clauDNA | M |
| 6 | `2026-10-04-runtime-neutral-observability-p3-claudlobby-summaries.md` | Claudlobby | M → L (eight tasks; seam after the Claudron pin) |

Order 2 may split at its marked seam (runtime field and derivation first, the doors and receipts second)
if the first half is wanted on a bot before the receipt bump is reviewed. Plan 5 releases clauDNA before
plan 6 can consume it.

## 11. What NOT to do

- Don't add hosted export or LangChain-family dependencies.
- Don't put an OTel SDK in Claudron (single dependency) or clauDNA (stdlib-only `lib/`). Don't add protobuf to Claudlobby's intake; use JSON encoding.
- Don't build a spawn-lineage tree, and don't depend on any §2.3 signal for fleet behavior.
- Don't stop touching `.last-tool-call`, and don't remove `tool_call` from the prunable set.
- Don't nest `runtime` inside clauDNA's `actor`.
- Don't take a worker's session uid from the bot-global `.plane-session`; derive it from the caller's own session id with the one Python implementation.
- Don't compose or validate a Codex bot before the companion epic.
- Don't store raw prompts or tool bodies in telemetry by default.
- Don't leave a Codex bot's `[otel].metrics_exporter` at its default: `statsig` is a hosted endpoint. Set
  `none` or the local Collector, and `[analytics] enabled = false` (companion).
- Don't let the plane mint a random uid for a `session` subject; derive it (P2 spec) or the §2.2 join fails.
- Don't rely on ingest truncation for `session_summary`: a truncated `data` is an unparseable prefix. Bound
  the payload in the emitter.
- Don't put the `entrypoint.json` write in the briefing hook; bots turn that hook off.
- Don't rename the Collector's metric *names* onto semconv; normalize attributes, aggregate in the intake.

## 12. Context

This plan comes from a cross-repo review on 2026-10-03/04 covering:
- Claudron's `contract --json` and consumer CI (v0.9.0);
- clauDNA's vendored contract and floor legs (v0.26.0);
- buy-vs-build research on Claude Code and Codex telemetry;
- the Claudlobby plane map.

An adversarial review of the first draft corrected its evidence and sequencing.

## 13. Disposition

Draft. Next:
1. ~~The operator ratifies F1–F17.~~ Done, 2026-10-04.
2. ~~`/claudna:forge`~~ done 2026-10-04 (specs in §6, per-PR plans in §10.1, open questions in §14).
   `/claudna:ironclad` next.
3. P0 runs; sub-issues per phase per repo are opened from the epic.

## 14. Open questions (forge, 2026-10-04)

Questions the code could not answer; each has a lean, and the operator decides.

1. **The `fleet.yaml` key for the runtime.** `runtime:` (this plan) or `agent_cli:` (#1997, which documents the
   naming clash). Lean: `runtime:`, documented against `runtime/`; the cross-repo vocabulary is already in F2/F9.
2. **`PROJECT_MISSION.md:114`.** It excludes per-bot provider abstraction. Amend it in the P1 Claudlobby PR, or
   record that F11's ratification supersedes it? Lean: amend, one sentence, in that PR.
3. **Where the canaries run.** Codex is not installed on the operator's machine (the Homebrew cask `codex`
   is available). C1–C5 and C7–C9 need it; C6, C10 and C11 need only Claude Code.
4. **The P2 budget figures.** The Collector-and-intake spec proposes RSS ≤ 192 MiB and ≤ 3 % of a core for the
   Collector on the smallest host class; C6 replaces them with measurements before the one-week canary starts.
5. **Session metric semantics.** The plane stores *uncached* input tokens so both runtimes compare, which
   departs from semconv's inclusive `gen_ai.usage.input_tokens` (the Collector still emits the semconv value
   in the raw files). Confirm, or store the inclusive figure and derive the uncached one.
6. **`api_error` severity.** `notice` keeps it out of `event list --critical` and the brief; `critical` would
   page and would also have to join fleet-pulse's lists. Lean: `notice`, revisit on the P2 canary's volume.

## 15. Forge change log (2026-10-04)

- Wrote the six per-PR plans §10.1 indexes (P1 ×3, P2, P3 ×2); their authors corrected four spellings here
  (`derive_uid("ev", …)`; the registry gate's document list; the mixed-version writer behaviour of
  `claudna.session/2`; the additive `BotPayload.runtime` decision) — decisions unchanged.
- Added the six missing specs as `#### Spec:` subsections under §6: F17 receipts (P1); the `fleet.yaml`
  telemetry schema and `config plan` check, the Collector configuration and the intake allowlist (P2); the
  clauDNA export-contract additions and the `session_summary` event (P3); the host-adapter interface (P4).
- Added §4.1 (evidence found by forge), §5.1 (what P4 and the companions wait on), §10.1 (per-PR plan index),
  §14 and this section; refined canaries C1, C3, C5, C6, C8 and C11 in P0.
- Corrected plan *steps* that were factually wrong (P2: the Collector binary is operator-installed; retention
  is `--retention-days`; the intake posts to the daemon socket. P3: the `entrypoint.json` write site. §2.3:
  `$CLAUDE_PID` is documented. §2.4: the diagram). Fork texts are untouched; each affected fork carries a
  *Forge note* with the correction. No fork decision changed and no amendment is proposed.
