---
title: "Runtime-neutral observability and memory for mixed Claude/Codex teams — plan (epic)"
type: plan
status: draft
owner: chrisrogers37
created: 2026-10-04
updated: 2026-10-05
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
> `origin/main` has since moved to `4da926d3` (#2143, #2127), touching nothing cited here — except that
> `origin/fix/2140-event-type-follow-ups` (`ef86465a`, `c74ad034`; not an ancestor of `cd292cb`) removes the
> `session_event` path from `bot-vitals.sh` and `registries.py` and edits `fleet-observability.md:66`, which
> P2-b also touches (ironclad cycle 1, §6 P2). Forge added the
> `#### Spec:` subsections in §6, the per-PR plan index in §10, and §14–§15; it changed no fork decision.
>
> **Ironclad cycle 1 (2026-10-05):** seven lenses, 147 → 76 findings after dedup. The fold is logged in §15; the
> proposed fork amendments and the two decisions needing ratification are in §16. No fork text in §3 changed;
> each affected fork carries one *Ironclad cycle 1* pointer. §4.2 holds the evidence the review added.

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

**P1–P3 stand on their own value** (ironclad cycle 1). The join key, clauDNA-owned summaries, consume-by-contract
for the export door, and the hook and volume reductions (`tool_call` off, the digest and its SessionStart hook
retired) pay for a Claude-only fleet; nothing in them waits on a Codex bot. The mixed-runtime goal itself is
delivered by P4 and the companion #2149, which together are the schedule's spine (§10).

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
  are **superseded**. The doors derive the uid from the caller's own session id (§2.2; F1(c) as ratified,
  A-F1 proposed, §16), and the OTel
  join is by the normalized session attribute, so the hook retires with the digest in P3. The amendment
  also dates v2 `:63` and the comment at `ids.py:25-28` ("minted fresh per process at SessionStart"):
  `process_uid` is unminted after P3, and `"process": "proc_"` stays registered so historical rows classify
  (ironclad cycle 1).
- v2 §12 item 2 (`:542`), "export through OTel — authoritative domain record stays local", had the plane
  *emitting*. This plan inverts the direction (OTel → the `plane-otel` intake); the outbound half is
  **deferred** pending §14 Q11 — nothing consumes an outbound stream, and telemetry stays local.

## 2. Architecture

### 2.1 Layers and owners

| Layer | Owner | Answers | Source |
|---|---|---|---|
| Inside a session | **Bought:** native OTel from each runtime, normalized by a local layer (F4(c) as ratified: a Collector; A-F4 in §16 proposes the `plane-otel` intake alone); the normalization layer and the normalized attribute names are Claudlobby's contract — worded mechanism-neutrally so the boundary-spec register row survives either answer | what ran, what it cost, which tools failed | runtime exporters |
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
  parity-tested until it retires in P3). The material rule itself is one Python function,
  `ids.session_alias(platform_session_id, runtime="claude") -> str` (P1 Claudlobby), with
  `derive_session_uid(id, runtime) = derive_uid("sess", session_alias(id, runtime))`; the P2 intake's
  `session` subjects call the same function, so the F2 rule is never spelled twice (ironclad cycle 1).
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
attribute mapping and the exact allowlist are the two `#### Spec:` subsections under §6 P2. A-F4 (§16)
proposes removing the `otelcol` hop — bots export straight to `plane-otel`, which writes the raw files —
and leaves the rest of the picture as drawn.

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

*Ironclad cycle 1:* amendment A-F1 proposed (§16).

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

*Ironclad cycle 1:* amendment A-F3 proposed (§16).

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

*Ironclad cycle 1:* amendments A-F4 and A-F4b proposed (§16).

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

*Ironclad cycle 1:* amendment A-F5 proposed (§16).

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

*Ironclad cycle 1:* amendment A-F6 proposed (§16).

*Forge note (2026-10-04), F6 — two facts the context lacked:* (1) the digest rows are invisible to all
four consumers today — `plane-readers.py` `fleet_events` filters `source_ref LIKE 'fleet-events:%'`
(`:1155-1164,1250-1251`) while `transcript-digest.sh:340-342` stamps `session-digest:<sid>`, and the reader
projects the nested `detail.data` shape `emit_fleet_event` writes (`:1226-1228`), so even a matching
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

*Ironclad cycle 1:* amendment A-F10 proposed (§16).

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

*Ironclad cycle 1:* amendment A-F14 proposed (§16).

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

*Ironclad cycle 1:* amendment A-F17 proposed (§16).

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
- ~~The boundary spec is a draft plan with no formal amendment process.~~ *(Corrected, ironclad cycle 1:)* the boundary spec's §10 was ratified on 2026-07-20 (`00-overview.md:56-58`); only its frontmatter still says `status: draft`, which the P1 Claudron plan fixes in the same touch. Changes land as a PR to Claudron (`documentation/plans/2026-07-20-claudfather-boundary-separation.md`).

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

### 4.2 Ironclad cycle 1 additions (2026-10-05)

Facts the seven lenses added or corrected. Fork texts in §3 are unchanged; where a fact bears on a fork it is
recorded here and, if it argues for a different option, in §16.

Claudlobby (`cd292cb` unless noted):
- **F17's restamp premise is false.** Every receipt mutation is `_save(replace(receipt, …))` and `_save`
  serializes `asdict(receipt)` (`request_receipts.py:519-527,555`), so a loaded v1 file *is* rewritten; a v1
  decoder then raises `TypeError → ReceiptError` (`:436-450`) while the planner trusts the stamp
  (`migration_plan.py:201-203`). The §6 P1 spec is corrected (decoder-first sequencing). Under (a) the first v2
  receipt blocks `host migrate` to every pre-P1 release for as long as it is retained (`_readability_blockers`,
  `migration_plan.py:69-78,425-429`; nothing prunes `state/requests/`), while `canary-rollout.md:49` assumes
  rollback exists. The replay conflict at `request_receipts.py:542-543` fires only for a *prepared, unrecorded*
  previous attempt; a committed one returns from `_existing`/`_replayed` first (`task_operations.py:450-456`).
  A-F17 (§16) proposes (c).
- **Every host-side validator probe warns, never errors** (`validator.py:1138-1152` "Warn (never fail)",
  `:1319-1331` "Warn, never error"), and the same `validate()` backs `config plan`, `config validate` and
  `doctor` (`config_validation.py:124`, `doctor.py:1373-1377`). F4's plan-time refusal would be the validator's
  first host-state `PlanError`; A-F4b (§16).
- **The join was written once before.** `lib/report-back.sh` attached `session_uid` from `.plane-session` to
  task facts from `86b4a987` (#1372) until `c4682f7a` (#1989) deleted it, unrecorded; `_runtime_scripts/CLAUDE.md:116`
  still describes the script. §4's "unwritten" means "unwritten today"; §14 Q12 asks whether the v2 §19 entry
  and the P1 CHANGELOG say *restored*.
- **The receiver hook already holds the executing session.** `plane-dispatch-in.sh:88-93` parses the hook JSON
  and reads only `prompt`; `hook["session_id"]` is in the same object. `Transmission` has a state-conditional
  field precedent — `received_bytes`/`received_sha256` are legal only on `state == "received"`
  (`contracts.py:296-311`) — and `_DELIVERY_MSG` (`queries.py:182-194`) is the read-back path. A-F1 (§16).
- **The marketplace auto-update reaches bots at their next restart.** `start-bot.sh:315-321` calls
  `plugin_ensure` for every required plugin, which runs `claude plugin update` at every start (or once per host
  boot under `BOOT_PLUGIN_UPDATE_ONCE`) and greps `installed_plugins.json` for the name only
  (`lib-common.sh:4699-4710`). `CLAUDNA_VERSION` is composed (`composer.py:1319-1324`) and consumed by nothing.
  §9 records the sequencing risk; §10 orders the per-bot state-dir line first.
- **`installed_plugins.json` is already read** by `validator.py:1630-1654` and `lib-common.sh:4660-4700`, so
  F16's "a dependency on an undocumented format" overstates option (a)'s novelty; clauDNA's `<claudna-root>`
  contract (SKILL_CONTRACT §1.1 `:26-43`, clauDNA#336) does resolve an install path at run time — for a caller
  *inside* a session, which is why F16(b) still holds for the export job (a caller outside one); and
  `<BOT_DIR>/.claude/session.md` (#2094) is the existing plugin-writes/fleet-reads shape `entrypoint.json`
  formalizes with a schema. Decision (b) unchanged.
- **In-flight overlap:** `origin/fix/2140-event-type-follow-ups` (`ef86465a`, `c74ad034`) removes the
  `session_event` path and adds a writer-scan gate (#2122/#2140); P2-b rebases on it (§6 P2).
- **The digest's reader defect is a standing bug, not context:** the `source_ref LIKE 'fleet-events:%'` filter
  landed in #1456, #1503 then moved the digest onto the plane without a read-back test, so 127 `session_digest`
  rows have been invisible to `fleet-digest` since. It is filed independently of P3, so "digester dormant"
  stops passing for a reader bug.
- **Design v2 §19 item 8** (operator ruling, 2026-08-24): hardware measurements decide *budgets* and *process
  choices*, never model shape. Whether a Collector sits between the bots and the intake is a category-(b)
  process choice, decidable on the floor host's C6 numbers (A-F4).

clauDNA (v0.26.0):
- **The summarizer gate's order** (`project.py:276-287`): `CLAUDNA_SESSION_SUMMARY=0` → `disabled`; `=1` →
  summarize; else `actor.kind in (headless, bot)` → `headless`; only then the harvest check. A bot with
  `harvest` on and no explicit `=1` is never summarized, and an unarmed bot's skip reason is `headless`, not
  `disabled`, unless `=0` is composed. §6 P3 says what Claudlobby composes.
- **`_seal` reads no transcript.** It records `file_size(transcript_path)` (`boundaries.py:247-254`);
  `read_range`'s only caller is the summarizer (`summarize.py:184`), running in the detached worker. The §6 P4
  spec is corrected: the per-runtime reader is selected in the worker from `session.json.runtime`.
- **A missing summarizer binary is retried.** `FileNotFoundError` → `SummarizerError(retryable=False)`
  (`summarize.py:88-89`), but the worker's catch-all records `retryable: True` (`:125-129`), so a host without
  `claude` retries each segment `MAX_ATTEMPTS` times. A-F10 names `skipped.reason: no_summarizer`.
- **F15's pre-registration exists:** `documentation/plans/2026-09-30-summarizer-comparison-preregistration.md`
  (#373), "ratified by the owner as written, 2026-09-30, and frozen"; the battery period never started.
  Claudlobby's `library/protocols/ab-gating-rollout.md:39` forbids changing a pre-registration as a quiet edit,
  so the waiver cites the document and withdraws it (P1 clauDNA Task 5: `**Status:** withdrawn 2026-10-04 —
  Claudlobby#2145 F15`, plus a dated spec §11 item in the #203-reversal form). The stronger ground for the waiver
  is that `session_digest` rows never reached their consumers (#1456/#1503): there was no baseline to compare
  against. F15(a) stands.
- **The export iterates every session** (`export.py:88`, with a per-consumer cursor), so one shared store per
  host grouped by `session.actor.bot_id` is mechanically possible — the premise of A-F14.
- **Mission text:** `PROJECT_MISSION.md:5,15` say "for Claude Code"; `:33`/`:62` say clauDNA does not handle
  telemetry; `:24`/`:64` say no hosted dependency, no account or API key. D2 (§16).

Claudlobby mission (`PROJECT_MISSION.md`): `:5` "a fleet of always-on Claude Code bots", `:11` "operating Claude
Code bots in production", `:114` "Per-bot LLM provider abstraction … belong in a different framework";
`README.md:3,12` and `CLAUDE.md:3` repeat the one-liner. `:17` is the 2026-07-06 form for recording a
ratification in the mission (#515). The unmerged `origin/codex/974-mission-consolidation` rewrites the mission
and keeps the line. D1 (§16).

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
| P4 clauDNA — `host_codex.py`, the rollout reader, the Codex manifest | C1 (field names, `session_id` format against `paths._SID_RE`, `transcript_path` nullability), C2 (owner pid or walk), C4 (record types; whether rollouts carry per-turn token usage and tool calls), C8 (plugin root), C9 (`--host` selection) | the `Host` table in the P4 spec; whether the clear link and the sweep work on Codex (F13) |
| P4 Claudron — the Codex adapter in `hooks.py`, the normative snippet, the capability | C1 (event names, `source` values), C3 (stdout injection on `SessionStart(compact)`; whether `PreCompact continue:false` is usable; and, per Claudron#179, whether a block reason reaches an *unattended* Claude model at all) | whether R-capture-prompt is held on Codex, and whether it is held on unattended Claude bots |
| P4 Claudlobby — payload adapters for `plane-dispatch-in` and the `bot-vitals` marker | C1 | field names only |
| Companion #2149 (Codex execution adapter) | C1–C4, C5 (`[otel]` shapes; that `metrics_exporter="none"` + `[analytics] enabled=false` keeps the box silent; a per-bot carrier such as `environment`), C7 (tmux), C8, C9; plus P1 and P4 releases | launcher, `config.toml`, hooks composition, trust-by-hash handling, the `[otel]` block |
| Companion clauDNA#404 (clauDNA on Codex) | C1, C3, C8; plus the P4 manifest | which hooks beyond the store carry over; the manifest's skills scope |

Cross-references the companions should carry: Claudlobby#1997 (the `agent_cli:` proposal and the
`PROJECT_MISSION.md:114` exclusion, §14 Q1–Q2), clauDNA#120/#300/#306 (Codex marketplace install, the
adapter-layer epic, deferred orchestration — #404 overlaps #300 items 1, 2 and 4), Claudron#178/#179.

Prior art the P4 plans carry (ironclad cycle 1, precedent-check). Claudron: decision C trigger 2 — a Cursor/Codex
fleet member un-parks MCP (`2026-07-18-decision-c-mcp-demand-gated.md:41-42`); the *structural* capture-prompt
claim (`CLI_CONTRACT.md:285-289`); the single-reader invariant (#205); "host" already means machine/settings file
in D009/D010, so `hooks install --host codex` overloads the term — the plan picks `--front-end codex` or
`--agent codex`, or documents the overload; R6 on unshipped doors; a new flag takes a capability (#209). clauDNA:
the `hosts` enum (SKILL_CONTRACT §2.2 `:66`; contract and validator move together), `scripts/check_cursor_scope.py`,
`validate-manifest.py` check 13, `release.sh:17-22` `VERSIONED_MANIFESTS` (the loop at `:157-161`), spec §10
`:453` ("a Cursor adapter can land later" → "a Cursor or Codex adapter", P1 clauDNA Task 5). Each P4 plan names
the mission decision it depends on (D1/D2, §16).

Canary answers that decide clauDNA code are mirrored: when the Claudlobby run log records C10/C11 (and P4's
C1–C4/C8/C9), the same row is added to clauDNA's `documentation/plans/2026-09-30-session-store-phase-3.md`
canary table in clauDNA#395's form, naming `scripts/session_canary.py` where it was the harness.

## 6. Implementation plan

Order: P0 → P1 → (P2 ∥ P3 ∥ P4); P2-a and P3 Claudlobby need only P1 Claudlobby's Half A, since Half B is held
(§10; interim fold). Releases gate cross-repo steps and are named in each phase.
Every PR that changes runtime behavior cites canary-root observation
(`harness/validate-bot-change.sh`) per Claudlobby's mandatory runtime validation.

### P0 — Canaries and fork ratification

Record results in a run log next to this plan, in the style of
`2026-09-28-unified-cli-run-log.md`. Size M, not S (ironclad cycle 1): eleven canaries, eight of which need a
Codex install the operator's machine lacks (§14 Q3). P0 runs as **two batches**: the Claude-only batch first
(C6, C10, C11 — they unblock P1 (C10 decides Half A's conditional Task 7b, C11 Half B's child-shell guard), P2
and P3), the Codex batch (step 0, C1–C5, C7–C9) once an install
exists. Each clauDNA-relevant answer is mirrored into clauDNA's phase-3 canary table (§5.1).

- [ ] **Step 0 — Codex telemetry preflight, before any Codex session** (ironclad cycle 1, security): write
  `~/.codex/config.toml` with `[otel] metrics_exporter = "none"` and `[analytics] enabled = false` — the
  default `statsig` exporter is a hosted endpoint (§4.1), and C1–C4/C7–C9 would otherwise run Codex sessions
  before C5 ever mentions it. Capture the socket table during C1 and record it in the run log; C5 then
  *verifies* the preflight. The §11 "no hosted telemetry export" rule applies to every P0 Codex session, not
  only to the companion's composed bots.
- [ ] **C1** Codex hook payloads: the real field names per event and the `session_id` format.
  - Is `transcript_path` set?
  - Does `PreCompact` fire for automatic compaction, and is it followed by `SessionStart(compact)`?
  - Is there a session-id env var for tools and CLIs (the analogue of `CLAUDE_CODE_SESSION_ID`)? Source
    says child shells get `CODEX_SESSION_ID` and `CODEX_THREAD_ID` and hook commands get neither; measure both.
  - Does `session_id` match `paths._SID_RE` (`[A-Za-z0-9][A-Za-z0-9._-]{0,127}`) and Claudron's `ops.py:33`?
  - `SessionEnd`: the docs say `reason` is always `other`, the timeout is 1 s by default (3 s max), and the
    hook does not run for subagents; measure what a 1 s budget leaves the store's `SessionEnd` path (clauDNA's
    appends are fsynced, spec §11.11), so the Codex host plans for the seal landing on the next `SessionStart`
    when the budget is exhausted.
  - Hooks need trust-by-hash before they run *(doc)*: record the `/hooks` step a composed bot would need.
- [ ] **C2** Codex owner process: is there an env var like `$CLAUDE_PID`, and what does an ancestor walk find?
- [ ] **C3** Codex context injection: is `SessionStart` stdout injected (the docs say plain stdout is "added as
  extra developer context", and `source: compact` hooks run "before the next model request")? Can
  `PreCompact` block (`continue: false`) or add an instruction (the docs say its stdout is ignored)? Also,
  per Claudron#179, run the same test on **Claude**: does a PreCompact `decision: block` reason ever reach an
  unattended model, or only `SessionStart(compact)` stdout? This decides where R-capture-prompt lives on both.
- [ ] **C4** Codex rollout files: are they append-only and byte-addressable? Which record types carry user and assistant text? Do rollout records carry per-turn token usage and tool calls (if they do, a Codex session's volume needs no OTel leg)?
- [ ] **C5** Codex OTel:
  - the actual event names and attributes;
  - whether `conversation.id` equals the hook `session_id`;
  - whether any traces exist;
  - whether it supports OTLP/HTTP JSON and resource attributes (source: `protocol = "json"`; the
    endpoint is the full `/v1/logs` URL; `environment` is the only free per-config string — is it usable as
    the per-bot carrier?);
  - that step 0's preflight (`metrics_exporter = "none"` plus `[analytics] enabled = false`) leaves no
    outbound connection (the default `statsig` exporter is hosted) — C5 *verifies* what step 0 introduced; the
    socket table captured during C1 is the first evidence.
- [ ] **C6** Claude OTel on a bot:
  - does `session.id` equal the hook `session_id`?
  - event volume per bot-hour;
  - Collector RSS and CPU on the smallest host class;
  - does it cover every `tool_call` the plane records today?
  - do the `OTEL_RESOURCE_ATTRIBUTES` keys land on every datapoint and event, as documented?
  - is temporality delta, and does the 60 s metric / 5 s log export interval hold under a busy bot?
  - the budget the P2 canary is judged against: Collector RSS and CPU, intake RSS, `state/otel/` growth per
    bot-hour (the proposed figures are in the P2 Collector-and-intake spec; C6 replaces them with actuals).
  - *(added, ironclad cycle 1)* the `resume` case (`ids.py:25-28`: does a resumed session keep its id in the
    exporter as it does in the hook?); whether Claude's exporter buffers when the endpoint is briefly down;
    intake handler latency under a slow daemon; plane samples per bot-day and `ingest_ledger` growth;
    fleet-level RSS/CPU on the canary host before and after (the mission metric is the fleet baseline,
    `fleet-memory-planning.md:43-54`, `PROJECT_MISSION.md:104` — not a per-component budget); the RC criterion —
    the canary bot is an RC/Telegram bot, RC comes up after restart and an inbound message gets a delivered
    reply on day 1 and day 7 (`27813876`, #533: `DISABLE_TELEMETRY` once killed `--remote-control` because
    flag evaluation rides the telemetry channel, `known_values.py:94-103`; *enabling* an exporter has never
    been measured against RC); what the Haiku summarizer children export with `CLAUDE_CODE_ENABLE_TELEMETRY=1`
    inherited under `set -a` (`start-bot.sh:223-228`) and no exporter env of their own (`summarize.py:73-84`
    strips only `CLAUDE_CODE_SESSION_ID`); a 24-hour `tool_call` overlap sample on one bot against transcript
    `tool_use` counts (informational — P2-b does not wait for it).
- [ ] **C7** Codex in tmux: prompt glyph, slash-command injection, restart behavior. Feeds the companion.
- [ ] **C8** Codex plugin-manifest hooks: how does a hook command resolve its plugin root (clauDNA uses
  `${CLAUDE_PLUGIN_ROOT}`)? Source says Codex sets `PLUGIN_ROOT`, `PLUGIN_DATA`, `CLAUDE_PLUGIN_ROOT` and
  `CLAUDE_PLUGIN_DATA` for plugin hook commands; confirm, and record whether a marketplace-installed clauDNA
  loads at all (clauDNA#120 saw the marketplace fetched but the plugin not installed).
- [ ] **C9** Host identification: confirm the adapter can be selected by the hook command itself (`… hook --host codex <event>` — the selector precedes the event, as in `session-store.sh --host codex <event>`, §6 P4 spec) rather than sniffed.
- [ ] **C10** `CLAUDE_CODE_CHILD_SESSION` leak: when a bot's tmux server is started from inside another Claude session, does the variable reach the bot? `start-bot.sh` does not scrub env. If it does, the scrub is P1 Claudlobby Task 7b (Half A; `tests/test_boot_policy_conformance.py`): `start-bot.sh` unsets `CLAUDE_CODE_CHILD_SESSION` before `exec $CLAUDE` (`:268`), with a subprocess check that a bot started from inside a Claude session sees no marker; P3 clauDNA's guard task (plan 5 Task 5) waits on it, and the 0.28 release does not wait (ironclad cycle 1; the "P3 Claudlobby Task 6b" name was a slip — interim fold).
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
  - §10.2: the bought layer, owned by Claudlobby — worded mechanism-neutrally as "the normalization layer and
    the normalized attribute names", so A-F4 (§16), if ratified, never re-amends the register;
  - §10.4: register rows for the join key, the export contract's new fields, and the Codex session-loop snippet (landing in P4);
  - the §2.3 rule from this plan.

  This lands **before** P2 ships the attribute contract (R1/R2).
- [ ] Unify `merge_settings`'s `event_cmds` with `SNIPPET_EVENTS`.
- [ ] Confirm that the ops-log id regex admits Codex ids (C1). If it doesn't, widen it together with its doc-parity update.

Claudlobby:
- [ ] `BotConfig.runtime: str = "claude"`, parsed with `_select_bot_scalar` and validated by `_parse_enum(…, KNOWN_RUNTIMES)` (plan 2's form; the earlier `Literal["claude","codex"]` spelling is corrected — ironclad cycle 1).
  - **Key name (§14 Q1):** #1997 proposes `agent_cli:` because `runtime` already names release activation,
    the Claude Code binary update and the composed-output audit. Forge's lean is to keep `runtime:` (it is
    the cross-repo vocabulary F2/F9 already use, and `host update runtime` already means the agent binary)
    and to document the distinction from `runtime/`; the operator decides before this PR opens.
  - **Mission (§14 Q2; D1 in §16):** `PROJECT_MISSION.md:114` excludes "per-bot LLM provider abstraction",
    and `:5`/`:11`, `README.md:3,12`, `CLAUDE.md:3` say Claude Code; no fork decides that Claudlobby composes
    Codex bots (F11 decides *where* Codex launching lives, not *whether*). The PR amends all of them
    coherently, **unconditionally and before merge**, citing F18 or the operator's decision record in the
    `:17` form (#515), coordinated with `origin/codex/974-mission-consolidation`. ~~or the operator records
    that the ratification supersedes it~~ — withdrawn (ironclad cycle 1).
  - The validator rejects unknown values.
  - A `codex` bot fails validation with "execution adapter not shipped" until the companion lands.
  - Docs: `documentation/fleet-yaml-schema.md`, `fleet.yaml.example`, `config_explain`.
- [ ] Composed `CLAUDLOBBY_RUNTIME` in `bot.conf`, so the doors know the runtime for `derive_session_uid(…, runtime)`. Add a row in `documentation/environment-variables.md` and a composer test.
- [ ] `BotPayload.runtime` in the registry keyframe (`contracts.py:670-688`, `extra="forbid"`) and `registry_emit.bot_payload`.
  - Decided in the per-PR plan: an **additive optional field with a `wire_additions` declaration, no
    payload-schema bump** (the `WorkstreamEvent.waiting_on` precedent, `migration_plan.py:421-424`), written
    only when it carries information — `registry_emit.py:170-173`'s rule, so a keyframe drained by an older
    daemon keeps the shape every daemon accepts. In P1 every bot is `claude`, so no keyframe carries the key
    until the companion ships on a release whose floor includes this contract.
- [ ] `ids.session_alias(platform_session_id, runtime="claude") -> str` (the raw id for `claude`, `f"{runtime}:{id}"` otherwise — the one statement of F2's material rule) and `derive_session_uid(platform_session_id, runtime="claude") = derive_uid("sess", session_alias(…))` per F2, in Python (the doors' only derivation; the P2 intake's `session` subjects call `session_alias` too — ironclad cycle 1). The bash mirror in `plane-session-start.sh` stays Claude-only until it retires in P3 (no Codex bot can run before the companion). Extend `tests/test_plane_ids.py` with a Codex case that pins the composition.
- [ ] Task and report doors attach `session_uid` per F1(c), derived from the caller's session-id env (§2.2) — plan 2
  Half B (Task 6 Step 2b, Tasks 7–9), held on A-F17/A-F1 (§16):
  - `task_operations.accept` and `_assignment_report` (`claudlobby/task_operations.py:445, 473+`; raws built by `_raw()` at `:312-317`) set `events.session_uid`;
  - `encode_report_facts` (`fleet reports submit`) and the send path in `message_operations` set `sender_session_uid`.
  - **Receipts:** per F17, a retried `--request-id` after a `/clear` or restart must not raise `ReceiptConflict`. Under F17(a), `RequestIntent` carries `session_uid` and a replay reuses it (receipt format bump). Test exactly that replay. *Ironclad cycle 1:* the spec below is corrected (decoder-first two-release sequencing; `_save` stamps the current `FORMAT_VERSION`); A-F17 (§16) proposes (c) instead, so plan 2's Task 6 Step 2b (the writer flip, Half B) is held until the operator rules; Step 2a, the decoder, is forward-compatible and ships unheld with Half A.

  `task show` (the reducer must keep the `session_uid` column it already selects, `task_state.py:77-85,200-204`)
  and `_runtime_scripts/plane-lookup.py` print the session — `plane-lookup.py --session <sess_uid>` is the door
  that joins on it, uid only: P1's rows carry no raw id and `.plane-session` holds uids only, so printing
  `session_id` beside `session_uid` is what A-F1 (§16) would add (interim fold). Tests cover each door, including the leg that reaches the F17
  code: a first attempt *prepared but not committed*, then the retry from session B (the §6 P1 spec). The canary
  adds a 48 h keepalive/restart-count observation — Half B changes every task, report and message door and the
  receipt format, and no harness scenario drives them (ironclad cycle 1).
- [ ] One Python fleet-event row helper (ironclad cycle 1, extension-check; plan 2 Task 9b, **Half A**):
  `fleet_event_request` in `claudlobby/plane/fleet_events.py` builds exactly the row `emit_fleet_event` writes (`lib-common.sh:2117-2173`: `event_type:
  system`, `source_ref: "fleet-events:…"`, `payload.data = {"source", "legacy_ts", "data"}`), with a bash/Python
  parity test; the two hand-rolled copies (`fleet_notification.py:91-97`, `message_operations.py:790-797`) adopt
  it in that PR; P2's intake events and P3's `session_export._system_event` call it. The digest is being retired
  because a writer got this shape wrong.
- [ ] Amend design v2 per §1.1 (including the `:63` note and a numbered §19 entry — the superseded SessionStart
  ruling is item 6, `:606`; `:603` is the bracketed-note form).

clauDNA:
- [ ] `session.opened.data.runtime`: optional, top-level, `choices=("claude","codex")`; when absent it reads as `claude`.
- [ ] `session.json` gains `runtime`: tag `claudna.session/2`, `claudna.session/1` added to `OLDER_PROJECTIONS`, and the stale-projection sweep taught about `session.json`. Mixed 0.26/0.27 **readers** then converge instead of
  rewriting each other's file (a 0.26 reader folds a `/2` file in memory and writes nothing). A 0.26 **writer** —
  a bot not yet restarted onto 0.27, or its detached summarizer/harvest/sweep — still rebuilds the file to `/1`
  on its own writes and 0.27 rebuilds it forward: one rebuild each, lossless (the log holds `runtime`), ending
  when the last 0.26 process restarts. The per-PR plan pins both halves with tests.
- [ ] `claudna.export/1` items gain `runtime` (additive). Document it in spec §8.
- [ ] Harvest provenance per F9. Add a case to `tests/test_claudron_live.py`.
- [ ] Amend spec §1.1: rule 1 (the plane no longer records every tool call after P2) and, F15(a) being ratified, rule 4 (the comparison is waived; the store's summarizer is the single owner; the coverage concern is kept by F6). The waiver cites and withdraws the frozen pre-registration (`documentation/plans/2026-09-30-summarizer-comparison-preregistration.md`, #373 — `**Status:** withdrawn 2026-10-04 — Claudlobby#2145 F15; the battery period never started; the digest retires in P3`) with a dated spec §11 item in the #203-reversal form (`:466`), per `ab-gating-rollout.md:39` (§4.2); and spec §10 `:453` gains "or Codex" (ironclad cycle 1).
- [ ] clauDNA's mission amendment (D2, §16): the session store as local memory with an export door (no phone-home), reconciling `PROJECT_MISSION.md:33`/`:62` with `tool.failed`/`skill.invoked`/`CLAUDNA_TELEMETRY`, and whether clauDNA's scope includes non-Claude hosts (decided before the P4 clauDNA plan is written; F8 is the aligned direction). **Requires operator approval**; lands before 0.27.0 ships `codex` in a closed vocabulary.
- [ ] Release clauDNA 0.27.0 (the export and session fields are needed by P3). Marketplace auto-update delivers it to tmux-hosted bots at their next restart whatever the fleet "pins" (§9): the 0.26/0.27 writer flip-flop above is fleet-wide until the per-bot root lands, and the sweep rebuilds any session with stale projections, not only closed ones (`retention.py:190-191`).

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

- **Resolution, once per invocation.** P1's `caller_session_uid()` in `operation_context` (plan 2 Task 7; bound
  by `bind_task_context`, `claudlobby/operation_context.py:109-136`, and by the message routes; the caller
  selectors are read at `:182-210`) maps `CLAUDLOBBY_RUNTIME` (default `claude`) to that runtime's session-id
  env — only `claude` → `CLAUDE_CODE_SESSION_ID` today, so `codex` (or any other runtime) → `None` until C1 names
  Codex's variable and the companion adds the row — and returns `derive_session_uid(id, runtime=runtime)` when
  the id is present and `None` otherwise (§2.2: "with no session-id env, the door records no uid rather
  than a possibly wrong one"), and `TaskOperationContext` (`claudlobby/task_operations.py:46-66`) carries it.
  Canary C11 decides whether a subagent's shell (`CLAUDE_CODE_CHILD_SESSION=1`) sees the same id; until it says
  so (`SUBAGENT_SHELL_SHARES_SESSION_ID`), the resolver returns `None` under that variable rather than another
  session's uid.
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
| `runtime_versions.py:21-22` | `RECEIPT_FORMAT_VERSION = 1`; `SUPPORTED = {0, 1}` | **Release N:** `SUPPORTED = {0, 1, 2}`, the writer stays `= 1` — the decoder ships first, per the module rule "Add readable versions only alongside their implemented decoders" (`:10`). **Release N+1:** `RECEIPT_FORMAT_VERSION = 2`. *(Corrected, ironclad cycle 1: one release doing both makes the first v2 receipt a rollback blocker to every earlier release.)* |
| `request_receipts.py:25` | `FORMAT_VERSION = 1` | `from .runtime_versions import RECEIPT_FORMAT_VERSION as FORMAT_VERSION, SUPPORTED_RECEIPT_FORMAT_VERSIONS` — the two literals become one |
| `request_receipts.py:300-301` `_validate` | `format_version != FORMAT_VERSION` → error | `format_version not in SUPPORTED_RECEIPT_FORMAT_VERSIONS - {0}` → error (0 is "absent", never a file) |
| `_save` (`:518-533`) | stamps the dataclass default | stamps the **current `FORMAT_VERSION` on every write**. ~~a loaded v1 receipt keeps `format_version: 1` through `replace(...)` and is never restamped~~ — false (ironclad cycle 1): every mutation is `_save(replace(receipt, …))` and `_save` serializes `asdict(receipt)` (`:519-527,555`), so a v1-labelled file would gain `"session_uid": null` and a v1 decoder would raise `TypeError → ReceiptError` (`:436-450`) while the planner trusted the stamp (`migration_plan.py:201-203`) — a rollback that plans clean and fails at runtime. Restamping on write is honest; the alternative is to serialize without `session_uid` while `format_version == 1` |

Tests this touches, by pin: `tests/test_request_receipts.py:127-131` (format 2 is now valid, 3 is refused;
a v1 file loads with `session_uid is None`), `tests/test_releases.py:93-96` (`"receipt_format": 2`),
`tests/test_migration_plan.py:229-233,356-366,416-421` (the `[1]` legs gain a v1-retained → v2-target case
with **no** blocker and a v2-retained → v1-target case with the blocker), `tests/test_release_install.py:118`
(the sealed manifest picks the declaration up unchanged); plus a `host migrate` **preview test for the N+1 → N
pair** (the rollback direction plans the blocker it will hit). **The F17 test itself:** the first attempt from
session A is *prepared but not committed* (`store.prepare` via `_prepare`, stopping before `_commit`, or
`store.outcome(0, "unrecorded")`) — a committed first attempt returns from `_existing`/`_replayed` before
`_prepare` (`task_operations.py:450-456`) and would pass with or without the change; `/clear`; retry the same
`--request-id` from session B → `status: committed`, no `ReceiptConflict`, the recorded row carries uid A
~~and the receipt on disk is unchanged~~ (the retry's `begin_attempt` rewrites the file); a *new* request from
session B carries uid B. The same pair for `message send` and `fleet reports submit` (`sender_session_uid`).
**Disclosed consequence** (CHANGELOG, `documentation/fleet-update-lifecycle.md`, the canary step): once the N+1
writer ships, the first v2 receipt is a `host migrate` blocker to every release before N for as long as it is
retained — `state/requests/` has no prune lane — so the canary observes `host migrate`'s plan output in the
rollback direction. **A-F17 (§16)** proposes leaving `session_uid` out of the hashed projection instead, which
needs none of this; plan 2's Task 6 Step 2b (the N+1 writer flip) is held until the operator rules, while Step 2a
(release N's decoder, with Half A) is forward-compatible and not held.

### P2 — Local OpenTelemetry pipeline (Claudlobby; gated by F4)

P2 splits (ironclad cycle 1; align-to-mission, cost-benefit, first-principles, precedent-check): **P2-b** stops
`tool_call` and needs nothing from the Collector or the intake — it ships first in §10's order, size S;
**P2-a** is the pipeline, size L, held on A-F4, A-F4b, A-F3 and A-F5 (§16; per task in §10.1). Plan 4 (§10.1)
carries both.

**P2-b — marker-only `bot-vitals.sh` (first, independent, S).**
- [ ] Stop `bot-vitals.sh` emitting `tool_call` (`:62-64`), with the #874 fix (the hook falls back to `$PWD` for
  cwd). Nothing reads the rows (`retention.py:77-90`), and until they stop the hook spawns `python3` plus a socket
  round-trip twice per tool call (~13,000 emits a day on the Pi — the largest avoidable resource cost in the
  epic). Gate: the harness marker scenarios plus a 48 h canary-root check that keepalive's busy verdicts
  (`keepalive.sh:123,378,435`) and fleet-pulse's `activity_stuck` (`fleet-pulse.sh:519-610`) are unchanged.
  C6's "does OTel cover every `tool_call`" is answered by a 24-hour overlap sample on one bot against transcript
  `tool_use` counts (informational) — P2-b waits neither for it nor for the week. §14 Q9 (does Claudosseum read
  `tool_call` rows) is answered before it merges.
  - It keeps touching `.last-tool-call`.
  - The registry entries stay, and `tool_call` stays in `PRUNABLE_SYSTEM_EVENTS` so existing rows can still be pruned.
  - ~~and remove the `session_event` path (no payload carries that key)~~ — already removed on
    `origin/fix/2140-event-type-follow-ups` (`ef86465a`, `c74ad034`): P2-b rebases on it or cites #2140,
    re-anchors its `bot-vitals.sh`/`registries.py`/`test_event_type_registry.py` cites at PR-open, and its writer
    change passes the #2122/#2140 writer-scan gate.
  - Update `tests/test_event_type_registry.py:100-101`, `tests/test_plane_cutover_keepalive.py:94-110`, and `tests/test_plane_emit_class.py:170-185`. (`tests/test_system_event_retention.py:100` pins `{"tool_call","wip_uncommitted"}` and stays unchanged.)
  - Update `library/protocols/fleet-observability.md:66`, `documentation/guides/observability.md:83,88`, and the `bot-vitals` index rows (root and `_runtime_scripts` `CLAUDE.md`/`AGENTS.md`).

**P2-a — the pipeline (L; held on A-F4, A-F4b, A-F3, A-F5).**
- [ ] The `otel-collector` host service (F4): a `Switch` row (`HOST_SERVICE`, `ENROLL_HOST`, opt-in, `plane=True`) and `host.jobs.otel-collector` (`unit: service`) running upstream `otelcol-contrib` on `127.0.0.1`. The binary is operator-installed (Claudlobby installs
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
  - `OTEL_RESOURCE_ATTRIBUTES` with `claudlobby.fleet=<fleet name>` (the alias, never a uid), `bot:<fleet>/<bot>` and `agent.runtime=claude`;
  - the content gates (`OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_TOOL_DETAILS`) only when `content: full`.

  Applied at the bot's next restart. `config plan` fails when an enabled bot's host has no enrolled Collector; test it. *(A-F4b in §16 proposes a named warning instead; the step stands as ratified until the operator rules.)*
- [ ] `plane-otel` intake: its own localhost host service, not the daemon or the view, which are pinned to their scopes, with its own `Switch` row (`HOST_SERVICE`, `OPT_IN`, `ENROLL_HOST`, `plane=True` — one row per service, `switches.py:290-310`; ironclad cycle 1). It maps an allowlist onto the daemon's socket through `daemon.send_batch` (`daemon.py:917-941`, the Python door; `emit_batch` behind it; the daemon stays the single writer):
  - per-session cost, tokens, tool calls/failures, API requests/errors and active time as `metric_samples`
    on `subject_kind='session'` (five new `METRIC_NAMES`), with the derive-not-mint rule for session
    subjects so the sample's `subject_uid` is the F2 uid;
  - `api_error` and repeated tool failure as system events (`SYSTEM_EVENT_SEVERITY`).

  Also: a doors-table row in `documentation/architecture/observable-plane.md`, and a launcher in `_runtime_scripts/` with its index line in both `CLAUDE.md`/`AGENTS.md` pairs, within the 32 KiB Codex budget (`tests/test_instruction_budget.py`).
- [ ] Canary: one Claude bot, one host, one week. It passes when C6 holds, the Collector stays inside its stated resource budget, the **fleet-level** RSS/CPU delta on the canary host stays within the recorded Pi 5 baseline (`fleet-memory-planning.md:43-54`; the mission metric, `PROJECT_MISSION.md:104`), the raw sink stays under its total bound (the Collector spec), and the canary bot — an RC/Telegram bot — gets a delivered reply to an inbound message on day 1 and day 7 (C6). The "what did session X cost" `jq` from `observable-plane.md` is run as a canary step (§8).
- ~~Then stop `bot-vitals.sh` emitting `tool_call`, and remove the `session_event` path~~ → **P2-b**, above: it
  no longer waits on the pipeline or the canary (ironclad cycle 1).
- [ ] Decide v2 §9b's usage items on the evidence, at the end of P2-a where the evidence is. ~~`claudlobby/transcript_usage.py` retires if OTel answers its questions~~ — it does not retire; its readers split (ironclad cycle 1): `session_usage`/`utilization_windows` are deleted from the roadmap per design v2 §12 pilot (a) (`:545`) — P2-a pre-empts that decision in substance by building per-window `metric_samples`; the MetricSample lane carries the token/cost axes as `session.*` samples; the evaluation axes (`protocol_sensitive`, `cost_weighted_total`, `comms_share_est`) stay in `transcript_usage.py`; `brief_read.py:114` / `usage_read.py:39` are repointed inside P2, or the first reader is named (§8). `session.active_time_s` is Claude-only and decides nothing: `utilization_windows` is decided on `plane/utilization.py:1-17`, which already computes busy % runtime-neutrally from `bot.heartbeat`.

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
# claudlobby/known_values.py — beside KNOWN_EFFORTS (enums live here, one home)
KNOWN_TELEMETRY_CONTENT: frozenset[str] = frozenset({"metadata", "full"})

# claudlobby/config.py — beside IsolationConfig / ObservabilityConfig
@dataclass(frozen=True)
class TelemetryConfig:
    enabled: bool = False
    content: str = "metadata"        # metadata | full — KNOWN_TELEMETRY_CONTENT

# P2's generic strict-mapping helpers: ONE parser and ONE field-wise merger for every strict sub-mapping on
# BotConfig (P3 Claudlobby registers `claudna:` here in one line, never a second copy)
def _parse_strict_mapping(raw: Any, where: str, fields: dict[str, Callable[[str, Any], Any]]) -> dict: ...   # unknown key → ValueError naming it
def _merge_fieldwise(cls, defaults_raw: Any, bot_raw: Any, name: str, key: str, fields: dict): ...   # field-wise, bot over defaults, built-in last
_TELEMETRY_FIELDS = {"enabled": …,   # _strict_bool
                     "content": …}   # _parse_enum(…, KNOWN_TELEMETRY_CONTENT)
_STRICT_MAPPING_FIELDS: dict[str, dict] = {"telemetry": _TELEMETRY_FIELDS}

# BotConfig gains:
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
```

`scalar_config_origin` (`config.py:2262-2297`) reads `_STRICT_MAPPING_FIELDS` so `config explain` reports where each
field came from (`bot` / `fleet.defaults` / `built_in`). `_select_bot_scalar` (`:1735-1741`) is not used:
it selects whole values by presence, and a bot that sets only `enabled: true` must still inherit
`content` from `defaults`. *(Correction to F4's "same precedence as other bot scalars,
`_select_bot_scalar`": the precedence is the same; the helper is the mapping one.)*

**`content: full` is the disclosed act** design v2 §11 asks for ("native OTel content gates … **off by
default**, enabling them is an explicit disclosed act"). It is the only thing that composes the runtime's
prompt and tool-detail gates (below). Nothing in `metadata` mode carries prompt or tool text. This is
reconciled explicitly with the plane's own "full bodies day one" default (v2 F7, `:31`; F23, `:46`): the plane
keeps its capture policy for what its *doors* emit, while the *runtimes'* prompt and tool-detail gates are the
vendor's (v2 §11, `:534`) and stay off until the operator discloses `full` (ironclad cycle 1).

**The switch row** (`claudlobby/switches.py`, beside `mcp-direct-launch` `:604-626`, the restart-gated
`COMPOSE_BOT` template):

```python
    Switch(
        key="telemetry",
        scope=GENERATE,
        polarity=OPT_IN,
        carrier=COMPOSE_BOT,
        config="telemetry.enabled",          # _bot_config_value walks this path; the leaf must be a bool (:853-860)
        why_opt_in="arrives at every armed bot at its next restart (the #1265 arrival category: composed "
                   "into bot.conf, restart-gated, like mcp-direct-launch) and changes what that bot's claude "
                   "process exports — with content: full, prompt and tool text; one bot on an independent "
                   "canary root first, remote-control verified (#533; #2145 F4)",
        what="export the bot's native OpenTelemetry metrics and events to the host's local Collector "
             "(127.0.0.1 only; raw files under state/otel/, an allowlist into the plane)",
        compose_steps=("config plan --release RELEASE_ID, config diff PLAN_ID, host activate PLAN_ID",),
        takes_effect=("it takes effect when that bot next restarts: bot.conf is read at session start",),
        takes_effect_off="exports stop at the bot's next restart",
    )
```

`tests/test_switches.py:99-178` (the opt-in allowlist) gains `"telemetry"`, `"otel-collector"` and `"plane-otel"`
with their reasons; `claudlobby host doctor --switches --markdown` regenerates the three pinned tables
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
is the opposite switch from `DISABLE_TELEMETRY` (`known_values.py:115-118`). That env block was measured against
RC, not assumed safe: `27813876` (#533) found `DISABLE_TELEMETRY` silently killing `--remote-control` because
flag evaluation rides the telemetry channel (`known_values.py:94-103`); *enabling* an exporter has never been
measured against RC, which is why the P2 canary bot is an RC/Telegram bot with the day-1/day-7 delivered-reply
criterion (C6; ironclad cycle 1).

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
  carrier=ENROLL_HOST, job="otel-collector", plane=True, why_opt_in="deletes data — its file rotation ages raw telemetry "
  "out by count and age (the plane-prune-system-events category, #1744); and a resident process on the "
  "smallest host class", what="run the host's local OpenTelemetry Collector on "
  "127.0.0.1 (receives the bots' OTLP, writes rotated raw files under state/otel/, forwards an allowlist "
  "to plane-otel)")`. ~~`plane-otel` rides the same row's arming recipe~~ — `plane-otel` gets **its own row**
  (`Switch(key="plane-otel", scope=HOST_SERVICE, polarity=OPT_IN, carrier=ENROLL_HOST, job="plane-otel",
  plane=True, …)`, its `what` naming the Collector as its feeder: every `HOST_SERVICE` has one,
  `switches.py:290-310`, and an unrowed job gets no `host doctor --switches` recipe — ironclad cycle 1);
  `_validate_timers` (`validator.py:2094-2103` pattern) still warns `job-inert` when exactly one of the two is
  enrolled. Both `why_opt_in` reasons sit in categories `tests/test_switches.py:99-178` admits (`telemetry`:
  arrival; `otel-collector`: deletes data).
- The binary: Claudlobby installs no third-party binaries (`commands/setup.py:85-88` refuses on a
  missing `tmux`/`claude`/`jq`; the one installer is the `claude-update` host job via `npm`,
  `update-claude-code.sh:400`). `otelcol-contrib` is **operator-installed** (Homebrew
  `opentelemetry-collector-contrib`, Debian package, or the upstream release tarball for
  `linux/arm64`); `host setup` adds `otelcol-contrib` to its refusal list **only when `otel-collector`
  is enrolled**, and `host doctor` gains a rung that names the resolved binary and its version. *(The
  epic's "installed by host setup" is corrected to this; it is not a fork.)*
- The launcher `_runtime_scripts/otel-collector.sh` is thin like `plane-daemon.sh:1-27`: sources
  `cli-context.sh`, requires the root, reads `OTELCOL_BIN` from `runtime/_host/otel/otelcol-bin` — the binary the
  composer resolved at compose time (`$CLAUDLOBBY_OTELCOL_BIN`, else `otelcol-contrib` on PATH) and wrote beside the
  config; the launcher never probes PATH itself (a unit's PATH is stripped; P2's form, interim fold) — and `exec`s
  it with `--config "$CLAUDLOBBY_ROOT/runtime/_host/otel/collector.yaml"`
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
        _otel_cfg = load_host_jobs().get("otel-collector") or {}   # _otel is the module alias (from . import otel_collector as _otel)
        otel_enrolled = _otel_cfg.get("enroll") is True      # the service rule, composer.py:5291-5306
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
exactly when `validate()` errors. *Ironclad cycle 1:* this would be the validator's first host-state `PlanError`
against its warn-never-error precedent (`validator.py:1138-1152,1319-1331`; §4.2), and one host's enrollment
state would refuse CI and other hosts through the shared `validate()`; A-F4b (§16) proposes a named warning plus
the `host doctor` rung. The block stands as ratified until the operator rules.

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
   a header comment, and the plane's own registry names (below) are the stable surface readers use. A-F5 (§16)
   proposes scoping F5 to the intake's internal mapping — no semconv pin, no OTTL; this spec stands as ratified
   until the operator rules.
2. **Semconv input tokens include cached tokens; Claude Code's do not.** `gen_ai.usage.input_tokens` "SHOULD
   include all types of input tokens, including cached tokens"; `claude_code.api_request.input_tokens` and
   `claude_code.token.usage{type="input"}` exclude "tokens read from or written to the prompt cache", while
   Codex's `input_token_count` follows OpenAI's inclusive convention. The Collector produces the semconv
   value for Claude by addition; the plane stores **uncached input** separately so both runtimes compare.
3. **Codex's default metrics exporter is hosted.** `[otel].metrics_exporter` defaults to `statsig`, which
   resolves to `https://ab.chatgpt.com/otlp/v1/metrics` *(source)*; keeping telemetry local requires
   `metrics_exporter = "none"` (or the local Collector) **and** `[analytics] enabled = false`. This is a
   companion-epic composition fact (F11), P0's step 0 for every canary Codex session, and a §11 "what not to
   do" line.

**The Collector.** `otelcol-contrib` (the core `otelcol` distribution ships no `transform` processor;
contrib v0.162.0 is 102 MB for `linux_arm64`, 97 MB for `darwin_arm64`). Rendered by the composer to
`runtime/_host/otel/collector.yaml` from the `system.yaml` entry (F4 spec), validated in tests with
`otelcol-contrib validate --config=<file>` — a **required CI leg with a pinned binary version** while F4 stands
(ironclad cycle 1, cost-benefit and adversarial-review: a config no CI can validate is the alpha exporter's
failure mode), never an optional local check:

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
    rotation: { max_megabytes: 64, max_days: <retention-days>, max_backups: 15, localtime: false }   # 64 MiB × 15 × 2 files ≤ 2 GB (was 30)
  file/metrics:
    path: <CLAUDLOBBY_ROOT>/state/otel/metrics.jsonl
    format: json
    create_directory: true
    rotation: { max_megabytes: 64, max_days: <retention-days>, max_backups: 15, localtime: false }
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
- **The raw sink's total bound** (ironclad cycle 1): `max_megabytes × max_backups × 2 files` = 64 MiB × 15 × 2
  = 1.92 GB per host, under the 2 GB ceiling (the earlier `max_backups: 30` was ~3.9 GB on the SD card the
  plane's SQLite shares). `max_backups` deletes by count before `max_days` deletes by age, so the ceiling holds
  whatever `--retention-days` says; both knobs are documented. Whichever component writes `state/otel/`:
  `UMask=0077` on its unit (precedents `start-bot.sh:204`, `plane-session-start.sh:80`, `plane/spool.py:105`,
  `plane/daemon.py:749-752` — with `content: full` these files hold prompt text, and v2 F22/§11 want 0700/0600
  for content-bearing stores; `_write_service_units`, `composer.py:4040-4178`, emits no `UMask=` today), the
  launcher pre-creates `state/otel/` 0700, `host doctor` gains a rung for mode, total size and oldest file, both
  process names join `fleet-memory-check.sh:82-89`'s filter with a row in `fleet-memory-planning.md`, and the
  total bound is a canary pass criterion.
- Budget for the one-host P2 canary (C6 records the actuals; v2's decision-framework ruling: hardware
  informs budgets, never model shape): Collector RSS ≤ 192 MiB and ≤ 3 % of one core, averaged over the
  week on the smallest host class; the intake ≤ 64 MiB. A miss opens a defect investigation, not a
  schema change.

**The `plane-otel` intake** (`claudlobby plane otel-intake --host 127.0.0.1 --port 4319`, launched by
`_runtime_scripts/plane-otel.sh`; stdlib only — `http.server` + `json`, no protobuf, per §11). It accepts
`POST /v1/logs` and `/v1/metrics` with OTLP/JSON bodies (`resourceLogs[].scopeLogs[].logRecords[]`,
`resourceMetrics[].scopeMetrics[].metrics[].sum.dataPoints[]`), always answers `200` **before forwarding** (it
never back-pressures a bot's exporter — a stdlib `BaseHTTPRequestHandler` that posted to the socket inside the
request path would block the next exporter for up to the 5 s socket timeout; the window flush already decouples
the timing, or the server is a `ThreadingHTTPServer` — ironclad cycle 1), and keys everything on
`(agent.runtime, gen_ai.conversation.id)` from the normalized attributes. It is a **translator, not a writer**:
it posts `{"events": […]}` batches to the daemon's socket (`<root>/state/plane/ingest.sock`, `daemon.py:207-208`;
protocol `:16-24`) through `daemon.send_batch(sock_path, events, timeout=5.0)` (`daemon.py:917-941`, signature
`send_batch(sock_path, events, *, timeout=30.0)` — "any Python
caller's door", which already guards the replied-and-closed race; the intake is its first production caller and
pins its exception contract in `tests/test_plane_daemon.py`, catching `OSError`/`ValueError` to drop-and-count),
never a private `post()`; its `serve()` mirrors `PlaneDaemon.serve(install_signals=True)`'s SIGTERM/SIGINT
handling (`:823-834`). So the daemon's held `PlaneWriter` stays the plane's single recorder; a socket miss drops
the batch and increments a counter in the intake's own log (the raw files already hold the data; staging would
duplicate the Collector's queue). The rule, written once (`daemon.py:1-30`): resident services post to the
daemon socket; one-shot timer ticks and CLI doors may call `emit_batch` in-process as the task doors do
(`task_operations.py:351`) — P3's `session-export` is the latter.

*The allowlist — everything else never reaches the plane.*

| Plane name | Kind | Value | Claude source *(doc)* | Codex source *(doc/source)* |
|---|---|---|---|---|
| `session.cost_usd` | metric | number (USD, **delta per window**) | `claude_code.cost.usage` datapoints | `codex.turn.cost_microusd` / 1e6, else `codex.turn_cost.usage.estimated_usd` |
| `session.tokens` | metric | `{input, output, cache_read, cache_write}` (delta; `input` is **uncached**) | `claude_code.token.usage` by `type`: `input`→input, `output`→output, `cacheRead`→cache_read, `cacheCreation`→cache_write | `codex.turn.token_usage` by `token_type`: `input − cached_input`→input, `cached_input`→cache_read, `output`→output; cache_write from `sse_event.cache_write_token_count` |
| `session.tool_calls` | metric | `{calls, failures}` (delta) | count of `tool_result` events; `success == "false"` → failures | count of `codex.tool_result` events; `success == false` |
| `session.api_requests` | metric | `{requests, errors}` (delta) | `api_request` events; `api_error` + `api_retries_exhausted` → errors | `codex.api_request` events; `error.message != nil` or `http.response.status_code ≥ 400` → errors |
| `session.active_time_s` | metric, **enrichment only** (§2.3: Claude-only; load-bearing for nothing — busy % is `plane/utilization.py:1-17` from `bot.heartbeat`) | seconds (delta) | `claude_code.active_time.total` | — (absent) |
| `api_error` | system event, `notice` | `data.data = {runtime, session_id, session_uid, model, status_code, attempt, exhausted, count}` | one per `api_error` / `api_retries_exhausted` (`exhausted: true`), ~~debounced to one event per session per 5-minute window~~ one event per session per **60 s intake window** with `count` (the window is the intake's only clock; see *Window*) | `codex.api_request` with an error |
| `tool_failure_streak` | system event, `notice` | `data.data = {runtime, session_id, session_uid, tool, count}` | ≥ 3 consecutive `tool_result success=="false"` for one `gen_ai.tool.name` in a session, counted **inside one 60 s window**; reset on success; no cross-window state (see *Window*) | same, from `codex.tool_result` |

- **Window.** Metric samples are emitted once per `(runtime, session)` per 60-second window (Claude's
  metric export interval *(doc)*; Codex's exporter batches asynchronously), carrying the window's **delta**
  (Claude's default temporality is delta *(doc)*; the intake sums datapoints and event counts inside the
  window). "What did session X cost" is `SUM(value)` over its samples — the intake keeps no cross-window
  state for samples, so a restart loses at most one window and never double-counts. Each `METRIC_NAMES`
  description says so (`registries.py:296-337` shape `name → {unit, description}`; object values follow the
  `host.swap_pages` precedent). *(Corrected, ironclad cycle 1:)* the two system events follow the same clock —
  `api_error` is one event per session per 60 s window with `count` (the 5-minute debounce would have needed
  state the intake forswears); `tool_failure_streak` is counted **inside one 60 s window**, with no cross-window
  state at all — a streak straddling a window edge is two shorter runs and may go unreported, the stated bound
  (P2's definition, adopted in the interim fold; the cycle-1 bounded cross-window counter is dropped). **A batch
  never mixes seen and unseen ids:** each
  window's samples go in one batch and each system event in its own single-event batch (`_verify_duplicates`
  refuses a mixed batch with `RuntimeError`, `ingest.py:548-552,598-605`, and `emit_batch` does not spool that,
  `emit_api.py:273-280`). A straddle test — failures 2+2 across a window edge → no `tool_failure_streak`, 3+1 →
  one; an `api_error` burst across one — pins both.
- **Sidechain spend** is attributed on neither path today (ironclad cycle 1): subagent turns live in nested
  transcripts outside any segment range (`transcript_usage.py:20-25`, ~13 % of real fleet spend), and whether
  OTel attributes a subagent's `api_request` to the parent `session.id` is exactly what C11 leaves open. The
  allowlist declares **no** `sidechain` bucket: `session.tokens` stays `{input, output, cache_read, cache_write}`
  and `session.cost_usd` a number (P2's shape) — subagent attribution is §2.3 enrichment, not allowlisted (interim
  fold; the cycle-1 bucket is dropped).
- **Subject and identity (the derive-not-mint rule).** Samples are `metric_sample` requests with
  `subject_kind = "session"` (legal everywhere: `contracts.py:86,732-733`, `samples.py:24`, the DDL
  CHECKs) and `subject = ids.session_alias(id, runtime)` (P1 Claudlobby Half A: the raw id for `claude`,
  `f"{runtime}:{id}"` otherwise — the one Python statement of F2's material rule; the intake never builds the
  string itself — ironclad cycle 1). Today
  `MetricSample` has only the alias form and `identity.resolve` mints a **random** uid on first sight
  (`identity.py:36`, via `ingest._batch_resolver.entity`, `ingest.py:136-149`), which would break the
  §2.2 join. P2 adds one rule to `_batch_resolver.entity`: for `kind == "session"`, the uid is
  `"sess_" + sha256(alias)[:32]` — by construction `derive_session_uid(id, runtime)` (`ids.py:79-89`
  extended per F2) — inserted `INSERT OR IGNORE` with `provisional = 0` and `parent_uid` = the bot's
  actor uid when `claudlobby.bot` is present. `MetricSample` has no slot to name that parent, so the P2
  plan adds an additive `subject_parent` alias, legal only for `session` subjects and declared in
  `migration_plan.py`'s `wire_additions` (the `waiting_on` precedent). `samples.py:35-43` infers the kind from the `session.`
  prefix as it does for `host.`. The `session_uid` the P1 doors (Half B) write on task events and the
  `subject_uid` on these samples are then the same string.
- **Row shape for the two system events:** the fleet-event shape, built by the one Python helper P1 Half A adds
  (`fleet_event_request`, `claudlobby/plane/fleet_events.py`, plan 2 Task 9b, §6 P1; `source_ref = "fleet-events:…"` as the helper writes it — the
  `otel:<session_uid>:<window>` sub-grammar is dropped, `event_id` already carries the dedup key;
  `data = {"source": "plane-otel", "legacy_ts": …, "data": {…}}`), subject `actor bot:<fleet>/<bot>` when the
  bot is known and the anchor pair `session`/`sess_…` otherwise, so `claudlobby event list --type api_error`
  renders them (`plane-readers.py:1155-1164,1226-1228`). `event_id` is derived (`derive_uid("ev", …)`
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
  produces one per-window event; a Codex `input/cached_input` pair produces uncached `input`; a payload
  with prompt text in `metadata` mode reaches the plane with no content field; a socket miss drops and
  counts; `derive_session_uid("abc", "codex") == entity("session", session_alias("abc", "codex"))` — the test
  pins the composition, not the literal `"codex:abc"`; an unknown metric name
  in the payload never reaches `emit`.

### P3 — Summaries owned by clauDNA; the export consumer

Claudlobby:
- [ ] Per-bot `CLAUDNA_STATE_DIR=$BOT_DIR/data/claudna`, with the cutover per F14. The composer line itself ships earlier, as its own one-line PR before clauDNA 0.27 merges (§9, §10 order 3; ironclad cycle 1); this step verifies it is in place on the canary fleet (plan 6 Task 1) and runs the F14 runbook (plan 6 Task 7, held on A-F14) — within F14(a), the `chmod -R a-w ~/.claudna` step is gated on the "no interactive clauDNA sessions for the service user" condition (or the service user's shell gets its own `CLAUDNA_STATE_DIR` first, verified with one session), and the old root is renamed rather than chmod'ed: the hook exits 0 and logs under a read-only root (`session-store.sh:22-31,43-44`, `cli.py:177-213`), and 0.28's `entrypoint.json` write would fail the same way. A-F14 (§16) is conditional on *why* the roots are per bot: the per-bot line and the `session-export` per-bot grouping ship now (clauDNA spec §1.1 rule 3 already says per bot) and A-F14, if ratified, would revert them; only the runbook is held (interim fold).
- [ ] `CLAUDNA_SESSION_SUMMARY` and `CLAUDNA_HARVEST` as fleet-level opt-ins, since they spend model calls. Add `Switch` rows and regenerate the switch tables (`switches.format_markdown` into `fleet-yaml-schema.md`, `system-yaml-schema.md`, `observable-plane.md`; pinned by `tests/test_switches.py`). Add rows to `documentation/environment-variables.md:183`. The composition matches the summarizer gate (`project.py:276-287`; §4.2): compose `CLAUDNA_SESSION_SUMMARY=1` whenever `harvest: true` (the loader refuses `harvest: true` with `session_summary: false` at config load — a `ValueError` from `_coerce_bot` naming both keys) and `=0` for unset/false, so an unarmed bot's status item reads `skipped.reason: disabled` — the common row — rather than the `headless` the gate yields by default; `fleet-yaml-schema.md` says which reason an unarmed bot produces, and the `claudna-harvest` switch's `what` ("summaries on, typed blocks filed") is what the composition produces (ironclad cycle 1).
- [ ] Bump the Claudron pin `v0.6.1` → `v0.9.0`. Harvest needs ≥0.7.1, and homes ≥0.8. The jump crosses vault migrations (vault format 3 / m003 in 0.7.0, index schema 7 in 0.8.0; Claudron's CHANGELOG says to run `claudron doctor --fix`), so the bump ships with an operator-run step (it writes a git commit to a shared vault — Defaults rule): `claudron doctor --fix --json` once per vault, from one clone, not from every host. Composition then ~~refuses~~ **warns** (`shared.add("claudron-migration", …)` naming the `--fix` line, plus the doctor rung — every host-side probe warns by stated intent, `validator.py:1138-1152,1319-1331`; #2001 made `doctor` surface migrations and never `--fix`) for a vault with **pending migrations or an old vault format**, never for unrelated doctor findings (D009/D010 hook warnings, structure warnings); a hard refusal, if wanted, is scoped to plans that change that bot's session-loop composition or move the pin, with the reason warn-never-fail does not apply recorded in the plan. The probe is gated on `claudron_on_path` and memoized per `validate()` call, well under `doctor`'s 60 s. This is **its own PR (6a), after 6b** (§10.1), with a per-host runbook: upgrade → `claudron doctor --fix` from one clone → `git pull` on each clone → `config plan` (ironclad cycle 1). Canary it on one host first. Touches:
  - `pyproject.toml`;
  - `conformance.yml`;
  - `claudlobby/claudron_compat.py` and `tests/test_claudron_compat.py`;
  - `tests/test_claudron_loop.py::TestSnippetParity` (`:452-500`);
  - `documentation/integrations/claudron-integration.md:7,44,48`.
- [ ] `session-export` fleet job. It invokes the export door at its contracted path (F16):
  - `python3 <entrypoint> export --consumer claudlobby --include-skipped --json`;
  - `<entrypoint>` is read from the file clauDNA's own hook writes into the bot's state dir;
  - the entrypoint file and the invocation are both clauDNA spec §8 contract.

  For each exported segment, including F6's status-only skipped items, it emits the F6 event (deterministic
  `event_id`, so emit-then-ack is safe to re-run) and then `--ack`s — **one `emit_batch` per item** (or a
  ledger pre-read of the derived ids, emitting only the unseen) and an ack per `sid` right after its items
  land, never one batch per bot: a batch mixing a duplicate with a new item rolls back and raises
  `RuntimeError("… mixed state")` (`ingest.py:548-552,598-605`), which `emit_batch` does not spool
  (`emit_api.py:273-280`), so a run that emitted [seg1, seg2] and died before `--ack` would wedge the bot
  forever with the tick exiting 0 (ironclad cycle 1, critical). A deterministic refusal
  (`ContractViolation`/`RuntimeError`) becomes a `status: skipped, skipped_reason: unexportable` item so the
  cursor advances; after N consecutive failures per bot the job emits an `export_stalled` system event at `critical`
  (the `reload_failed` precedent, `registries.py:135-136`) and `doctor` gains a rung for per-bot cursor age. The
  job reads `entrypoint.json` and skips a bot, naming the reason, on `no_entrypoint` (no file), `unknown_schema`
  (`schema` ≠ `claudna.entrypoint/1`), `old_entrypoint` (`plugin_version` missing or `< 0.28.0`) or
  `stale_entrypoint` (the recorded path gone) — a skip, not a failure; a 60 s timeout is `export_timeout` and a
  non-zero exit or non-JSON stdout is `export_failed` (its own counter), both counted as failures — six reasons,
  the same six P3 clauDNA's writer-side text lists (interim fold). The export item also needs the segment's
  `sealed_at`/`sealed_by`/`counts` (additive, the clauDNA P3 PR; plan 5 Task 1, not held — A-F6 would only add to it).
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
- [ ] Export gains `--include-skipped`: status-only items for skipped segments (F6). Default output unchanged (#387's no-item default, preserved); spec §8 — *extending* the "Item fields and rules" table P1 writes (one author per section: rows for `segment`, `skipped` and the status items; grammar, `--since-seg`/`--limit`, ack and additive rules stay where P1 put them — ironclad cycle 1).
- [ ] The store's own SessionStart handling (`boundaries._session_start`, not the briefing hook) writes
  `<CLAUDNA_STATE_DIR>/entrypoint.json` (F16): the absolute path of the store entrypoint for the running
  plugin version, with the join key spelled `runtime` (not `host`) and the staleness semantics the consumer
  relies on stated on the writer side too. Spec §8; tested.
- [ ] Freeze the activity layer per F8, with a spec note. `TestFrozenLayer`'s docstring says the freeze covers the store's hooks and kinds and not `plugin-hooks/telemetry-emit.sh` (Claudosseum's `skill_invocation` writer, its own hook entry by owner decision, phase-4 plan `:97-99`; §11).
- [ ] Child guard reads `CLAUDE_CODE_CHILD_SESSION=1` first, keeping the pid and entrypoint checks as fallbacks. This step waits on P1 Claudlobby Task 7b (Half A; `tests/test_boot_policy_conformance.py`) if C10 shows a leak; the 0.28 release does not wait. If C11's hook-env leg shows a subagent's `PostToolUse` carrying the marker with the parent's `session_id`, the marker check is restricted to the lifecycle events. Update the `SETUP_GUIDE.md` env table (`:309-317`) if any documented semantics change; fix the "Claudlobby sets `CLAUDNA_TELEMETRY=1`" claims (`SETUP_GUIDE.md:704`, `telemetry.py:3`, phase-4 plan `:68` — it never has, `composer.py:1319-1330`); and close Claudlobby#1961's `CLAUDLOBBY_HOOK_CHILD` thread in spec §11.5 (never built; superseded by Claude Code's own marker, spec §4.4 `:145`, §11.5 `:464`).
- [ ] Release clauDNA 0.28.0.

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
 "plugin_version": "0.28.0",
 "python": "/usr/bin/python3",
 "runtime": "claude",
 "written_at": "2026-10-04T12:00:00.000Z"}
```

- Path `<CLAUDNA_STATE_DIR>/entrypoint.json` (`paths.state_root`, `paths.py:51-58`), mode 0600, written
  by atomic replace; rewritten on each opening SessionStart (one small write).
- `entrypoint` is `str(Path(__file__).resolve().parent)` — exactly the package path `spawn_worker` already
  execs as `[sys.executable, "-S", str(package), …]` (`boundaries.py:174-177`); `plugin_root` is
  `package.parents[2]` (cf. `__main__.py:16`); `plugin_version` is read from
  `<plugin_root>/.claude-plugin/plugin.json` (the first time `lib/` reads that file; a missing or
  unparsable manifest leaves the key `null`, never an error); `runtime` is the selected host's name — a host's
  name *is* its runtime value, so the contract surface spells the join key the way `session.opened.runtime` and
  the export item do ("host" stays the adapter-module vocabulary; for Codex, `actor.entrypoint` carries the
  launch mode C1 exposes, or `null`); `written_at` is `ev.now_ts()`'s millisecond form (`events.py:218-221`).
  *(Corrected, ironclad cycle 1: `"host"` → `"runtime"`; `0.27.0` → `0.28.0`, the P3 clauDNA release.)*
- The consumer invocation is `python3 -S <entrypoint> export --consumer <name> [--include-skipped] --json`
  and `… --ack --sid <sid> --through <seg>`. A consumer that finds `entrypoint` missing on disk treats the
  record as **stale** (a plugin version replaced underneath a running session, `reload-fleet.sh`) and
  skips the bot until the next SessionStart rewrites the file; it never guesses another path. **Staleness is
  more than absence** (ironclad cycle 1): Claude Code's plugin cache is per version and nothing establishes
  that the previous directory is removed on update, so a surviving directory would run an *older* store
  against newer files — the consumer requires `plugin_version >= 0.28.0` (an older entrypoint rejects
  `--include-skipped` with exit 2, which no skip would name). The consumer's reasons are six, and P3 clauDNA's
  writer-side text lists the same six (interim fold): `no_entrypoint` (no file), `unknown_schema` (`schema` ≠
  `claudna.entrypoint/1`), `old_entrypoint` (`plugin_version` missing or `< 0.28.0`), `stale_entrypoint` (the
  recorded path gone), `export_timeout`, and `export_failed` (a non-zero exit or non-JSON stdout, with its own
  counter); the P3 canary records whether `claude plugin update`
  leaves the prior `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>` directory.
- Spec §8 gains this record as the second contract surface beside the export envelope; spec §1.1 rule 2 and
  §8 `:440` ("the plane never parses the store's files") are amended to "…never parses the store's *session*
  files; the one file a consumer reads is the published `entrypoint.json`", related to `<claudna-root>`
  (SKILL_CONTRACT §1.1 — the loaded-version answer for a caller inside a session) and `<BOT_DIR>/.claude/session.md`
  (#2094, the same plugin-writes/fleet-reads shape). §8 does **not** say Claudlobby is "drift-gated on their
  side per R3" until a gate exists (§14 Q8); the additive-field rule is new in 0.27 (P1 clauDNA Task 3, citing
  `2026-10-01-session-store-hardening.md:44`). SETUP_GUIDE's env table (`:309-317`) gains no row (nothing to set).

#### Spec: the `session_summary` plane event (F6)

*Added by `/claudna:forge`, 2026-10-04. Grounded in Claudlobby `cd292cb` and clauDNA v0.26.0. Implements
F6(b) as ratified. Two facts the fork's context did not have are recorded here first, because they shape
the payload.*

**What the digest's consumers can actually read today.** The four F6 consumers reach digest rows through
one door, `claudlobby event list --type session_digest` (`fleet-digest/SKILL.md:58-69`;
`fleet-monitoring.md:98`), which `plane-readers.py`'s `fleet_events` serves (`:1238-1279`). That reader
(1) filters `e.source_ref LIKE 'fleet-events:%'` (`:1155-1164,1250-1251`), while `transcript-digest.sh` stamps
`source_ref = "session-digest:<sid>"` (`:340-342`); and (2) projects `data` from the nested
`detail.data` that `emit_fleet_event` writes (`legacy_event_row`, `:1226-1228`; `lib-common.sh:2166-2167`),
while the digest writes a flat `data`. So a `session_digest` row never reaches `fleet-digest` today, and
would render as `data: {}` if it did. Neither side is tested (`tests/test_transcript_digest.sh` asserts on
the staged payload only). The filter landed in #1456, and #1503 then moved the digest onto the plane without a
read-back test — a standing defect, filed independently of P3 (§4.2), not merely context. Consequence: the
replacement event is shaped for that reader, and P3's "repoint the four consumers" is a re-pointing to a door
that works for the first time.

**A `system` row cannot carry the session in its stream column.** `KIND_MANIFEST` allows `session_uid`
only on `kind=task` (`claudlobby/plane/contracts.py:112-116,124-141`; DDL `0011:150`), so the join key
rides inside `data` (queryable with `json_extract`) and the row's subject stays the **actor**
`bot:<fleet>/<bot>` — which is also what gives the consumers their `bot` column. The anchor pair
`subject_kind="session", subject_uid=sess_…` is legal (`contracts.py:464-465`) but would cost the
consumers `bot`; the session-anchored rows are P2's metric samples, not this event. **Revisit trigger**
(ironclad cycle 1, the record F1 keeps for the analogous `Transmission` CHECK): once the P2-b prune makes the
`0011`-style table copy cheap, widen `KIND_MANIFEST["system"].allowed` and the DDL CHECK, then move
`session_uid` from `data` to the column.

**The event.**

| Envelope / row | Value |
|---|---|
| `event_type` | `system`; `payload.event = "session_summary"` |
| `SYSTEM_EVENT_SEVERITY["session_summary"]` | `"notice"` (the registry rule, `registries.py:100-104`: "recorded directly by a writer, notice"); `session_digest` stays registered at `:204` so history classifies (the `shadow_parity_*` precedent, `:114-115`) |
| `emitter` | `session-export` (the P3 fleet job) |
| `source_ref` | `fleet-events:…` as the one Python fleet-event helper writes it (`claudlobby/plane/fleet_events.py`, §6 P1; ~~`fleet-events:session-summary:<sid>/<seg>`~~ — the sub-grammar is dropped, `event_id` carries the dedup key) — the `fleet-events:` prefix is what the reader selects on |
| `event_id` | `derive_uid("ev", f"session_summary:{fleet}:{sid}:{seg}")` (`claudlobby/plane/ids.py:57`) — deterministic, so a job run that re-emits before its `--ack` landed is a `duplicate` success, never a second row |
| `fleet` | the bot's fleet (alias) |
| `payload.subject_kind` / `payload.subject` | `actor` / `bot:<fleet>/<bot>` (alias form, resolved at ingest, `ingest.py:313-322`) |
| `payload.data` | `{"source": "session-export", "legacy_ts": <occurred_at>, "data": {<summary, below>}}` — the nested shape `legacy_event_row` projects |
| `occurred_at` | the segment's `sealed_at` (the fact's time), `observed_at` the export time |

**`data.data` — the summary record** (classified **DIAGNOSTIC with an emitter-side bound** — `journey` is
LLM-authored narrative, already a summary, in a field whose cap is `registries.py:72`'s 16 KiB; every field is
bounded by the emitter, and `session_export.DATA_CAP_BYTES` derives from `registries.cap_for("system", "data")`
minus headroom rather than a hardcoded 12_288, so ingest never truncates `data` into an unparseable prefix —
ironclad cycle 1):

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
`procedures` (count) and `producer` from the summary document; `blocks.kinds` is
`Counter(b["home"] for b in summary["blocks"])` — the block type key is `home`. ~~A skipped item carries the
identity, status, volume and segment fields~~ *(Corrected, ironclad cycle 1:)* a skipped item has
`summary: null` (`export.py:113-114`), so it carries the identity, status and `segment` fields and **`turns`,
`transcript_bytes`, `journey`, `blocks`, `procedures`, `producer` are `null`** — unless A-F6 (§16) is ratified,
in which case the job computes the volume fields from the transcript pointer for every row.

**Re-pointing the four consumers** (`fleet-digest/SKILL.md`, `fleet-observe/SKILL.md`,
`fleet-monitoring.md:102-115`, `ai-platform-monitor.md:22`), field by field:

| Digest field the consumers read | `session_summary` field |
|---|---|
| `status` (`ok · skipped · error`) | `status` (`ok · skipped`; a summarizer that gave up is `skipped` with `skipped_reason: gave_up`; an item the plane deterministically refused is `skipped` with `skipped_reason: unexportable`, the job's own reason — there is no separate `error` class) |
| `session_id`, `bot`, `fleet`, `ts` | `session_id` (+ `session_uid`, `runtime`); `bot`/`fleet`/`ts` on the row as before |
| `turns`, `transcript_bytes` | same names — `null` on a skipped row unless A-F6 (§16) is ratified |
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
positive control at `:323`, so both land in one PR (P3 Claudlobby). The monitor also gains the stop rule for
the window P3 actually produces (ironclad cycle 1): `session-export` ships on, so week one is one status row per
sealed segment with `journey: null` unless a bot is armed — `fleet-digest` stops on "every row `skipped` →
COVERAGE: summaries armed on 0 of N bots" (mirrored in `fleet-observe`, reconciled with its `:47-49`;
`fleet-monitoring.md:39-41` "Nothing watches for sessions ending" joins the edit range), and the "per-session
tool totals are P2's `session.tool_calls`" promise either comes with the `plane samples` grant for `fleet-digest`
(`SKILL.md:5-8`) when the plane leg lands (A-F3) or is dropped. `_session-export-tick --dry-run` is not monitor
advice (a suppressed private command that spawns every bot's export from the monitor's session); the monitor
reads through a read-only door.

**Idempotency and ack.** The job emits one event per exported item, then `--ack --sid <sid> --through
<seg>` (`export.py:135-149`; the cursor never moves back, `store.py:385`). The deterministic `event_id`
makes the emit-then-ack pair safe to re-run: a crash between them re-emits a `duplicate` and acks — **provided a
batch never mixes seen and unseen ids** (ironclad cycle 1, critical): `_verify_duplicates` rolls back a batch in
which one item collides and another is new (`RuntimeError("… mixed state")`, `ingest.py:548-552,598-605`), and
`emit_batch` does not spool that (`emit_api.py:273-280`), so ~~one batch per bot per run~~ becomes **one
`emit_batch` per item** (or a ledger pre-read of the derived ids, emitting only the unseen) with an ack per `sid`
as its items land; the test "emit seg1 without acking, seal seg2, run → seg2 `committed`, seg1 `duplicate`, both
acked" pins it. A deterministic refusal becomes a `skipped_reason: unexportable` status item so the cursor
advances; N consecutive failures per bot raise `export_stalled`; `doctor` reports per-bot cursor age. Emits go
through `emit_batch` in-process (`claudlobby/plane/emit_api.py:149-153`, the Python-door precedent for one-shot
timer ticks and CLI doors — resident services post to the daemon socket instead, `daemon.py:1-30`),
`require_commit=False` so a daemon outage spools rather than fails the job. The row is built by the one
fleet-event helper (`claudlobby/plane/fleet_events.py`, §6 P1), so `source_ref` is `fleet-events:…` as it writes
it (the `session-summary:<sid>/<seg>` sub-grammar is dropped; `event_id` carries the dedup key).

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
  - `hooks install --host codex` (name settled in P4: `--host` clashes with Claudron's existing `host` term;
    `--front-end`/`--agent` are candidates);
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
stays as it is (F10) — (a) covers the **model call**; the transcript **reader** is per runtime and is selected
*inside the detached worker* by the session's recorded `runtime` (`session.json.runtime`, P1): the summarizer
(`summarize.py:184`, `read_range`'s only caller), harvest's `resummarize` and the sweep's `close_abandoned` have
no `--host`, and `_seal` reads no transcript (it records `file_size`, `boundaries.py:247-254`). A Codex-only
host means summaries stay off — its status items read `disabled`/`headless` (§6 P3) — and no vendor canary is
needed unless (b) is taken; the revisit trigger is a Codex-only host that wants summaries (§14 Q10). A-F10
(§16) proposes an additive per-host runner. (Ironclad cycle 1; F10's decision unchanged.)

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
    read_range: Callable[[Path, int, Optional[int]], list]       # transcript reader (path, start, end) → list[Turn]; consumed by the detached worker (summarize.py:184), which selects HOSTS[session.json.runtime] — never by the hook path

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
5. `_seal` (`:247-254`) ~~reads the transcript through `host.read_range`~~ records only the transcript's size
   (`file_size(transcript_path)`) — it reads nothing; `host.read_range` is consumed by the detached worker, which
   selects `HOSTS[runtime].read_range` from the session's recorded `runtime` for the summarizer, `resummarize`
   and `close_abandoned` (corrected, ironclad cycle 1; the field stays on `Host`, the consumer is the worker).
   `_record_activity` (`:257-275`) passes `n.activity` to `activity.event_for`.

The owner pid has three readers that all go through the recorded `claude_pid` (F13): the guard, the clear
link (`links/<pid>.json`, `lineage.py:26-27,73-97`) and the abandoned-session sweep (`unclosed.py:67,101-110`,
`os.kill(pid, 0)`). Whatever C2 finds for Codex must serve all three.

**Selection (C9): by the hook command, never by sniffing** — for the hook path; the detached worker dispatches
on `session.json.runtime` (above). "Host" is clauDNA's adapter-module vocabulary, and a host's name is its
runtime value; Claudron's `--host codex` would overload a term D009/D010 already use for machine/settings file,
so the P4 Claudron plan picks `--front-end codex` or `--agent codex`, or documents the overload (§5.1).

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
| `read_range` | the rollout reader over the C4 record types, replacing both `turn_of` and the byte prefilter; selected in the detached worker by `session.json.runtime` | C4 |

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
  - the Collector config passes `otelcol-contrib validate` — a required CI leg with a pinned binary version
    while F4 stands (A-F4 would remove it);
  - the clauDNA→Claudlobby export contract gets the drift gate every sibling contract has (§14 Q8):
    `conformance.py`'s clauDNA leg running the real `session_store export --include-skipped --json` on a fixture
    store through `session_export.summary_record`, and/or a published `schemas/export.schema.json` vendored by
    Claudlobby;
  - P2-b: the harness marker scenarios plus 48 h of unchanged keepalive and fleet-pulse verdicts on the canary
    root; the intake's straddle test; `session-export`'s seg1-unacked test (§6 P3).
- Contract tests: clauDNA's live suite covers runtime-qualified provenance and export `runtime`/skipped items, and Claudron's consumers job runs it.
- Fixtures: a recorded Codex hook payload per event (C1) and a Codex rollout excerpt (C4), both redacted;
  one OTLP/JSON window per runtime from C5/C6 for the intake. Every externally produced shape is grounded in
  a live capture, never in the producer's source (Claudlobby's fixture rule).
- Canary-root observation for every runtime-behavior PR in P1–P3. A cold-host onboarding run when the Collector install lands. The P2 canary criteria are recorded in the run log.

## 8. Verification checklist

- [ ] `task show` names the session that worked on a task, for a Claude bot, and `plane-lookup.py --session <sess_uid>` joins on it (the raw `session_id` beside it only if A-F1 is ratified, §16). A Codex bot follows in the companion.
- [ ] One query over `state/otel/` answers "what did session X cost and which tools failed" — the `jq` is written once in `observable-plane.md` and run as a P2 canary step, starting from what `task show` prints when P1 Half B has landed; until then from `derive_session_uid(hook session_id)` directly.
- [ ] The first reader of the intra-session layer is named before P2-a opens, and the allowlist is sized to it (ironclad cycle 1): a fleet-pulse rung reading `api_error` as the `rate_limit` instrument `fleet-observability.md:94` says does not exist, `brief_read.py:114`/`usage_read.py:39` repointed *inside* P2, or the `fleet-digest` `plane samples` grant with a `plane samples session.tool_calls` step. The plane leg lands with that reader (A-F3).
- [ ] Fleet-level RSS/CPU on the P2 canary host, before and after, within the Pi 5 baseline; the canary RC/Telegram bot answers an inbound message on day 1 and day 7.
- [ ] 48 h keepalive/restart-count observation for P1 Half B on the canary root.
- [ ] `PROJECT_MISSION.md` amended and cited — Claudlobby (F18/D1) and clauDNA (D2) — before the P1 PRs that depend on them merge.
- [ ] `tool_call` rows stop arriving (P2-b), while keepalive busy detection and fleet-pulse idle checks are unchanged.
- [ ] No composed bot carries a `transcript-digest.sh` hook. The F6 event, including skipped sessions, feeds the four consumers.
- [ ] A Codex session fixture segments, summarizes and harvests with a runtime-qualified ref.
- [ ] No fleet feature reads a signal listed in §2.3.

## 9. Risks

- **Collector on the smallest host:** memory and CPU under a full fleet — ~4 % of a Pi 5's usable RAM with the intake (`fleet-memory-planning.md:43-54`) against the mission metric "Resource efficiency holding on the Pi 5 baseline" (`PROJECT_MISSION.md:104`). Covered by the P2 canary budget — held against the *fleet-level* baseline, not per component (ironclad cycle 1) — and by F4 keeping it opt-in; A-F4 (§16) removes the Collector.
- **Vendor drift:** Claude's trace schema is in beta and Codex telemetry is young. Only logs and metrics are load-bearing here, and the Collector config absorbs renames in one place.
- **Codex hook details differ** (C1–C4). Each adapter documents what it cannot hold rather than approximating it.
- **Older readers sharing state:** the clauDNA field placement and projection steps in P1 exist for this reason.
- **Codex hosts need Claude for summaries** (F10).
- **Volume moves rather than disappears:** `state/otel/` needs its own retention and a total bound (`max_megabytes × max_backups × 2 files` ≤ 2 GB, the Collector spec), 0700/0600 modes, and a doctor rung.
- **Marketplace auto-update defeats the release ordering** (ironclad cycle 1, compatibility). `start-bot.sh:315-321` runs `claude plugin update` through `plugin_ensure` at every bot start (or once per host boot), and `CLAUDNA_VERSION` is composed with no consumer (`composer.py:1319-1324`; `plugin_ensure` greps the registry for the name only, `lib-common.sh:4699-4710`), so clauDNA 0.27 and 0.28 reach tmux-hosted bots at their next restart — weeks before P3 composes the per-bot root — a fleet shares one `~/.claudna` with mixed 0.26/0.27 writers (the one-rebuild-each convergence of §6 P1 holds, fleet-wide), and the plan's C10 ordering is unenforceable the same way. "Pinned" means nothing until something consumes the pin. **Remedy:** pin `claudna_version` on the fleet before merging the clauDNA P3 release — `plugin_ensure` honours `CLAUDNA_VERSION`, or the marketplace entry is pinned by `sha`/tag per clauDNA `CONTRIBUTING.md:140` — or ship the state-dir line first (§10, order 3).
- **Two foundation costs are paid on purpose** (first-principles): the strict `RequestIntent(**intent)` decoder makes every new intent field a receipt-format bump (F17; `request_receipts.py:436-450`), and `session.schema.json`'s `additionalProperties: false` makes one optional field a projection bump (`claudna.session/2`, §6 P1). Both are the price of refusing unknown fields at a boundary; neither is loosened here.
- **Calendar gates the size table does not price** (§10): the one-week P2 canary, the three-repo release chain (clauDNA 0.27 → 0.28, Claudron, the pin bump), and the Codex install (§14 Q3). The plane's own history (v2 → F18 cutover in 18 days, 13 post-ship fixes) says that is where schedule risk sits.

## 10. Complexity and sequencing

| Phase | Size | Repos | Release gates |
|---|---|---|---|
| P0 | ~~S~~ **M** (eleven canaries, eight needing a Codex install; two batches, §6 P0) | all | — |
| P1 | M (P1 Claudlobby ships Half A, unheld, then Half B, held on A-F17/A-F1; §10.1) | Claudron (spec, prep), Claudlobby, clauDNA | clauDNA 0.27 release |
| P2-b | **S** (independent; first) | Claudlobby | — |
| P2-a | **L** (held on A-F4, A-F4b, A-F3, A-F5; §16) | Claudlobby | — |
| P3 | M (P3 Claudlobby is M → L and splits 6b/6a; only its F14 runbook is held, on A-F14; §10.1) | Claudlobby, clauDNA | clauDNA 0.28 release; Claudron pin bump (6a) |
| P4 | L | clauDNA, Claudron, Claudlobby (adapters) | clauDNA and Claudron releases |
| Companion | L | Claudlobby | needs P1, P4 releases |

*(Ironclad cycle 1: P0 was S; P2 was one L row. The forge order below is struck and replaced.)*

~~Suggested PR order: 1. The Claudron boundary-spec PR. 2. The Claudlobby runtime field and derivation. 3. The
clauDNA runtime-at-open change, then its release. 4. The Claudlobby session uid on its doors. 5. P2 and P3 in
parallel. 6. P4 per repo.~~

PR order (ironclad cycle 1; packaging and holds per the interim fold):
1. P0, the Claude-only batch (C6, C10, C11).
2. **P2-b** — marker-only `bot-vitals.sh` with the #874 fix (plan 4 Tasks 1–2; S; §6 P2).
3. **Per-bot `CLAUDNA_STATE_DIR="$BOT_DIR/data/claudna"`** — one composer line and one test, its own Claudlobby PR,
   before clauDNA 0.27 merges (§9); the earliest Claudlobby item. It ships now; A-F14 (§16), if ratified, would
   revert it.
4. The Claudron boundary-spec PR (plan 1).
5. **P1 Claudlobby Half A** (plan 2, release N; unheld): the runtime field and derivation (Tasks 1–5), the receipt
   decoder (Task 6 Step 2a — forward-compatible), the C10 scrub if C10 leaks (Task 7b), the fleet-event helper
   (Task 9b) and the row-10 flip. Then **Half B** (release N+1, after every host has activated N): the writer flip
   (Task 6 Step 2b), the doors and readers (Tasks 7–9), their docs and the door observation — held on A-F17/A-F1.
6. The clauDNA runtime-at-open change (plan 3), then its 0.27 release — with the mission amendments (D1 before
   plan 2's Half A merges; D2 before 0.27 ships).
7. In parallel: **P2-a** (plan 4 Tasks 3–9, on Half A; held on A-F4, A-F4b, A-F3, A-F5) ∥ (**P3 clauDNA** 0.28
   (plan 5; Task 1 not held — A-F6 is additive) → **P3 Claudlobby 6b** (plan 6, on Half A and 0.28; Tasks 1–2 and
   4–8, of which only Task 7, the F14 runbook, is held — A-F14)).
8. **P3 Claudlobby 6a** — the Claudron pin bump, its own PR after 6b, with the per-host runbook.
9. P0, the Codex batch (install → step 0 → C1–C5, C7–C9).
10. P4 per repo.
11. Companions (#2149, clauDNA#404).

**The schedule's spine is the Codex critical path:** install → C1–C9 → P4 → #2149. P2 and P3 are
parallel-optional to it — they pay for a Claude-only fleet (§1) and neither blocks P4. The calendar gates the size
table does not price are listed in §9.

### 10.1 Per-PR plans (forge, 2026-10-04)

One plan per PR beside this file, each with `epic:` pointing here. P4 and the companions get theirs when
§5.1's canary answers are in the run log.

| Order | Plan | Repo | Size |
|---|---|---|---|
| 1 | `2026-10-04-runtime-neutral-observability-p1-claudron-boundary-spec.md` | Claudron | S |
| 2 | `2026-10-04-runtime-neutral-observability-p1-claudlobby-runtime-and-join-key.md` | Claudlobby | M — Half A (release N: Tasks 1–5, Task 6 Step 2a, 7b, 9b, the row-10 flip; unheld), then Half B (release N+1: Task 6 Step 2b, Tasks 7–9, their docs and observation; held on A-F17/A-F1) |
| 3 | `2026-10-04-runtime-neutral-observability-p1-claudna-runtime-at-open.md` | clauDNA | M |
| 4 | `2026-10-04-runtime-neutral-observability-p2-otel-pipeline.md` | Claudlobby | ~~L~~ P2-b S (Tasks 1–2, first) + P2-a L (Tasks 3–9; held: Tasks 3, 4, 7, 8, 9 on A-F4, Task 4's OTTL block also on A-F5, Task 5 on A-F4b, Task 6 Steps 3–5 on A-F3) |
| 5 | `2026-10-04-runtime-neutral-observability-p3-claudna-export-contract.md` | clauDNA | M (nearer S–M); not held — A-F6 would add a key to Task 1's `segment` object, an additive follow-up |
| 6 | `2026-10-04-runtime-neutral-observability-p3-claudlobby-summaries.md` | Claudlobby | ~~M → L (eight tasks; seam after the Claudron pin)~~ two PRs: 6b M (Tasks 1–2, 4–8; only Task 7, the F14 runbook, is held — A-F14), then 6a S (Task 3, the pin bump) |

Order 2 splits at its marked seam, and the split is required at the release level (F17 decoder-first): Half A
carries everything unheld — the decoder, Task 7b and Task 9b among it, which P3 clauDNA's Task 5, P2-a and P3
Claudlobby consume — and Half B only what waits on A-F17/A-F1 (interim fold). Plan 5 releases clauDNA before
plan 6 can consume it. *(Ironclad cycle 1:)* the per-bot `CLAUDNA_STATE_DIR` one-liner (§10 order 3) needs no
plan file — one composer line, one test, a CHANGELOG line — and plan 6's Task 1 becomes a verification that it
landed. Plans 4 and 6 edit the same files (`test_event_type_registry.py`, `fleet-observability.md`, the switch
tables, `system.yaml`, `observable-plane.md`, both `CLAUDE.md`/`AGENTS.md` pairs) with no ordering between P2
and P3: whichever merges second rebases, regenerates the three switch tables and `system.yaml.example`,
re-copies both `AGENTS.md`, and re-runs `tests/test_instruction_budget.py`, `tests/test_switches.py` and
`tests/test_event_type_registry.py` before pushing; the fleet-event helper (§6 P1) lands with P1 Half A (plan 2
Task 9b) and both consume it. Plans 4–6 cite pre-dependency line numbers and carry plan 1's caveat ("line numbers are pre-edit; edit
bottom-up or re-grep"). Every plan implementing a contested option carries a `> **Held:**` line at the top of
the affected task (§16), no unheld work rides a PR that waits on an amendment (interim fold), and each plan names
the mission decision it depends on (D1/D2).

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
  `none` or the local Collector, and `[analytics] enabled = false` — for **every** Codex session this epic runs,
  the P0 canaries included (step 0), not only the companion's composed bots.
- Don't let the plane mint a random uid for a `session` subject; derive it (P2 spec) or the §2.2 join fails.
- Don't rely on ingest truncation for `session_summary`: a truncated `data` is an unparseable prefix. Bound
  the payload in the emitter.
- Don't put the `entrypoint.json` write in the briefing hook; bots turn that hook off.
- Don't rename the Collector's metric *names* onto semconv; normalize attributes, aggregate in the intake.
- Don't mix seen and unseen ids in one `emit_batch` (the intake's windows, `session-export`'s items): ingest rolls
  a mixed batch back and nothing spools it (ironclad cycle 1).
- Don't count `plugin-hooks/telemetry-emit.sh` → `telemetry.py` (Claudosseum's `skill_invocation` line) inside
  F8's freeze or this epic: it is a Claudosseum contract under clauDNA's approval gate — out of scope here, or a
  P2 allowlist candidate; after P2 one skill call is otherwise recorded three times.
- Don't `chmod -R a-w` a clauDNA root that a service user's interactive sessions still resolve (the F14 runbook):
  the hook exits 0 and logs under it; rename the root, and gate the step on the no-interactive-sessions condition.

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
   ~~`/claudna:ironclad` next.~~ Cycle 1 done 2026-10-05 (§15, §16); the operator rules on §16's amendments and
   decisions (D1/D2 before the P1 Claudlobby and clauDNA PRs merge), then cycle 2 or the fold closes.
3. P0 runs as two batches (§6 P0; step 0 first on the Codex side); sub-issues per phase per repo are opened
   from the epic.
4. Register rows 10–12 flip from *planned* to *shipped* by one Claudron PR each, from the plan that ships the
   surface: row 10 (the join key) from P1 Claudlobby Half A (Task 10's row-10 step: `session_alias`/`derive_session_uid`
   and `observable-plane.md` ship in release N), row 11 (the export contract's new fields) from P3
   clauDNA Task 9, row 12 (the Codex session-loop snippet) from P4 Claudron. One ADR in
   `documentation/decisions/` (the house `supersedes_scope` form, `2026-07-18-claudron-consumption-door.md:9`)
   records F18 and the v2 §19 ruling this plan supersedes, or the operator states the reason for none; the v2
   §19 entry itself is P1 Claudlobby's (§1.1).

## 14. Open questions (forge, 2026-10-04)

Questions the code could not answer; each has a lean, and the operator decides.

1. **The `fleet.yaml` key for the runtime.** `runtime:` (this plan) or `agent_cli:` (#1997, which documents the
   naming clash). Lean: `runtime:`, documented against `runtime/`; the cross-repo vocabulary is already in F2/F9.
   Whichever is chosen, the one-mapping-sentence approach is right; `CLAUDLOBBY_RUNTIME` joins a crowded
   namespace (extension-check).
2. **`PROJECT_MISSION.md:114` — and `:5`/`:11`, `README.md:3,12`, `CLAUDE.md:3`.** They exclude per-bot provider
   abstraction and say Claude Code; no fork in F1–F17 decides that Claudlobby composes Codex bots (F11 decides
   *where* Codex launching lives, not *whether*). ~~Amend it in the P1 Claudlobby PR, or record that F11's
   ratification supersedes it?~~ The "F11 supersedes" option is withdrawn (ironclad cycle 1; D1 in §16). Ratify
   **F18** (or an operator decision record) cited from the mission in the `:17` form (#515), and amend every line
   coherently in P1 Claudlobby's Half A PR before it merges, coordinated with `origin/codex/974-mission-consolidation`.
   Lean: F18 as worded in §16.
3. **Where the canaries run.** Codex is not installed on the operator's machine (the Homebrew cask `codex`
   is available). C1–C5 and C7–C9 need it; C6, C10 and C11 need only Claude Code.
4. **The P2 budget figures.** The Collector-and-intake spec proposes RSS ≤ 192 MiB and ≤ 3 % of a core for the
   Collector on the smallest host class; C6 replaces them with measurements before the one-week canary starts.
5. **Session metric semantics.** The plane stores *uncached* input tokens so both runtimes compare, which
   departs from semconv's inclusive `gen_ai.usage.input_tokens` (the Collector still emits the semconv value
   in the raw files). Confirm, or store the inclusive figure and derive the uncached one.
6. **`api_error` severity.** `notice` keeps it out of `event list --critical` and the brief; `critical` would
   page and would also have to join fleet-pulse's lists. Lean: `notice`, revisit on the P2 canary's volume.

Added by ironclad cycle 1 (2026-10-05) — carried to the operator; answered where the code could:

7. **F1 — land (a) before (c)?** (A-F1, §16.) The receiver hook holds the executing session's id and needs no
   CHECK change, receipt or C11; (c) gives per-act attribution later. Lean: the amendment's.
8. **A drift gate for the clauDNA→Claudlobby export contract** (precedent-check). Every sibling contract has one
   (`tests/test_claudron_loop.py`; `contracts/claudron.json` + consumer CI #223; `conformance.py`'s pinned clauDNA
   checkout); this one would be tested against canned envelopes and a stub `__main__.py`. Extend `conformance.py`'s
   clauDNA leg (the real `session_store export --include-skipped --json` on a fixture store, through
   `session_export.summary_record`), have clauDNA publish `schemas/export.schema.json` for Claudlobby to vendor
   (its `tests/test_session_store.py:648-655` walk gates every schema file), or both? Lean: both — the schema for
   shape, the conformance leg for behaviour. Until one exists, spec §8 does not claim the contract is drift-gated.
9. **Does any Claudosseum ingestion read the plane's `tool_call` rows?** `validating-bot-changes.md:80-82` says
   Claudlobby emits telemetry "for it to consume", and the #1659/#1744 census swept Claudlobby only. Answer before
   P2-b merges. Lean: none does — but no Claudosseum checkout was available to this review; confirm there.
10. **Is a Codex-only host (no Claude bots) foreseen within this epic's horizon?** It decides F10's "a Codex-only
    host means (b), or summaries off" consequence (A-F10) and whether the normalization layer's two-vocabulary
    work is ever exercised where one runtime runs. Lean: no.
11. **Design v2 §12 item 2's outbound "export through OTel"** (`:542`): superseded, deferred or still intended,
    now that the direction is inverted (OTel → plane intake)? One clause in the §1.1 amendment / P1 Claudlobby
    Task 5. Lean: deferred (§1.1 says so pending this answer).
12. **Say the join is *restored*?** `lib/report-back.sh` wrote `session_uid` from `.plane-session` (`86b4a987`,
    #1372) until `c4682f7a` (#1989) deleted it, unrecorded (§4.2; `_runtime_scripts/CLAUDE.md:116` still
    describes the script). Should the v2 §19 entry and the P1 CHANGELOG say so? Lean: yes, one sentence each.
13. **Who reads `gen_ai.*` names?** Metric names stay vendor-prefixed and the intake aggregates per runtime, so
    the semconv names exist only in the raw files and the mapping, pinned to a tagless commit. If the only reader
    is a human with `jq`, house names (F5(b)) are cheaper; if an external tool is anticipated, name it (A-F5).
    Lean: A-F5 — scope F5 to the intake's mapping.
14. **Why does `journey.arc` ride into the plane, and why does `session-export` ship ON?** The four F6 consumers
    have read nothing since the digest moved to the plane (#1503), the digest is opt-in (`switches.py:529-540`),
    and the arc (≤ 2,000 chars) is "what a session meant" — the layer §2.1 reserves for clauDNA — in a plane row;
    a thin row (identity, status, counts, `journey.title`) fetched through the export door is the alternative.
    Lean: the consumers are manager-bot skills whose only door is `claudlobby event list`
    (`fleet-digest/SKILL.md:58-69`), which is why the arc rides in the row — if that is the reason, it goes into
    F6's context at the next fold; `session-export` ON stands on the Defaults rule (it deletes nothing and spends
    nothing — the model calls are the opt-in `claudna:` knobs), with the monitor's new stop rule (§6 P3) keeping
    week one cheap.

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

### Cycle 1 fold (ironclad, 2026-10-05)

Folded by `/claudna:forge --reforge` from the seven-lens review of #2144 (147 → 76 findings after dedup). Forks
unchanged; each affected fork carries an *Ironclad cycle 1* pointer; the amendments and decisions are in §16.

- **Spec corrections.** F17 (§6 P1): the "never restamped" premise was false — decoder-first two-release
  sequencing, `_save` stamps the current `FORMAT_VERSION`, a `host migrate` preview test for N+1 → N, the permanent
  rollback blocker disclosed, the test leg that reaches the code (prepared, not committed). P4 host adapter:
  `_seal` records offsets only; `read_range` is consumed by the detached worker, selected by `session.json.runtime`;
  the `--host` overload noted. F6 `session_summary`: a skipped item's `turns`/`transcript_bytes`/`journey`/`blocks`/
  `procedures`/`producer` are `null` unless A-F6; DIAGNOSTIC with an emitter-side bound; the KIND_MANIFEST revisit
  trigger; `blocks.kinds` over `home`; `unexportable`; #1456/#1503 cited; the monitor's second stop rule.
  Collector: the raw sink's total bound (`max_backups: 15`, ≤ 2 GB), `UMask=0077`/0700, the doctor rung;
  `otelcol-contrib validate` a required CI leg with a pinned binary while F4 stands. F4 spec: `plane-otel` gets its
  own switch row; both `why_opt_in` reasons in admitted categories; `27813876` cited; `content: metadata`
  reconciled with v2 F7/F23. Intake: `daemon.send_batch`, 200 before forwarding, `session_alias` for `session`
  subjects, per-60 s events with one bounded streak counter, never-mix batches, the `sidechain` bucket,
  `active_time_s` enrichment-only, the fleet-event helper's `source_ref`. Export contract: `entrypoint.json`
  carries `runtime`, `0.28.0`, millisecond `written_at`; staleness semantics (`old_entrypoint`, `export_failed`);
  rule 2 amended; no "drift-gated" claim. `session-export`: one `emit_batch` per item, ack per `sid`,
  `unexportable`, `export_stalled`, the doctor rung. The `claudna:` knobs match the summarizer gate (`=1` with
  harvest, `=0` otherwise → `disabled`). The Claudron pin bump warns rather than refuses and is its own PR (6a).
  F15: the pre-registration named and withdrawn; the never-rendered ground. §2.1 and the P1 Claudron bullet word
  the bought layer mechanism-neutrally. `BotConfig.runtime` is `str` + `_parse_enum`. `transcript_usage.py` does
  not retire.
- **Sequencing.** P0 is M and runs as two batches with step 0 (the Codex telemetry preflight) before C1; P2 splits
  into P2-b (S, first, independent; rebased on `fix/2140`) and P2-a (L, held on A-F4); the per-bot
  `CLAUDNA_STATE_DIR` line ships first (§9's auto-update risk); P3 Claudlobby splits 6b/6a; the Codex critical path
  is the spine (§10); plans 4 and 6 carry a merge-order rule (§10.1).
- **Added.** §1's stand-alone paragraph; §1.1's `:63` and v2 §12 item 2 clauses; §4.2 (evidence) and the §4
  Claudron correction; the header's `fix/2140` anchor note; §5.1's prior art and the canary mirror; C1/C4/C6/C10
  lines; `session_alias`, the fleet-event helper, `plane-lookup.py --session`, the 48 h door observation and the
  mission steps (D1, D2) in P1; §7/§8 items (the named reader, the `jq`, fleet RSS/CPU, the drift gate, the
  missions); §9's four risks; §11's four lines; §13's row flips and ADR; §14 Q1/Q2 rewritten, Q7–Q14 added; this
  entry and §16.
- **Carried to the operator** (answered where the code could): §14 Q7–Q14; §16's amendments and D1/D2; whether
  the P2-a Collector is kept (A-F4), which decides A-F5's fate too. Not applied: nothing in §3 beyond the one-line
  pointers (the F10/F15/F16/F17 context corrections live in §4.2 and the §6 specs instead); F14's context is not
  amended because the operator's reason for per-bot roots is not on record (A-F14 asks for it).

### Interim fold (ironclad cycle 1.5, 2026-10-05)

Folded by `/claudna:forge --reforge` from two sweeps run after the cycle-1 fold commit (`5df2429b`): a Held-task ×
cross-plan dependency sweep (unheld work was riding PRs that wait on amendments, and downstream plans depended on
it unconditionally) and a cross-document naming check (one name, several spellings). No fork's Options, Decision,
Lean or Status line changed; no amendment was applied or added.

- **Packaging — unheld work never rides a PR that waits on an amendment.** P1 Claudlobby Half A (release N) = plan 2
  Tasks 1–5, Task 6 Step 2a (the decoder, forward-compatible, not held), Task 7b (the conditional C10 scrub),
  Task 9b (`fleet_event_request`) and the row-10 flip; Half B (release N+1) = Task 6 Step 2b and Tasks 7–9 with
  their docs and the door observation, held on A-F17/A-F1. P2-a and P3 Claudlobby depend on Half A; P3 clauDNA's
  guard waits on Task 7b. Plan 5 Task 1 is not held (A-F6 is additive). Plan 6 Task 7, the F14 runbook, is the only
  held P3 Claudlobby task: the per-bot root and the `session-export` per-bot grouping ship now, and A-F14 would
  revert them. P2-a's holds are A-F4, A-F4b, A-F3 and A-F5. §6 (order, P0, P1, P2, P3), §8, §10, §10.1, §13, §14 Q2,
  D1 and the §16 "if ratified" lists follow.
- **Stale task numbers.** §10.1 (P2-b Tasks 1–2, P2-a Tasks 3–9; plan 6 Task 1 is the per-bot root) and §16 (A-F3:
  P2 Task 9; A-F4: the intake is P2 Task 6, the Collector parts of Tasks 3–5; A-F4b: Task 5; A-F5: Task 4; A-F6:
  plan 5 Task 1; A-F14: plan 6 Task 1).
- **One spelling per name.** The C10 scrub is P1 Claudlobby Task 7b (no "P3 Claudlobby Task 6b" exists);
  `plane-lookup.py --session <sess_uid>` only (the raw id beside it is what A-F1 would add); `tool_failure_streak`
  counts inside one 60 s window (2+2 → none, 3+1 → one; the cross-window counter is gone); no `sidechain` bucket;
  the six `entrypoint.json` skip reasons; the loader, not the validator, refuses `harvest` without
  `session_summary`; `skipped.reason` on export items, `skipped_reason` on the event; `_otel_cfg` beside the
  `_otel` module alias; `api_error` per window, not debounced; `claudlobby.fleet=<fleet name>`, never a uid;
  `hooks install --host codex` with P4's naming caveat; the hook selector before the event (C9); `export_stalled`
  a system event; no payload-schema bump for `BotPayload.runtime`; `session_alias(…, runtime="claude")`;
  `send_batch`'s signature; the telemetry spec on P2's generic strict-mapping helpers, `KNOWN_TELEMETRY_CONTENT`
  and `field(default_factory=…)`; the Collector binary resolved at compose time into `otelcol-bin`; `plane=True` on
  both service rows and `plane-otel` in the opt-in allowlist; the F17 resolver sketch names `caller_session_uid()`
  and its codex → `None` rule.
- **Citations re-verified at `cd292cb`:** `ids.py:25-28` (the `process_uid` comment), `lib-common.sh:2117-2173`
  (`emit_fleet_event`) and `:2166-2167` (its nested row), `daemon.py:917-941` (`send_batch`), `composer.py:4040-4178`
  (`_write_service_units`), `fleet-memory-check.sh:82-89`, `plane-readers.py:1238-1279` (`fleet_events`),
  `:1250-1251` and `:1226-1228` (the filter and the nested projection), `TestSnippetParity` `:452-500`,
  `tests/test_switches.py:99-178` (the opt-in allowlist); `plane/daemon.py:749-752` and `samples.py:24,35-43`
  confirmed as cited.

## 16. Proposed fork amendments (ironclad cycle 1, 2026-10-05)

*Recorded by `/claudna:forge --reforge` from the seven-lens review of #2144 (147 → 76 findings after dedup).
Ratified forks F1–F17 stand as written in §3; every per-PR plan implementing a contested option keeps its steps
and carries a `> **Held:**` line at the top of the affected task until the operator rules. Each entry gives the
proposal, the evidence, the lenses for and against, and what changes in which plan if ratified. A ratified
amendment is a `[FORK-REOPEN F<n>]` on new information (`library/protocols/decision-fork-lifecycle.md:55,68-69`),
not a re-argument of the original position. Lens keys: AR adversarial-review, AM align-to-mission, FP
first-principles, CB cost-benefit, EC extension-check, PC precedent-check, PH plan-health-audit.*

**A-F4 — Drop the Collector from P2; the bots export straight to `plane-otel`. Status: proposed amendment.**
- Proposal: the per-bot `telemetry` mapping composes OTLP/HTTP-JSON export straight to `plane-otel`, the single
  enrolled host service, which writes the rotated raw JSONL under `state/otel/` and applies the allowlist;
  `otelcol-contrib` stays a documented optional pass-through (for a protocol gap C5 finds, or a second OTLP
  consumer), never a prerequisite. The decision pins to C5/C6's "exports OTLP/JSON to a configurable endpoint"
  line. F3(b) — raw files plus an allowlist — and F5 survive in structure; the normalization lives in the intake's
  mapping (A-F5).
- Evidence: both runtimes export OTLP/HTTP JSON *(doc; C5/C6 confirm)*; the intake must parse OTLP/JSON and map
  attributes anyway (P2 Task 6), so the Collector spec's OTTL statements duplicate a Python dict; what the
  Collector uniquely adds — a queue on a loopback hop, gRPC bridging — nothing needs. Its cost: a ~100 MB
  operator-installed binary (§4.1), ≤ 192 MiB / 3 % of a core resident (the P2 budget; with the intake ~4 % of a
  Pi 5's usable RAM, `fleet-memory-planning.md:43-54`, against `PROJECT_MISSION.md:104`), an alpha `file`
  exporter, a tagless semconv pin, and about half of P2-a's L. Design v2 §19 item 8 classes a hop like this as a
  process choice decidable on floor-host measurements, not model shape.
- Lenses: for — FP, CB, AR; AM's resource concern. Against — none argued for keeping it. If (c) stands, the costs
  are recorded as accepted and `otelcol-contrib validate` is a required CI leg with a pinned binary (§6 P2, done).
- If ratified: plan 4 — the Collector parts of Tasks 3, 4 and 5 go (the `otel-collector` switch row; the Collector
  unit and launcher, the rendered `collector.yaml`, its `validate` leg and the `host setup` refusal; the
  Collector-enrollment check), while Task 6 Steps 3–5 are A-F3's; the intake gains the
  raw-file writer (rotation, the total bound, `UMask=0077`, the 0700 directory, the doctor rung) and the
  `telemetry` composition points `OTEL_EXPORTER_OTLP_ENDPOINT` at `plane-otel`'s port; §2.4's diagram, the two §6
  P2 specs and F4's "Collector" bullet are re-specified by a forge pass (the F4 context's "no Collector running"
  becomes "no intake enrolled"); the boundary-spec §10.2 row needs no re-amendment (mechanism-neutral wording,
  plan 1 Task 1 Step 3). A-F5 follows automatically.

**A-F4b — The plan-time refusal becomes a warning. Status: proposed amendment.**
- Proposal: downgrade the ratified bullet "`config plan` fails when a bot has telemetry enabled on a host whose
  Collector is not enrolled" to a named warning (`shared.add(…)`, the validator's host-probe form) plus the
  `host doctor` rung and the `job-inert` pairing warning P2 already adds — or confine the error to `config plan`
  alone, never `config validate` or `doctor`.
- Evidence: every host-side validator probe is warn-level by stated intent (`validator.py:1138-1152` "Warn (never
  fail)", `:1319-1331` "Warn, never error"); the same `validate()` backs `config plan`, `config validate` and
  `doctor` (`doctor.py:1373-1377`, `config_validation.py:124`), so one host's enrollment state would refuse CI and
  other hosts; this would be the validator's first host-state `PlanError`.
- Lenses: for — PC, AR. Against — the ratified text's own ground (F4 context): an enabled bot with no Collector
  exports to an unbound port; the §6 P2 spec records "all three refuse together — intended".
- If ratified: plan 4 Task 5 — the rung appends a warning, not `report.errors`; the `config plan` test asserts the
  warning, not `PlanError`; F4's bullet 3 is amended; the §6 P2 "`config plan` check" block is rewritten.

**A-F17 — Adopt (c): leave `session_uid` out of the hashed projection. Status: proposed amendment.**
- Proposal: `expected_fact` pops `session_uid`/`sender_session_uid` beside the clock columns it already pops as
  "observations, not retry semantics" (`request_facts.py:31-32,48-49`); `reconcile_facts` hashes each receipt's own
  `fields` tuple; plan 2 Task 6 (receipt format 2) is dropped — no format bump, no rollback blocker.
- Evidence: under (a) the first v2 receipt blocks `host migrate` to every pre-P1 release for as long as it is
  retained (`migration_plan.py:69-78,425-429`; nothing prunes `state/requests/`), while `canary-rollout.md:49`
  assumes rollback exists; (b) is infeasible (a receipt stores no field values); the options differ only on an
  *uncommitted* replay from another session (`request_receipts.py:542-543` fires only for a prepared, unrecorded
  previous attempt; a committed one returns from `_existing`/`_replayed`, `task_operations.py:450-456`). The
  restamp defect is fixed under (a) regardless (§6 P1 spec; §4.2).
- Lenses: for — CB, AR. Against — FP keeps (a): the receipt freezes stable identities before effects
  (`message_id`, `expected_by`); (c) rehashes every projection, so an in-flight request straddling activation
  conflicts once; the strict decoder makes any new intent field an honest bump — and asks that F17's record carry
  the permanent blocker and why (c) was rejected (§4.2 and the §6 P1 spec do).
- If ratified: plan 2 — Task 6 Step 2b (the writer flip, Half B) dropped; Step 2a's decoder (Half A) is harmless
  whether or not it has shipped — no receipt is ever written in format 2; `request_facts.expected_fact`/`reconcile_facts` change, with tests and the
  prepared-not-committed replay leg; the `fleet-update-lifecycle.md` blocker sentence and the `host migrate`
  preview test are not needed; the §6 P1 spec is replaced; F17's lean stands as history with its pointer.

**A-F10 — An additive per-host summarizer runner (conditional). Status: proposed amendment.**
- Proposal: add `summarizer` to the P4 `Host` table, host-selected by the session's recorded `runtime`; a missing
  runner binary is `skipped.reason: no_summarizer`, never a retried failure; canary C12 covers `codex exec`'s
  structured-output and cost flags. Conditional on how clauDNA's `PROJECT_MISSION.md:24,64` ("never requires an
  account or API key") is read for Codex hosts — if it is Claude-relative, F10's text says so and the fork stands.
- Evidence: today a missing `claude` is retried `MAX_ATTEMPTS` times per segment (`summarize.py:88-89,125-129`);
  the runner seam exists (`summarize.py:110-111`); the actor kind a Codex host derives already decides whether a
  Codex session is summarized at all (§4.1).
- Lenses: for — AR; AM (conditional). Against — CB and FP keep (a): on a mixed host (a) costs nothing (`host setup`
  refuses without `claude`; summaries are opt-in per bot), (b) buys a second prompt baseline, cost model and
  isolation story, and the seam makes (b) a ~30-line function whenever wanted; no Codex-only host is foreseen
  (§14 Q10). All four agree F10's *text* needed the reader-half correction (§6 P4 spec, done).
- If ratified: the P4 clauDNA plan gains `Host.summarizer`, the `no_summarizer` reason (`events.py:174`'s
  vocabulary; spec §8's `skipped.reason`) and C12 in P0's Codex batch; F10's consequence line changes.

**A-F1 — Land F1(a) before (c): the receiver hook writes the executing session. Status: proposed amendment.**
- Proposal: `Transmission.session_uid` legal only with `state == "received"` (the `received_bytes` precedent,
  `contracts.py:296-311`), written into `detail` by `plane-dispatch-in.sh` from `hook["session_id"]` (F2 material
  when `CLAUDLOBBY_RUNTIME != claude`), declared in `wire_additions`, read back through `_DELIVERY_MSG`
  (`queries.py:182-194`); (c) follows for per-act attribution once C11 is answered, decoupled from F17.
- Evidence: P1 writes a one-way `sess_` hash no door joins on (§8 item 1 could not start from `task show`'s
  output), while the receiver hook already holds the authoritative `session_id` (`plane-dispatch-in.sh:88-93`) and
  needs no CHECK change, receipt or C11.
- Lenses: for — AR. Against — none argued the opposite; F1's own revisit trigger was "a reader needs the uid on
  the transmission row".
- If ratified: plan 2 Half B adds the field (contract + `wire_additions` + `plane-dispatch-in.sh` + `_DELIVERY_MSG`
  + tests); the F17 work waits for (c); §2.2 "Where it is written" gains the transmission row; `plane-lookup.py
  --session` prints the raw `session_id` that row carries beside `session_uid` (plan 2 Task 9).

**A-F3 — Sequencing only: raw files first, the plane leg with its first reader. Status: proposed amendment.**
- Proposal: the plane leg (five `session.*` metrics, two events) lands in the PR that repoints the first reader
  (`brief_read.py:114`/`usage_read.py:39`, or `fleet-digest`'s `plane samples` grant), at the grain that reader
  needs; the raw files land first. F3(b) is unchanged.
- Evidence: nothing reads `plane samples`; `brief_read`/`usage_read` keep reading transcripts (P2 Task 9 deferred
  the repoint); `api_error`/`tool_failure_streak` page nothing (§14 Q6); by the plan's own discriminator
  (`retention.py:84-91`) unread rows are the class the epic retires; the one problem the layer could solve
  (`rate_limit`, `fleet-observability.md:94`) stays unsolved.
- Lenses: for — AR. FP and CB agree on the ground ("no named consumer").
- If ratified: plan 4 splits P2-a into the raw-file PR and the plane-leg PR (Task 6 Steps 3–5, held on A-F3, move
  to the latter); §8 names the reader (done); the allowlist is sized to it.

**A-F5 — Scope F5 to the intake's internal vendor→house mapping. Status: proposed amendment.**
- Proposal: F5 names the intake's mapping (one dict, tested from the C5/C6 fixtures); raw files stay
  vendor-native; no OTTL, no semconv pin; the inclusive-vs-uncached token rule (§14 Q5) recorded once in
  `METRIC_NAMES`. Falls out of A-F4 if that is ratified.
- Evidence: the plan already demotes semconv ("the plane's registry names are the stable reader surface", F5's
  forge note); every `gen_ai.*` attribute is `Development` and the conventions carry no release tag; Claude and
  Codex raw metric *names* still differ (§11's last forge line).
- Lenses: for — AR. FP asks without proposing: who reads `gen_ai.*` names — if only a human with `jq`, F5(b) is
  cheaper (§14 Q13). Against — F5's lean: a convention over house names, for an external reader.
- If ratified: plan 4 — Task 4's `transform/normalize` OTTL block goes; the intake's mapping dict is the F5 artefact;
  the §6 P2 spec's "Three things" item 1 and the Collector YAML are rewritten; the "Stated limitations" semconv
  pin is deleted.

**A-F6 — The export item carries a transcript pointer; the job computes volume. Status: proposed amendment.**
- Proposal: add `transcript: {path, range}` to the export item's `segment` object (a pointer, not bytes) and have
  `session-export` compute `turns`, `tool_calls`, `tokens{…}`, `transcript_bytes` for every item from the
  transcript (`transcript_usage.parse_range` over the byte range); `usage: null` with `reason:
  no_transcript|retired` when the file is gone.
- Evidence: the F6 spec sourced `turns`/`transcript_bytes` from `summary.input`, yet a skipped item has
  `summary: null` (`export.py:113-114`), so the common row's volume is dark and the consumers lose `tool_calls`
  "until P2"; the null rule now in the §6 P3 spec is the fallback.
- Lenses: for — AR (PH supplies the fallback). Against — F6's shape: the plane holds what the consumers read, not
  transcript access; `tool_calls` is P2's `session.tool_calls` sample; a job reading transcripts re-creates the
  reader the retired digest had.
- If ratified: plan 5 Task 1 gains a key (`segment.transcript`) — an additive follow-up, not a hold: Task 1 ships
  unheld, since `{sealed_at, sealed_by, counts}` is needed under either outcome; plan 6 Task 4 computes volume in
  `summary_record`; the §6 P3 spec's null rule narrows to `journey`/`blocks`/`procedures`/`producer`.

**A-F14 — One shared store per host instead of per-bot roots (conditional). Status: proposed amendment.**
- Proposal: if the only reason for per-bot `CLAUDNA_STATE_DIR` is "the export job runs per bot", one shared store
  with one `export` call per host grouped by `session.actor.bot_id` (`export.py:88` iterates every session with a
  per-consumer cursor) avoids the seal → read-only → removal cutover F14 exists to manage. If the reason is bot
  lifecycle hygiene — a reaped bot's sessions leave with `$BOT_DIR`; `data-sweep` scope; per-bot writer isolation
  during the 0.26/0.27 flip-flop (§9) — F14's context states it at ratification (an ordinary fold) and the fork
  stands.
- Evidence: §4.2 (the export's iteration). The runbook's `chmod -R a-w` finding (AR) is adjacent and is already
  folded within F14(a) (§6 P3).
- Lenses: for — FP (conditional). Against — the hygiene reasons above, if they are the operator's.
- If ratified: plan 6 — the per-bot root (Task 1, with the §10 order-3 one-liner) and the `session-export` per-bot
  grouping, which ship now (clauDNA spec §1.1 rule 3 already says per bot), would be reverted: `session-export`
  groups by `actor.bot_id` and the one-liner gives way to the X21 pin remedy alone (§9); Task 7 (the runbook, the
  only task held on A-F14) goes. A-F14 holds nothing else: it is conditional on an operator reason not on record.

**Decisions needing ratification**

**D1 — Claudlobby's mission: a new fork F18. Status: proposed decision.** The epic's premise — Claudlobby
composes Codex bots — is excluded by `PROJECT_MISSION.md:5,11,114`, `README.md:3,12` and `CLAUDE.md:3`, and no
fork in F1–F17 decides it (F11 decides *where* Codex launching lives, not *whether*; §14 Q2's "F11 supersedes"
option is withdrawn). Proposed: **F18 — "Claudlobby composes and supervises agent CLIs: Claude Code today, Codex
through an execution adapter (#2149); the model stays each CLI's concern; no LLM-provider abstraction beneath the
CLI"** — recorded in the mission in the 2026-07-06 form (`PROJECT_MISSION.md:17`, #515), amending `:5`/`:11`/`:114`
and both one-liners coherently, coordinated with `origin/codex/974-mission-consolidation` (which rewrites the
mission and keeps the line). With it: the epic joins Current sprint focus (`PROJECT_MISSION.md:52-87`) carrying
#2145, and item 2 there (`:84`, the optional Claudosseum telemetry emitter) is advanced or retired explicitly.
Lens: AM (PC on the form). Lands before P1 Claudlobby's Half A merges (plan 2 Task 5 Step 2, unconditional).

**D2 — clauDNA's mission amendment. Status: proposed decision.** clauDNA's mission (last amended 2026-07-06) says
"for Claude Code" (`:5`, `:15`) and "does not handle telemetry" (`:33`, `:62`), while this epic assigns clauDNA
`runtime ∈ {claude, codex}`, an export contract Claudlobby's job consumes, `host_codex.py` and a Codex manifest.
Proposed: amend before 0.27.0 ships the closed `{claude, codex}` vocabulary — the session store as local memory
with an export door (no phone-home), reconciling `:33`/`:62` with `tool.failed`/`skill.invoked`/`CLAUDNA_TELEMETRY`;
whether clauDNA's scope includes non-Claude hosts, decided before the P4 clauDNA plan is written; F8 cited as the
aligned direction. Lands in plan 3 Task 5 or its own PR (**requires operator approval**). Lens: AM.

**Records.** One ADR in `documentation/decisions/` in the house `supersedes_scope` form
(`2026-07-18-claudron-consumption-door.md:9`) for F18 and the v2 §19 ruling this plan supersedes — or the operator
states the reason for none; the v2 §19 entry itself is plan 2's (§1.1). Every amendment above is a `[FORK-REOPEN]`
candidate. The plans implementing the contested options are held at the affected task and nothing else about them
changes; the fold that follows ratification re-specifies §6 for whichever amendments pass.
