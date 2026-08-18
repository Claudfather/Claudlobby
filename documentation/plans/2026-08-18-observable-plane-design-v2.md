# The Observable Plane — design v2 (reconciled)

**Status:** WORKING SPEC — v2, supersedes `2026-08-17-observable-plane-design.md` (kept as audit trail). Produced by reconciling an external implementation-grade review (2026-08-18) into the design walk; every factual claim in that review was independently verified before adoption (repo drift, landed commits, LangSmith plugin, Deep Agents capabilities, all five OTel trace-context specifics). Baseline: `origin/main` @ `e3b6347` — **not** the 56-commit-stale checkout v1 was written against.
**Session:** `8ad2aa7e-bade-4c55-b3c3-8af5869b7693` ("OBSERVABLE PLANE DESIGN").
**Lineage:** #974 (mission epic) · #264 (observability play) · #886 (central logging epic) · #904 (`claudlobby brief` — **landed**, `0cbca28`; the plane consumes it, never duplicates its joins) · dispatch `--type` gating (`d9c40b7`) · supersession (`a3644ca`, `36eb7a4`) · 2026-07-30 system review (write-rich/read-starved).

---

## 1. Thesis

Claudlobby's important state exists **between** independently running agents and **around** their processes: who asked whom to do what, whether it reached them, what role each actor held, what happened across restarts, which commitments remain open, what changed, which facts are trustworthy.

The observable plane is first an **organizational flight recorder**. The cockpit is a projection over it. Positioning (not an exclusivity claim): *LangSmith observes the work; LangGraph orchestrates the work; Claudlobby governs the standing team doing the work.*

**Build and own:** stable host/actor/instance/session identity · communication intent · transport attempts and acknowledgement · task/work-item lifecycle · workstream lifecycle · supervision/process lifecycle · declared topology vs observed interaction · provenance, gaps, trust state · (later) deliberation, dissent, decisions, outcomes.
**Adopt/integrate:** Claude Code's native OTel export · standard trace/span/message identifiers · OTel messaging semconv · LangSmith as an optional trace/eval surface (piloted, never automatic) · framework runtimes (LangGraph / Deep Agents / Agent SDK) as *possible future execution backends* behind an execution-adapter seam — never as substitutes for the domain model.
**Defer:** library navigator · equipment UI · utilization dashboards · PWA polish · org-chart polish · broad terminal integration · management verbs beyond the eventual equip verb.

## 2. Decision forks — LOCKED

| # | Fork | Ruling |
|---|---|---|
| F1 | Placement | Inside claudlobby (`plane/` subsystem, `claudlobby plane` command); contract-first so extraction stays cheap |
| F2 | Stack | FastAPI daemon + React/Vite static assets; Pydantic v2 contracts → JSON Schema → generated TS types (CI-verified); no ORM; versioned `.sql` migrations |
| F3 | Storage | SQLite WAL, one db per host, outside any vault working tree; nightly `.backup`; FTS5 over permitted content only |
| F4 | Two-lane rule (v2 wording) | **Declare durable organizational configuration in git. Record runtime facts and operational commitments as append-only domain events in SQLite. Build all current state as projections.** |
| F5 | Teams | **Preserve optional groups.** Fleet = deployment/policy/supervision boundary; group = optional logical layer inside a fleet, hidden in UI when a fleet has exactly one; `fleet.manager` scalar lands; `reports_to`/`manages` remain the topology surface. The team=fleet collapse is rescinded; no schema removal |
| F6 | Workstreams | **Migrate to events + projection**: `workstream_contracts` + append-only `workstream_events`; `workstreams.json` becomes a rebuildable compatibility projection during transition, then retires; `workstream-update.sh` becomes a door shim |
| F7 | Privacy default | Shipping default: metadata + redacted preview; full bodies **opt-in per fleet** (fleet.yaml surface, no-silent-switches). **Operator estate ruling: full bodies day one on all fleets**, alias-only exports, raw identifiers separated from presentation |
| F8 | v1 UI cut | Attention queue + lifecycle timeline (the channel) + trust/gaps surface + thumbnail grid + one read-only live pane. Everything else deferred (§16) |
| F9 | Comms truth | Intent/attempt/ack split (§7); **no `delivered` for tmux, ever** — `pane_submitted` + acknowledgement; at-least-once with idempotent handling; explicit `unknown` |
| F10 | Identity | Minted uids; names are aliases (§4) |
| F11 | Vocabulary | Two axes: `message_class` × `command_type` (§7), reconciled with landed `--type` semantics |
| F12 | Snapshots | Keyframes for slow-changing resolved state only; volatile telemetry to presence/health events (§9) |
| F13 | Vault layout | `local/` is the primary vault (Claudron-made, e.g. `<operator>-claudron-vault`) gaining a `fleets/` namespace; N secondary mounts under `vaults/` (gitignore already landed upstream); graduation ladder root-mode → `vault init` → remote |
| F14 | Terminology | Host = the machine, THE container (Host → Fleet → Bot); "system" = the claudlobby install only; rename PR **off the critical path** |
| F15 | Sequencing | Substrate before UI; phases §18; vocabulary/layout/teams PRs never block the semantic slice |

Open Phase-1 lock items (must be ruled before migration 1, recommendations attached): §19.

## 3. Identity model

Minted, immutable uids; every human-readable name is a mutable alias.

- `host_uid` — UUID minted once, stored outside mutable config (e.g. `state/host-uid`); aliases: hostname, MagicDNS name, operator label.
- `fleet_uid` — stable logical fleet identity (survives rename/move).
- `actor_uid` — the logical specialist (survives sessions, restarts, host moves).
- `bot_instance_uid` — one installed/supervised runtime instance of an actor on a host.
- `session_uid` — one Claude Code session/incarnation (joins to transcript + OTel `session.id`).
- `process_uid` (optional) — one OS process lifetime.

`bot:<fleet>/<name>` survives as the **composed alias** — it is what bash doors stamp and humans read; ingest resolves alias→uid against the registry at write time. Group membership (F5) is registry data on the fleet payload, not identity. Global uniqueness is designed now even while the first db is one-per-host.

## 4. Common event envelope

Every authoritative observed fact carries: `event_id` (globally unique, **minted before any insert attempt**) · `event_type` · `schema_version` · `occurred_at` (producer time) · `observed_at` · `ingested_at` · `ingest_seq` (explicit local ordering authority — `rowid` is never the public cursor; API cursors are opaque) · `host_uid` · `fleet_uid?` · `actor_uid?` · `bot_instance_uid?` · `session_uid?` · `msg_id?` · `work_item_id?` · `task_attempt_id?` · `workstream_id?` · `deliberation_id?` (reserved seam; entity is Phase 5) · `correlation_id` · `causation_id` · `trace_id?`/`span_id?` · `source`/`emitter` · `source_ref` · payload · privacy classification.

Physical form (one event table with typed projections vs typed tables sharing the envelope + one ingest sequence) is a §19 lock item. Column naming avoids reserved-looking SQL (`sender_id`/`recipient_id`, never `from`/`to`).

## 5. Write spine

Doors → `claudlobby emit` → validation → parameterized INSERT; infrastructure failure → spool (§10); **contract violation fails loudly at the caller** (inspectable, never silently coerced). Invariant (v2 wording): **authoritative facts are immutable append-only events; mutable queues and caches may exist but are never the historical source of truth.**

Performance gates before the implementation locks (§14): the repo has already measured the hazard class on the Pi — 137 ms Python spawn vs 3.5 ms prefilter (`mention-rewrite` notes). The CLI contract stays stable; the implementation may be a direct lightweight writer, a Unix-socket ingest daemon, or opportunistic spooling. The authoritative write path and spool drain **must function without the UI daemon**.

## 6. Model catalog (lanes)

**Lane A — declared (files, git):** fleet.yaml (per fleet; groups optional per F5) · system.yaml (per host; follow-on: vault `hosts/<host_uid>/`) · `.env` (never in git; `${VAR}` names only) · templates (OSS git = schema history).

**Lane B — observed (append-only, envelope-bearing):**

| Family | Grain |
|---|---|
| `registry_snapshots` | slow-changing entity state × observed change (§9) |
| `declaration_observed` | source-revision observation (vault rev seen), even when resolved state is unchanged (§9) |
| `communication_intents` | one semantic message (`msg_id`) (§7) |
| `transport_attempts` | one attempt (`attempt_id`) per carrier try (§7) |
| `work_items` / `task_attempts` / `task_events` | durable objective / one assignment / lifecycle events (§8) |
| `workstream_contracts` / `workstream_events` | operational commitments (F6) |
| `lifecycle_events` | machinery events (restart/heal/stuck/teardown; vocabulary defers to #903) |
| `presence_health_events` | volatile samples: load, disk, thermal, vault freshness, RC state, pane activity, heartbeat (§9) |
| `session_digests` · `session_usage`* | existing schemas; *usage lane pending the OTel pilot (§12) |

**Lane C — derived (rebuildable, never authoritative):** current-registry projections (hash-verified against files) · SCD2 views · task-status view (evidence-based activation, §8) · workstreams.json compatibility projection (transitional) · presence rollups · trust metrics (§15) · interaction-density graph (§16 — explicitly **not** "observed org": message volume shows interaction, not authority; rendered as time-windowed density beside declared topology, never overriding it).

## 7. Communication model

**Communication is the base class; task is a decorator** (unchanged). One universal door (`comms-send`) is the only serializer; task machinery is an optional module it invokes.

**Intent** (immutable): `msg_id` · `sender_id` · `intended_recipient_id` · `message_class` · `command_type?` · links (`work_item_id`/`task_attempt_id`/`workstream_id`/`deliberation_id?`) · `reply_to_msg_id?` · `supersedes_msg_id?` · cancellation target? · body or redacted-body reference (per F7 classification) · `body_bytes`/`body_sha256`/`truncated` · created time · correlation/causation · idempotency key.

**Transport attempts** (append-only): `attempt_id` · `msg_id` · attempt number · carrier (`tmux` | `telegram-tgpost` | `telegram-bridge`) · destination (+ `to_chat_id` stored, alias-rendered) · state: `send_attempted → carrier_accepted | pane_submitted | failed | unknown`, plus `recipient_acknowledged`, `duplicate_suppressed` · `carrier_ref?` (e.g. Telegram message_id) · error details · timestamps. Telegram API acceptance = carrier accepted, nothing more; `pane_send_verified` establishes submitted, never receipt.

**Delivery contract:** at-least-once intent/attempt recording, idempotent `msg_id` handling, explicit `unknown`, recipient acknowledgement required for task activation. Intent + task contract are created atomically in SQLite **before** transmission.

**Vocabulary — two axes** (reconciled with `d9c40b7`, where only `task` mints):
- `message_class`: `task_request, report, question, answer, alert, notice, briefing, nudge, acknowledgement, chat, config_change, raw_control`.
- `command_type` (optional): `task, cancel, compact, restart, query` — the landed dispatch `--type` set; extension governed where that vocabulary lives.
- `decision` remains reserved for Phase 5.

**Misroute detection** stays in the door (warn at send time: report-shaped content to a human-channel recipient while a joined attempt sits unclosed) and in derived views.

**Carriers in v1.0:** tmux doors, `tg-post` shim, **bridge outbound and inbound** (the operator appears in their own stream).

## 8. Task model

Three-level split: **`work_item_id`** (durable objective) → **`task_attempt_id`** (one assignment to one actor/instance; reassignment and retry mint new attempts) → **`msg_id`** (one communication carrying it).

Event vocabulary (append-only `task_events`): `contract_created, dispatch_intended, transmission_failed, dispatch_submitted, receiver_acknowledged, accepted, rejected, progress, blocked(§19), resumed, completed, failed, cancelled, deadline_changed, superseded, reassigned, retry_created, orphaned_by_session_loss, recovered_after_restart, expired`.

**Activation is evidence-based** (derived, never stored): contract exists ≠ active. `created-not-sent → submitted-unacknowledged → accepted/open → …closed`, with `dispatch failure` and `overdue`/`orphaned` distinguished. Supersession is a landed runtime fact (`a3644ca`/`36eb7a4`) and is first-class here.

`blocked` semantics: current runtime treats blocked as terminal (`dispatch-overdue.py` `_TERMINAL`); the operator model may need open-but-waiting — §19 lock item, recommendation `blocked_waiting` vs `returned_blocked` as two events, **no silent behavior change during migration**.

## 9. Registry snapshots v2 + presence/health split

**Keyframes keep:** identity, declared topology (incl. optional groups), resolved configuration, equipment, permission posture, versioned hashes, vault binding, software versions. **Presence/health events take:** CPU/load, disk, thermal/under-voltage, boot/session state, vault ahead/behind freshness, RC state, pane activity, heartbeats. (Volatile data inside keyframes would invalidate the tens-of-rows/week envelope.)

- SCD2 view: `PARTITION BY host_uid, entity_type, entity_uid ORDER BY observed_at, ingest_seq` — never bare `ts`; no dependence on timestamp uniqueness; indexes match the view.
- **Tombstones + scan completeness:** rows carry `operation (create|update|delete)` and `scan_id`; absence implies deletion only after a complete authoritative scan; a partial/failed scan must not tombstone the estate.
- **Provenance without hash-suppression:** `declaration_observed` events record every newly observed vault revision even when resolved state is byte-identical — the provenance chain never disappears into the hash gate.
- **Canonicalization is a versioned spec** (encoding, Unicode normalization, numeric representation, null/default inclusion, path normalization, key ordering, serializer version, hash algorithm) with golden fixtures across supported versions. ("Sorted keys" vs "Pydantic field order" was a v1 contradiction; one canonical-bytes definition replaces both.)
- Human surface remains the field-level diff view; payloads bounded (verbatim short lists; hashes for bodies).

## 10. Spool contract

Mint `event_id` before any db access · serialize the full versioned envelope · atomic temp-write + rename · infrastructure failures separated from contract violations · replay under a unique `event_id` constraint (duplicate replay = success) · delete only after committed ingestion · capped retries · poison/unsupported-version quarantine (never retried forever, never silently discarded) · oldest-age + retry-history exposed · drains independently of the UI daemon. Operator verbs: `claudlobby plane spool list|inspect|retry|quarantine`, plus `plane status` / `plane doctor`.

## 11. Privacy & retention — pre-v1 requirements

Ratified **before** the first canary stores anything searchable (tailnet admission is network access control, not content authorization):

- Field classification on every envelope; collection defaults per F7 (metadata + redacted preview; bodies opt-in per fleet — operator estate: on).
- ANSI stripped at the door; native OTel content gates (`OTEL_LOG_USER_PROMPTS` etc.) **off by default**, enabling them is an explicit disclosed act; hosted export (LangSmith) is an explicit operator decision per fleet.
- Raw identifiers (chat ids, remotes) stored separately from alias-first presentation; exports alias-only; full-content reveal is an explicit UI act; FTS indexes only permitted content and search results state completeness when bodies are redacted/truncated.
- Local file permissions, backup handling, and deletion behavior specified with the schema; retention windows are part of this policy — the v1 "no retention until 1 GB" line is **removed**.
- Sensitive-by-nature registry fields (permission posture, env-key names) carry classification; `dangerously_skip` badging includes severity + remediation.

## 12. Build-vs-adopt (three layers; verified 2026-08-18)

1. **Execution runtime** — Claude Code sessions today. LangGraph / Deep Agents / CrewAI / Agent SDK are real candidates *someday* (they support persistence, long-running async subagents with follow-up/cancel — the "frameworks are ephemeral" and "frameworks can't consensus" claims are withdrawn). Adopting one changes who owns execution and forfeits the existing harness + subscription economics; they are futures behind the execution-adapter seam, not v1 choices.
2. **Telemetry/tracing** — adopt. Native OTel confirmed: `agent_id`/`parent_agent_id`, `workflow.run_id`/`workflow.name`, beta distributed tracing (`CLAUDE_CODE_ENHANCED_TELEMETRY_BETA`), `TRACEPARENT` propagated to Bash/PowerShell subprocesses and **accepted inbound by `-p`/Agent SDK — while interactive sessions deliberately ignore inbound trace context**. The precise limitation that defines the build boundary: a tmux-injected peer dispatch stays uncorrelated unless Claudlobby instruments the boundary. So: mint `msg_id`, attach `trace_id`/`span_id`/correlation/causation, emit producer/consumer semantics per OTel messaging semconv, export through OTel — **authoritative domain record stays local**. LangSmith's official Claude Code plugin (verified: messages/tools/compaction/subagents, `thread_id` grouping, `CC_LANGSMITH_METADATA`, no rewrite) is a pilot for intra-session tracing/eval.
3. **Organizational domain model** — build. Nothing off the shelf knows actors, dispatch contracts, acknowledgement, topology, supervision, workstreams, or misroutes.

**Pilots (Phase 3, one canary bot, conservative content):** (a) native OTel → answers cumulative-vs-delta, duplicate/gap behavior on retries, collector cost on the Pi, session→actor mapping — decides whether `session_usage`/`utilization_windows` are deleted from the roadmap; (b) LangSmith plugin with safe metadata (`host_uid`, `fleet_uid`, `actor_uid`, `bot_instance_uid`) — decides which trace surfaces we never build.

## 13. Enforcement net & migration

- Doors become shims over `comms-send` (identical interfaces, canary-able per bot). `pane_send_verified` emits transport-attempt evidence when handed a `msg_id`; otherwise ledgers `raw_control` with caller provenance.
- **Raw rate is tracked by caller class** (dispatch/report vs keepalive/pulse/briefing/sweep control-plane) — control traffic is classified, not zeroed; a global zero was the wrong target. Phase 0 re-inventories every callsite against current main.
- **Dual-write canary:** one durable id minted before either sink; the sink that determines command success is declared; per-sink results recorded; reconciliation by id; mismatches exposed — never inferred equivalence from timestamps+text.
- **Backfill:** compare a clean semantic epoch (old ledgers as read-only legacy history; import only where provenance is adequate) against broad first-boot backfill — deterministic import ids (file identity + offset + content hash), `source=legacy`, batch + confidence, malformed records preserved as gaps, restartable/idempotent, dry-run counts, **never silently infer task closure**. Decision falls to the implementation plan with a stated default: clean epoch + selective import.

## 14. Performance & dependency gates

Before locking implementation: cold/warm emit p50/p95 on the Pi · 20–25-bot burst · concurrent lifecycle+comms writers · `busy_timeout`, transaction mode, retry budget, WAL checkpoint policy, `synchronous` mode specified · disk-full behavior · startup/import time. Dependency/capability matrix for FastAPI/Pydantic/ttyd/Tailscale/frontend: optional UI features degrade without disabling the core ledger. Thumbnail capture: one bounded backend sampler per pane, cached — never multiplied by browser count.

## 15. Schema compatibility, testing, trust metrics

- Shell doors are the highest-risk clients: accepted schema versions (N/N-1), unsupported-future behavior, stable JSON input, exit codes, fixtures, downgrade refusal, pre-migration backup + restore verification. TS-type regeneration CI-verified.
- **Test matrix (pre-substrate, not post-UX):** empty-db migration · migration from every released schema · downgrade refusal · append-only enforcement · envelope constraints · duplicate `event_id` replay · concurrent emitters · timestamp ties · out-of-order producer clocks · complete-vs-incomplete scans · tombstones · canonical-hash golden fixtures · **every communication crash boundary** (send-succeeded/record-missing; record-exists/send-not-attempted; unknown; ack; duplicate suppression) · `SQLITE_BUSY` · disk full · malformed spool item · poison/dead-letter · drainer restart · dual-write mismatch · idempotent legacy import · projection rebuild parity · OTel duplicate/delta fixtures · collector outage · real shell door → CLI → SQLite → reader integration.
- **Trust metrics:** emit attempts, committed, spooled, rejected, duplicate ids, oldest spool age, replay failures, dual-write mismatches, raw-by-caller, unacknowledged-delivery age, projection lag, migration integrity failures.

## 16. v1 UI (F8)

Three questions: *What needs me now? What happened, and why? What changed or became untrustworthy?*

- **Attention queue:** failed dispatches, unacknowledged tasks, overdue, orphaned, broken emitters/spool.
- **Lifecycle timeline — the channel:** the comms stream threaded by work item / reply chain; full bodies per F7; FTS; one message's complete lifecycle (intent → attempts → ack → reports → closure) inspectable end to end.
- **Trust/gaps surface:** gaps counter (every gap: which caller, why unclassifiable, which door should replace it, communication vs machinery), malformed counters, spool state, dormant emitters, provenance/freshness badges.
- **Thumbnail grid + one read-only live pane:** capture-pane sampler + ttyd `attach -r`; retained against review advice — substrate-independent, weekend-scale, and the founding ask ("is my fleet alive" precedes "what needs me").
- Minimal identity context (fleet/bot cards enough to navigate) — full org chart, library, equipment, utilization, PWA deferred to Phase 6.
- **Required panel states everywhere:** first load / legitimately idle / emitter disabled / stale / partial source / malformed / unreadable / daemon disconnected / reveal denied / unknown — with last-successful-observation, provenance, freshness, remediation. **Never render zero when the source is absent** (brief's degraded/omitted philosophy). Design pass owes: semantic state colors with non-color equivalents, stable ordering under live updates, preserved focus, reduced-motion, keyboard nav, WCAG contrast, accessible alternatives to ANSI. Mobile intent: **awareness/triage only** in v1.
- The plane consumes the `brief` service layer for its joins wherever brief already answers the question.

## 17. Operator experience

Golden path: `claudlobby plane init | start | status | doctor | open`. Clocks: existing fleet → useful dashboard < 5 min; returning operator after 3 months → health/URL/next action < 60 s; symptom → understood lifecycle < 10 min. `/healthz` + readiness: schema version, db path, last successful write/projection, spool depth + oldest age, migration state, collector/OTel status, stale-emitter detection, exact corrective commands. The plane never alerts through the fleet it observes; it has its own local service-health path ("browser does not load" is not failure observability).

## 18. Phased sequence

- **Phase 0 — reconcile facts:** ✅ mostly done in-session (baseline refreshed to `e3b6347`; capabilities re-verified; landed prerequisites removed). Remaining: callsite/state-store re-inventory against current main.
- **Phase 1 — lock the semantic kernel:** rule §19's items; ratify identity, envelope, ordering, privacy, vocabulary, comms model, task state machine, spool, schema compatibility, workstream migration. **No UI.**
- **Phase 2 — headless vertical slice:** `task contract → message intent → transport attempt → acknowledgement → progress → completed/failed/blocked/overdue/orphaned`, exposed via CLI + SQLite + one reader/service layer + doctor/status + projection rebuild. Dual-write canary beside JSONL.
- **Phase 3 — commodity telemetry pilot:** OTel + LangSmith on one canary bot; delete redundant roadmap surfaces on evidence.
- **Phase 4 — minimal operator plane:** F8's five surfaces over the proven slice.
- **Phase 5 — organizational learning:** deliberations, independent contributions, synthesis, preserved dissent, decisions, outcome joins — only then consensus-learning claims.
- **Phase 6 — broader cockpit:** org/interaction graph, equipment, library, utilization, PWA, management verbs (equip first, through git), terminal breadth.

## 19. Phase-1 lock items (open, with recommendations)

1. **Envelope physical form** — one event table + typed projections **vs** typed tables sharing the envelope. Rec: typed tables + shared `ingest_seq` (simpler indexes, per-family retention), decide after the emit benchmark.
2. **`blocked` semantics** — rec: two events (`blocked_waiting`, `returned_blocked`); overdue matching keeps current behavior until the cutover is explicit.
3. **Canonical-bytes spec** — full definition + golden fixtures.
4. **Ingest implementation** — direct writer vs socket daemon, from Pi benchmarks (§14).
5. **Backfill posture** — clean epoch + selective import (default) vs broad backfill.
6. **Exact DDL for every Lane-B family** — after 1–5.
7. **Claudron confirmations** — `fleets/` namespace tolerance; multi-vault capability (issue on the Claudron repo).

## 20. References

v1 draft (audit trail): `documentation/plans/2026-08-17-observable-plane-design.md`. External review: reconciled 2026-08-18; all factual claims verified (repo: `git log` on the five named commits; ecosystem: LangSmith trace-claude-code docs, Deep Agents async-subagents docs, code.claude.com monitoring-usage). Research memo: artifact "The Observable Plane" (2026-08-08) — note its §span-tracer claims predate the LangSmith plugin and are superseded by §12 here.
