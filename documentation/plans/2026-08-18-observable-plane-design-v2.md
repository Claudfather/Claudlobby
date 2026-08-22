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
| F6 | Workstreams | **Migrate to events + projection**: the `workstreams` construct + workstream-kind events; `workstreams.json` becomes a rebuildable compatibility projection during transition, then retires; `workstream-update.sh` becomes a door shim |
| F7 | Privacy default | Shipping default: metadata + redacted preview; full bodies **opt-in per fleet** (fleet.yaml surface, no-silent-switches). **Operator estate ruling: full bodies day one on all fleets**, alias-only exports, raw identifiers separated from presentation |
| F8 | v1 UI cut | Attention queue + lifecycle timeline (the channel) + trust/gaps surface + thumbnail grid + one read-only live pane. Everything else deferred (§16) |
| F9 | Communications truth | Intent/attempt/ack split (§7); **no `delivered` for tmux, ever** — `pane_submitted` + acknowledgement; at-least-once with idempotent handling; explicit `unknown` |
| F10 | Identity | Minted uids; names are aliases (§4) |
| F11 | Vocabulary | Two axes: `message_class` × `command_type` (§7), reconciled with landed `--type` semantics |
| F12 | Snapshots | Keyframes for slow-changing resolved state only; volatile telemetry to metric_samples (§9) |
| F13 | Vault layout | `local/` is the primary vault (Claudron-made, e.g. `<operator>-claudron-vault`) gaining a `fleets/` namespace; N secondary mounts under `vaults/` (gitignore already landed upstream); graduation ladder root-mode → `vault init` → remote |
| F14 | Terminology | Host = the machine, THE container (Host → Fleet → Bot); "system" = the claudlobby install only; rename PR **off the critical path** |
| F15 | Sequencing | Substrate before UI; phases §18; vocabulary/layout/teams PRs never block the semantic slice |
| F16 | Physical shape (v2 — first-principles re-ruling 2026-08-23, supersedes the 2026-08-19 typed-per-family ruling) | **Constructs + one events log.** Typed tables for the constructs the system works with — `communications`, `work_items`, `assignments` (né assignments), `workstreams` (né workstreams — the contract row IS the workstream), `registry_snapshots`, `metric_samples`, `identity_registry`, `ingest_ledger` — plus **ONE `events` stream** logging what happens to them: kinds `transmission` (né transmissions) · `task` · `workstream` · `system` · `declaration`. Shared typed ref columns, per-kind conditional vocabulary CHECKs, per-kind JSON `detail` tails, partial indexes per hot kind. 13→9 physical tables; five concepts unchanged; every walk ruling preserved (crash-correctness: the communication construct is written before its first transmission event; registry-stamped severity: sparse column on kind=system; samples retention: own table). Renames dissolve the "attempts" double-duty defect (an assignment is a contract; a transmission is a log row). Decided by 7-dimension matrix (plan file, session record); flip conditions: Pi bench pathologies on partial indexes/conditional CHECKs, or the per-kind parity test proving untestable |
| F19 | System-event vocabulary | **Warn-on-unknown registry** (ruled 2026-08-20): known set seeded from observed emitters + `events.py` CRITICAL_TYPES; unknown types still INGEST, counted in trust metrics, surfaced by doctor; #903's SSOT tightens the registry later. Deliberately asymmetric with communications — communications callers are our doors (bugs loud), system-event emitters are the whole estate (never lose a machinery event to a new script) |
| F20 | Metric-sample shape | **Generic samples table** (ruled 2026-08-20): subject uid + registry-governed metric name + JSON value + status flag; new probes need no migration; hot metrics promote to indexed generated columns on evidence; the aggressive-retention lane |
| F21 | Workstream vocabulary | **Mirror the task model** (ruled 2026-08-20): contract row IS "opened" (one fact, one row — same mapping as `contract_created`); events = `progressed, renewed, blocked, unblocked, closed, archived, plan_linked, plan_unlinked`; status always derived, never stored; `workstream-update.sh` verbs map 1:1 in the door shim |
| F17 | `blocked` semantics | **Two events**: `blocked_waiting` (nonterminal — assigned, cannot progress) and `returned_blocked` (terminal — responsibility back to manager). Legacy `blocked` reports map to `returned_blocked`; existing overdue-matching behavior preserved exactly, no silent change (ruled 2026-08-19) |
| F18 | Backfill posture | **Clean epoch + selective import** — new trustworthy epoch at cutover; dispatch-log + report-back rows (task-id-bearing) imported with `source=legacy`, deterministic import ids, confidence markers; ambiguous history stays read-only legacy; task closure never inferred (ruled 2026-08-19) |

Remaining Phase-1 items (technical, owned by the implementation plan — no further operator rulings required): §19.

## 3. Identity model

Minted, immutable uids; every human-readable name is a mutable alias.

- `host_uid` — UUID minted once, stored outside mutable config (e.g. `state/host-uid`); aliases: hostname, MagicDNS name, operator label.
- `fleet_uid` — stable logical fleet identity (survives rename/move).
- `actor_uid` — the logical specialist (survives sessions, restarts, host moves).
- `bot_instance_uid` — one installed/supervised runtime instance of an actor on a host.
- `session_uid` — one Claude Code session/incarnation (joins to transcript + OTel `session.id`).
- `vault_uid` — one mounted vault (primary or secondary); its name and remote are aliases.
- `project_uid` — one declared project (projects.yaml, grain `(fleet, key)`); the kebab key is its alias.
- `library_item_uid` — one library item at grain `(category, name, source_tier)`; equipment lists resolve to these.
- `process_uid` (optional) — one OS process lifetime.

`bot:<fleet>/<name>` survives as the **composed alias** — it is what bash doors stamp and humans read; ingest resolves alias→uid against the registry at write time. Group membership (F5) is registry data on the fleet payload, not identity. Global uniqueness is designed now even while the first db is one-per-host.

## 4. Common event envelope

Every authoritative observed fact carries: `event_id` (globally unique, **minted before any insert attempt**) · `event_type` · `schema_version` · `occurred_at` (producer time) · `observed_at` (**non-null exactly when the emitter reports another system's past fact** — bridge-inbound carrier timestamps, log-derived detections like dmesg SD stalls, legacy import, the OTel adapter; null asserts first-handedness; stamped by the emitter at emit time — **reads never write**, and spool lag deliberately does NOT use it, being visible as ingested_at − occurred_at) · `ingested_at` · `ingest_seq` (explicit local ordering authority — `rowid` is never the public cursor; API cursors are opaque) · `host_uid` · `fleet_uid?` · `actor_uid?` · `bot_instance_uid?` · `session_uid?` · `msg_id?` · `work_item_id?` · `assignment_id?` · `workstream_id?` · `deliberation_id?` (reserved seam; entity is Phase 5) · `correlation_id` · `causation_id` · `trace_id?`/`span_id?` · `source`/`emitter` · `source_ref` · payload · privacy classification.

Physical form (one event table with typed projections vs typed tables sharing the envelope + one ingest sequence) is a §19 lock item. Column naming avoids reserved-looking SQL (`sender_id`/`recipient_id`, never `from`/`to`).

## 5. Write spine

Doors → `claudlobby emit` → validation → parameterized INSERT; infrastructure failure → spool (§10); **contract violation fails loudly at the caller** (inspectable, never silently coerced). Invariant (v2 wording): **authoritative facts are immutable append-only events; mutable queues and caches may exist but are never the historical source of truth.**

Performance gates before the implementation locks (§14): the repo has already measured the hazard class on the Pi — 137 ms Python spawn vs 3.5 ms prefilter (`mention-rewrite` notes). The CLI contract stays stable; the implementation may be a direct lightweight writer, a Unix-socket ingest daemon, or opportunistic spooling. The authoritative write path and spool drain **must function without the UI daemon**.

## 6. Model catalog (lanes)

**Lane A — declared (files, git):** fleet.yaml (per fleet; groups optional per F5) · projects.yaml (per fleet) · **system.yaml — PACKAGE tier, not the operator's** (verified 2026-08-23: the live file is `claudlobby/system.yaml`, tracked, resolved from the package dir; header says "package-owned and pristine"; operator edits dirty the checkout and `repo_pull_blocker` then BLOCKS updates — a loud fence, not a silent clobber; operator intent rides fleet.yaml overrides; package-tier movement is exactly snapshot situation-1: changed payload hash, unchanged vault_rev) · `.env` (never in git; `${VAR}` names only) · templates (OSS git = schema history). **NAMED DESIGN ITEM — the missing host construct: `vault hosts/<host_uid>/system.yaml`** (promoted from follow-on 2026-08-23; two walks have hit this wall). Today host-scoped operator intent is fragmented: package tier (immutable), host `.env` (secrets only), and fleet.yaml awkwardly arming HOST jobs (update-siblings — which of four fleets speaks for the host?). The hosts/ construct gives per-host operator declarations a versioned vault home: fresh machine = clone vault → setup-system reads declared host config; the Host card's system facet gains real declared-vs-package provenance.

**Lane B — observed (append-only, envelope-bearing):**

**Physical mapping (F16-v2)** — family names below remain the model's logical vocabulary; each lives either as its own construct table or as a `kind` on the one events stream:

| Family (logical) | Physical home | Grain |
|---|---|---|
| `communications` | own table (msg_id PK, body, FTS) | one semantic message (§7) |
| transmissions | `events` kind=`transmission` | one carrier try per row: send_attempted → pane_submitted/carrier_accepted/failed → recipient_acknowledged (§7) |
| `work_items` | own table | durable objective (§8) |
| `assignments` | own table (né assignments) | one actor's assignment of an objective (§8) |
| task events | `events` kind=`task` | work lifecycle happenings (§8) |
| `workstreams` | own table (né workstreams; the contract row IS the workstream) | one campaign (F6) |
| workstream events | `events` kind=`workstream` | campaign happenings incl. plan_linked/unlinked (F21) |
| system events | `events` kind=`system` | machinery detections (F19; registry-stamped severity) |
| declaration observed | `events` kind=`declaration` | vault revision seen (§9) |
| `registry_snapshots` | own table | entity keyframes × observed change, tombstones (§9) |
| `metric_samples` | own table (the volume/retention lane) | one level sample, 30d raws (F20) |
| `session_digests` · `session_usage`* | own tables | existing schemas; *usage pending the OTel pilot (§12) |

**Lane C — derived (rebuildable, never authoritative):** current-registry projections (hash-verified against files) · SCD2 views · task-status view (evidence-based activation, §8) · workstreams.json compatibility projection (transitional) · presence rollups · trust metrics (§15) · interaction-density graph (§16 — explicitly **not** "observed org": message volume shows interaction, not authority; rendered as time-windowed density beside declared topology, never overriding it).

## 7. Communication model

**Communication is the base class; task is a decorator** (unchanged). One universal door (`communication-send`) is the only serializer; task machinery is an optional module it invokes.

**Intent** (immutable): `msg_id` · `sender_id` (+ `sender_session_uid?` — the transcript/OTel join, populated when the door knows its session) · `intended_recipient_id` · `message_class` · `command_type?` · links (`work_item_id`/`assignment_id`/`workstream_id`/`deliberation_id?`) · `reply_to_msg_id?` · `supersedes_msg_id?` · cancellation target? · body or redacted-body reference (per F7 classification) · `body_bytes`/`body_sha256`/`truncated` · created time · correlation/causation · idempotency key.

**Transport attempts** (append-only): `attempt_id` · `msg_id` · attempt number · carrier (`tmux` | `telegram-tgpost` | `telegram-bridge`) · destination (+ `to_chat_id` stored, alias-rendered) · state: `send_attempted → carrier_accepted | pane_submitted | failed | unknown`, plus `recipient_acknowledged`, `duplicate_suppressed` · `carrier_ref?` (e.g. Telegram message_id) · error details · timestamps. Telegram API acceptance = carrier accepted, nothing more; `pane_send_verified` establishes submitted, never receipt.

**Delivery contract:** at-least-once intent/attempt recording, idempotent `msg_id` handling, explicit `unknown`, recipient acknowledgement required for task activation. Intent + task contract are created atomically in SQLite **before** transmission.

**Vocabulary — two axes** (reconciled with `d9c40b7`, where only `task` mints):
- `message_class`: `task_request, report, question, answer, alert, notice, briefing, nudge, acknowledgement, chat, config_change, raw_control`.
- `command_type` (optional): `task, cancel, compact, restart, query` — the landed dispatch `--type` set; extension governed where that vocabulary lives.
- `decision` remains reserved for Phase 5.

**Misroute detection** stays in the door (warn at send time: report-shaped content to a human-channel recipient while a joined attempt sits unclosed) and in derived views.

**Carriers in v1.0:** tmux doors, `tg-post` shim, **bridge outbound and inbound** (the operator appears in their own stream).

**Separation & keying (2026-08-20 walk ruling).** ALL communications route to `communications`; it records communication facts only — outcomes live in `transmissions`, task semantics in the task tables, with exactly two nullable link ids on the intent. The write order is fixed and sequential in concept: task machinery first (contract rows exist before anything references them), intent second, transport third; whether contract+intent share one SQLite transaction is a storage detail — separation lives in tables and writer modules, never in transaction boundaries. `msg_id` is the communication id and the table's PRIMARY KEY. Party keying: aliases are namespaced text (`bot:<fleet>/<name>` · `operator` · `system:<job>` · `telegram:<alias>` · future `slack:<team>/<channel>` · `broadcast:<fleet>`) resolved at ingest to `sender_uid`/`recipient_uid`; the carrier-native raw address rides `recipient_raw` (sensitive). Extensibility asymmetry: a new recipient NAMESPACE costs nothing (alias string + registry row); a new CARRIER is a code+migration event, because a carrier exists only when a door can move bytes and emit attempt evidence. Task closure never rides communications: tool-call doors emit `completed/failed/returned_blocked`; hooks and sweeps emit what no tool call sends (`orphaned_by_session_loss`, `expired`).

## 8. Task model

Work items carry two nullable WHERE-axis fields pointing at existing Lane-A vocabulary (2026-08-20 walk): `repo` (owner/name) and `project_key` (the projects.yaml slug — the tier map the sprint path already joins repo→project through). On work_items ONLY: attempts inherit the objective's target, events reference the item, communications stays communication-facts, workstreams span repos (coverage derives). Enables derived tier-vs-closure and scope-misroute flags.

Three-level split: **`work_item_id`** (durable objective) → **`assignment_id`** (one assignment to one actor/instance; reassignment and retry mint new attempts) → **`msg_id`** (one communication carrying it).

Event vocabulary (append-only `task_events`): `contract_created, dispatch_intended, transmission_failed, dispatch_submitted, receiver_acknowledged, accepted, rejected, progress, blocked(§19), resumed, completed, failed, cancelled, deadline_changed, superseded, reassigned, retry_created, orphaned_by_session_loss, recovered_after_restart, expired`.

**Activation is evidence-based** (derived, never stored): contract exists ≠ active. Derivation is two-level — attempt status (created-not-sent → dispatch-failed / pending-unacknowledged → open[-blocked] → closed(reason)) and item status over all attempts (done / active / stalled) — with **overdue and orphaned as overlay flags, never states**. `task_events.successor_id` links reassigned/retry_created to the successor attempt (and superseded to its superseding id); `task_events.session_uid` names the session the event concerns (reporter's for reports, the lost one for orphaned/recovered). Authored bodies (work items) REJECT over-cap as contract violations; only relayed content (communications) truncates-with-proof. `created-not-sent → submitted-unacknowledged → accepted/open → …closed`, with `dispatch failure` and `overdue`/`orphaned` distinguished. Supersession is a landed runtime fact (`a3644ca`/`36eb7a4`) and is first-class here.

`blocked` semantics: current runtime treats blocked as terminal (`dispatch-overdue.py` `_TERMINAL`); the operator model may need open-but-waiting — §19 lock item, recommendation `blocked_waiting` vs `returned_blocked` as two events, **no silent behavior change during migration**.

## 9. Registry snapshots v2 + metric-sample split

**Keyframes keep:** identity, declared topology (incl. optional groups), resolved configuration, equipment, permission posture, versioned hashes, vault binding, software versions. **Presence/health events take:** CPU/load, disk, thermal/under-voltage, boot/session state, vault ahead/behind freshness, RC state, pane activity, heartbeats. (Volatile data inside keyframes would invalidate the tens-of-rows/week envelope.)

**Walked columns (2026-08-22)** — `registry_snapshots` = envelope + `entity_type` (closed 6-enum) · `entity_uid` · `entity_alias` (name-as-it-was — tiny volume, human-listed; the cost argument that excluded aliases from metric_samples does not apply) · `tombstone: bool` (the ONLY stored operation — create-vs-update is derivable first-row-in-partition, and storing derivable state violates status-is-always-derived; `payload`/`payload_hash` are null iff tombstone) · `payload` (canonical JSON) · `payload_hash` (the write gate) · `cause` (generate|probe|equip|migration — the emitter's context, underivable) · `scan_id` · `vault_rev?` (null on probe rows). `declaration_observed` = envelope + `vault_uid` + `vault_rev` — two family columns, complete.

- SCD2 view: `PARTITION BY host_uid, entity_type, entity_uid ORDER BY occurred_at, ingest_seq` (**occurred_at, not observed_at** — first-hand snapshots carry null observed_at per §4; the earlier observed_at ordering was a consistency casualty of that ruling) — never bare `ts`; no dependence on timestamp uniqueness; indexes match the view.
- **Tombstones + scan completeness:** deletion is the only underivable operation, so only deletion gets a marker (`tombstone`). Tombstones are emitted only after a COMPLETE enumeration — the rule lives in the emitter, and **no completeness table exists** because partial scans are self-healing by construction: a crashed scan emits no tombstones (rule held) and leaves some entities un-snapshotted, which the next run's hash gate catches up. `scan_id` groups one scan's rows for audit.
- **Provenance without hash-suppression:** `declaration_observed` events record every newly observed vault revision even when resolved state is byte-identical — the provenance chain never disappears into the hash gate.
- **Canonicalization is a versioned spec** (encoding, Unicode normalization, numeric representation, null/default inclusion, path normalization, key ordering, serializer version, hash algorithm) with golden fixtures across supported versions. ("Sorted keys" vs "Pydantic field order" was a v1 contradiction; one canonical-bytes definition replaces both.)
- Human surface remains the field-level diff view; payloads bounded (verbatim short lists; hashes for bodies).

## 9b. Final data model — entity payloads and remaining families (finalized 2026-08-20)

This section supersedes the "v1 §10 normative for fields" pointer: the payloads below ARE the final field lists. Fields marked ⚑ carry privacy classification `sensitive` (§11: alias-first rendering, reveal is an explicit act, alias-only exports).

### HostPayload (keyframe; volatile → metric_samples per F12/F20)

```yaml
host_uid: str            # minted (F10); aliases: hostname, magicdns, operator_label?
aliases: {hostname: str, magicdns: str?, operator_label: str?}
os: linux|darwin;  arch: str;  kernel: str
ram_total_mb: int;  disk_total_gb: int          # stable hardware facts only
system:                                          # the claudlobby install (F14)
  claudlobby_version: str                        # git rev/tag
  claude_version: str;  node_version: str?;  python_version: str
  host_jobs: [{name: str, enrolled: bool}]
  plugins: [{name: str, version: str}]   # daily plugin update = estate-wide behavior change; record when
  emitters: [{name: str, armed: bool}]           # dormant-switch registry
  defaults_tier_hash: str                        # canonical hash of system.yaml defaults
declared_fleets: [str]   # fleet aliases from manifests — NEVER process inference
schema_version: str
# OUT to metric_samples: load, mem_available, disk_free, thermal/undervoltage flags,
# boot_time, tailscale up/ip.
```

### VaultPayload (keyframe)

```yaml
vault_uid: str;  alias: str                      # e.g. "example-claudron-vault"
role: primary|mounted                            # exactly one primary per host (F13)
mount_path: str
remote: str ⚑                                    # raw stored; rendered aliased
compat: {floor: str, cli_version: str?, ok: bool}   # claudron_compat
carries_fleets: bool
gitignore_safe: bool                             # doctor rung: runtime/ledger/.env covered
schema_version: str
# OUT to metric_samples: behind/ahead/last_fetch freshness (a failed fetch must
# never render as "up to date" — sample status carries fetch_failed).
```

### FleetPayload (keyframe)

```yaml
fleet_uid: str;  alias: str;  service_prefix: str
mission: str?                                    # the one-paragraph anchor, verbatim
mission_file: {path: str, content_hash: str}?    # charter drift via daily probe hash
manager: str | [str]                             # bot alias(es) — F5 scalar
groups:                                          # OPTIONAL logical layer (F5) —
  - {name: str, manager: str, members: [str], mission: str?}   # UI hides when len==1
org_edges: [{bot: str, reports_to: str?}]        # + manages edges, verbatim
roster: [str]                                    # every bot alias
defaults_summary:
  model: str;  effort: str?;  account: str
  list_tier_hashes: {skills: str, mcp: str, guardrails: str, protocols: str,
                     expertise: str, permissions: str, hooks: str}   # bounded
env_keys: [str] ⚑                                # names ONLY, never values
jobs: [{name: str, enrolled: bool}]
plugins_additional: [str]
vault_binding: {vault_uid: str?, path: str}      # resolved claudron_vault_path
telegram: {group_alias: str}? ⚑                  # raw chat id sensitive-separate
declared_hash: str;  vault_rev: str?;  schema_version: str
```

### ProjectPayload (keyframe — the third config tier, declared in projects.yaml)

```yaml
project_uid: str;  key: str                      # kebab slug = the PROJECT_TIER_<SLUG> env identity
fleet_uid: str                                   # the DECLARING fleet — grain is (fleet, key);
                                                 # same-key/different-tier across fleets is FLAGGED, never resolved
title: str;  repos: [str]                        # owner/name — "the join key mapping work to this project"
tier: auto|review|preview|human                  # governance: tier changes are the SCD2 payoff
validation_hash: str                             # bounded canonical hash of the validation block
mission_file: {path: str, content_hash: str}?    # same charter-drift pattern as fleets
declared_hash: str;  vault_rev: str?;  schema_version: str
# No projects.yaml = legitimately zero projects (empty state, not error).
# work_items.project_key stays a STRING at ingest; the reg_projects join
# happens at derivation — undeclared key = warn + trust metric, never reject.
```

### LibraryItemPayload (keyframe — what bots are made of)

```yaml
library_item_uid: str
category: str                # expertise|skills|mcp|guardrails|protocols|tools|voices|...
name: str;  source_tier: shared|fleet-overlay
fleet_uid: str?              # set for overlay items — the declaring fleet
content_hash: str            # canonical hash of the item's file(s)
title: str?;  description: str?   # frontmatter, bounded
declared_hash: str;  vault_rev: str?;  schema_version: str
# WHY an entity: skills reach bots via SYMLINK + live reload (reload-fleet,
# daily) — content changes under every equipped bot with NO generate, NO
# composed-hash change. Snapshotted at generate AND by the daily probe,
# closing the equipment×behavior loop at item-content grain.
```

### BotPayload (keyframe)

```yaml
actor_uid: str            # the logical specialist (survives restarts/moves)
bot_instance_uid: str     # THIS supervised install on THIS host
alias: str                # "bot:<fleet>/<name>" — what doors stamp, humans read
display_name: str?;  fleet_uid: str
account: str;  service: str          # BOT_SERVICE unit/socket name
model: str;  effort: str?
org: {mission: str?, reports_to: str?, manages: [str], group: str?, scope_hash: str?}
equipment:                # verbatim short lists; each id links into the library
  expertise: [str];  voice: str?;  skills: [str];  mcp: [str]
  integrations: [str];  guardrails: [str];  protocols: [str]
  resources: [str];  lessons: [str];  principles: [str]
  post_actions: [str];  tools: [str];  plugins: [str]
posture: ⚑                # the security surface — sensitive as a block
  permissions_mode: acceptEdits|dangerously_skip   # dangerously_skip badged loud,
                                                    # severity=high + remediation text
  tool_allow: [str];  tool_deny: [str]
  sandbox: {enabled: bool?, auto_allow_bash: bool?, config_hash: str}
  permissions_grants: {count: int, hash: str}
  hooks: [{event: str, matcher: str?, cmd_hash: str}]
  env_keys: [str]                                   # names only
  rc_enabled: bool
  telegram: {chat_alias: str?, require_mention: bool}
  git_credentials_profile: str?
schedule: {briefings: [str], sprint: str?}
vault_binding: {vault_uid: str?, path: str}?     # bot-tier override else fleet
composed_hashes: {claude_md: str, bot_conf: str, mcp_json: str, settings_local: str}
declared_hash: str;  vault_rev: str?;  schema_version: str
# OUT to metric_samples: session up, bridge up, RC live, pane activity, RSS.
```

### MetricSample (F20 — the volume lane; table `metric_samples`, walked 2026-08-20)

*(Renamed from `presence_health_events`: rows are neither presence nor events — they are **samples of levels**. "Presence" now names the Lane C LIVE derivation — is-it-up-right-now, in-memory, poller-fed, never a table — and "events" stays reserved for detections per the system_events reading rule.)*

```yaml
# envelope +
subject_kind: host|vault|fleet|actor|bot_instance|session   # shared vocabulary with system_events
subject_uid: str          # NO subject_alias — deliberate asymmetry vs
                          # communications/system_events: volume lane, rows are
                          # aggregated into charts and never read singly; display
                          # joins the registry once, not per row
metric: str               # registry-governed (METRIC_NAMES seed carries
                          # {name, unit, description} — unit lives in the
                          # registry, never on rows)
value: json               # number | bool | string | object (load triplet); hot
                          # numeric metrics promote to indexed generated columns
                          # on evidence
status: ok|warn|alert|null  # THE EMITTER'S JUDGMENT, recorded as a claim with
                          # attribution — thresholds live in per-emitter,
                          # fleet-overridable config, so the registry CANNOT
                          # honestly own this mapping (contrast
                          # system_events.severity, which it can); null = plain
                          # sample, no judgment made
```

Emitters (all existing machinery, pointed at emit): bot-vitals, keepalive heartbeats, fleet-memory-check, host-health-check, disk-monitor, the vault-freshness probe, creds-check. Seed metric registry: `host.load` `host.mem_available_mb` `host.disk_free_gb` `host.thermal_flags` `host.undervoltage` `host.boot_time` · `vault.behind` `vault.ahead` `vault.last_fetch_age_s` `vault.fetch_failed` · `bot.session_up` `bot.bridge_up` `bot.rc_ok` `bot.pane_last_change_age_s` `bot.heartbeat` `bot.rss_mb` · `host.job_ran` (one sample per machinery run via the shared lib-common emit hook — EVERY run leaves a trace; stale-emitter detection = expected job_ran absent; runs are LEVELS here, detections stay system_events) · `env.key_state` (emitted by creds-check runs: `{tier, key, state: present|empty|absent}` — names never values; the #1213 present-but-empty class as observation).

Sizing: ~21 bots + host at roughly per-minute cadence ≈ 30–45k rows/day ≈ a few MB/day — SQLite-trivial with the `(subject_uid, metric, ingest_seq)` index. **Retention ruled (2026-08-20): raw samples 30 days — the incident-join window; NO rollup family in v1.** Invariant correction (supersedes the earlier "rollups are Lane C and survive" line, which was self-contradictory): a rollup that outlives its aged-out raws is not rebuildable and would silently become authoritative — so if long-horizon trends ever earn persistence, rollups enter as a **Lane B fact family through emit**, never as a Lane C view. Within the 30-day window, latest-per-metric and windowed aggregates remain ordinary Lane C derivations.

### SystemEvent (F19) — walked 2026-08-20 under earns-its-place

*(Renamed from `lifecycle_events` during the walk: the emitters are the `system:<job>` actors — table name and actor grammar now agree — and "lifecycle" is freed to mean only task/message lifecycles, dissolving the F8 "lifecycle timeline" collision.)*

```yaml
# envelope +                (emitting script IS the envelope's `emitter` —
#                            the old `source` column was redundant and is KILLED;
#                            legacy import maps JSONL source -> emitter)
subject_kind: host|vault|fleet|actor|bot_instance|session|null
subject_uid: str?;  subject_alias: str?    # nullable as a TRIPLE; host events
                                           # (under-voltage, disk) name the host,
                                           # not a bot — the kind field is what
                                           # makes that expressible
type: str                 # registry-governed (F19): seeded from events.py
                          # CRITICAL_TYPES + the Phase-2b emitter inventory;
                          # unknown types INGEST + trust-counter + doctor listing
severity: critical|notice|null    # REGISTRY-OWNED, point-in-time: ingest stamps it
                          # from the package-owned seed module (plane/registries.py);
                          # callers cannot set it; unknown type => null
data: json                # bounded diagnostic payload (existing events' data{}
                          # verbatim); over-cap => data_truncated flag ONLY — the
                          # communications sha-proof triple is deliberately NOT earned here:
                          # communications bodies are the record of what was said, system-event
                          # data is pointer-grade diagnostics whose full text lives
                          # in the emitting script's own logs
data_truncated: bool
```

Reading rules (all three load-bearing): **events are DETECTIONS, never states** (overdue_dispatch here records "the watchdog fired at T"; live overdue-ness is only the task derivation's flag; session_missing = detection => lifecycle, session-up = level => presence). **Repeated detections are repeated rows** — debounce is the emitter's policy (fleet-pulse's existing debounce preserved), grouping is the derivation's job; a dedup column would smuggle state into a fact table. **Pairing is derivation** — bridge_down→bridge_heal matches by subject + type-family over time; a resolved_by column would let one row claim knowledge of a later row. Cross-table demo of `causation_id`: a FLEET ALERT is TWO rows — the lifecycle detection + the alert-class communications intent whose `causation_id` = the detection's `event_id` — so "why did this alert exist" is a click-through, not a grep.

### WorkstreamContract + WorkstreamEvent (F6, F21) — walked 2026-08-22

```yaml
workstream_contract:      # envelope + — the contract row IS "opened" (F21)
  workstream_id: str      # existing id scheme preserved, deliberately pattern-free
                          # (the single-writer mints; format is foreign vocabulary,
                          # same stance as project_key)
  title: str;  goal: str?
  owner: str?             # alias -> actor_uid; unowned workstreams exist
  opened_by: str          # alias -> actor_uid
  project_key: str?       # kickoff-declared association ("this campaign is for
                          # acme-shop") — a kickoff-time fact, which is what an
                          # immutable row is FOR; nullable so cross-project
                          # campaigns stay expressible. The work-item derivation
                          # survives as the observed complement; declared-vs-
                          # observed mismatch is a flag (org-overlay pattern).
                          # Concedes the earlier derivation-only ruling, which
                          # failed exactly when wanted most: a fresh campaign has
                          # no work items, and tagging is optional
  # Observed project coverage still derives via member work items'
  # repo/project_key — spanning campaigns need no special case.
  # No mission link (fleet_uid reaches it in one hop) · no repo/project
  # (campaigns span repos; coverage derives from member work items) · no
  # initial horizon (staleness = age-since-last-event vs fleet-policy window,
  # overridden by latest renewed_until — a stored deadline would duplicate it)

workstream_event:         # envelope +
  workstream_id: str
  event: progressed|renewed|blocked|unblocked|closed|archived|plan_linked|plan_unlinked
  actor: str?             # alias -> actor_uid
  plan_ref: str?          # meaningful on plan_linked/plan_unlinked: an AUTHORED
                          # plan artifact (repo-doc path or issue URL), pattern-free
                          # foreign vocabulary. SET SEMANTICS (settled 2026-08-22 on
                          # operator usage: epics carry ~10 live planning docs at
                          # once): plan_linked adds to the campaign's plan set,
                          # plan_unlinked removes; current set = linked - unlinked,
                          # plural whenever reality is. Supersession is the pair
                          # unlink(v1)+link(v2) — no special case. No role/anchor
                          # column: which doc is spec vs phase plan is the docs'
                          # own business (the epic links its phases); `note` labels
                          # a link when wanted. The plane lists, never ranks.
                          # Kickoff door emits contract + first plan_linked.
                          # Chain: plans -> workstream -> project (transitively
                          # attached, no plans table, no frozen columns)
  note: str?              # authored, small cap, REJECTS over-cap; carries the
                          # blocked reason and the closed disposition — the old
                          # blocked_reason column is KILLED as pure duplication
  renewed_until: ts?      # meaningful on renewed
  # No session_uid — workstreams are not session-bound, no orphan class
  # (contrast task_events, where it earned its place for orphan tracking)
```

Vocabulary notes (deliberate, not drift): **no blocked split** — F17's waiting/returned distinction exists because a task has an assignee to hand responsibility back to; a campaign has no such party. **`unblocked` is an expansion** — today's verbs have block and no unblock; the shim maps nothing to it until the door gains the verb. **`plan_linked`/`plan_unlinked` are likewise expansions** (unlink exists because a typo'd ref must be correctable append-only). **Plan STATUS is never stored**: its SSOT is the doc itself (phase markers + archive convention, already consumed by implement-plan/session-resume); the observed lane structurally cannot hold it (status change = UPDATE or a foreign event family); campaign motion is answered by the plane's OWN events. Plan-status-in-UI, if ever wanted, is a render-time read of the anchor doc — displayed live, stored never. Doc-side `workstream:` frontmatter is the doc ecosystem's complementary convention (clauDNA/index territory); the plane's authoritative graph is the event trail, and a frontmatter mismatch is a discoverable inconsistency, not something the plane resolves. **`closed` carries no outcome enum** — goal-achievement disposition is Phase 5's outcome-labelled-learning seam; `note` carries prose until a structured `outcome` field earns ratification there.

Verb mapping 1:1 (`open`→contract · `progress`→`progressed` · `renew`→`renewed` · `block`→`blocked` · `close`→`closed` · `prune`→`archived`). Status (`active|stale|blocked|closed|archived`) is always derived — contract × events × clock × policy window, never stored. **Transition mechanics:** the `workstream-update.sh` shim writes the db through emit AND regenerates `workstreams.json` as a Lane C projection, so `brief.py`, the fleet-pulse stall checks, and sprint selection keep reading the file unchanged until each is repointed; the file's authority ends when the shim lands, its existence when its last reader moves.

### SessionDigest / SessionUsage (adopted as-is, usage pilot-pending)

```yaml
session_digest:           # envelope + — transcript-digest.sh's existing rubric verbatim
  session_uid: str;  actor_uid: str
  status: ok|skipped
  context: str?;  worked: str?;  failed: str?;  would_change: str?;  reusable: str?

session_usage:            # envelope + — transcript-usage.py's existing axes verbatim
  session_uid: str;  actor_uid: str
  tokens: {input: int, output: int, cache_read: int, cache_write: int}
  protocol_sensitive: int;  cost_weighted_total: int
  comms_share_est: int?
# utilization_windows: {bot_instance_uid, window_start, window_end, busy_pct}
# USAGE LANE IS PILOT-PENDING (Phase 3): if native OTel metrics answer the
# mapping/duplicate/gap questions, the transcript parser retires and this
# family is fed by the OTel adapter — the MODEL stays, the source changes.
```

### Lane C — final derived set (closes the walk)

`reg_hosts / reg_vaults / reg_fleets / reg_bots / reg_projects / reg_library_items` (current-state, hash-verified, disposable) · SCD2 `*_history` views (F16 windowing over snapshots) · `registry_changes` field-level diff view · `task_status` view (evidence-based activation, §8) · `workstream_status` view + `workstreams.json` compatibility projection (transitional, F6) · presence (live, in-memory — pollers + latest samples) · in-window sample aggregates (never outliving raws) · trust metrics (emit/committed/spooled/rejected · duplicate ids · oldest spool age · replay failures · dual-write mismatches · raw-by-caller · unacked-delivery age · projection lag · system-event-unknown-type count · provisional-actor count) · interaction-density graph (never "observed org") · FTS index (permitted content only, completeness-stated).

### Vocabulary governance census (2026-08-20 walk)

Three mechanisms, counted: **12 closed vocabularies** — code-governed enums (Pydantic `Literal` + DDL `CHECK`, pinned by the parity test): message_class · command_type · transmission state · carrier · task event · workstream event · stream kind · privacy level · snapshot cause · entity type · identity kind · subject kind (shared: metric_samples + system_events) · sample status; changing one = code + migration, on purpose. **2 open registries** — system-event types (with severity) and metric names, both seeded from ONE package-owned module (`plane/registries.py`), warn-on-unknown at ingest, additions by PR; **no vocabulary registry tables in v1** — a table plus its management door arrive together with #903 if runtime mutation is ever needed (a table without its door is a constant with a sync-bug surface). QoL surfaces for the seed, in order of cheapness: `claudlobby plane registry` prints it; the `plane schema` export carries it to the UI; and if ad-hoc SQL wants it joinable, a **Lane C projection** synced from the seed at plane start (rebuildable, never written by anything else) is the census-compatible "printed to a table" form. **1 identity table** — `identity_registry` is not a vocabulary: runtime-minted alias→uid mappings, never seeded. Lane A vocabularies (project tier, fleet.yaml fields) keep their existing `known_values.py` + validator governance; the plane adds nothing there.

**With this section, every model in the system is finalized.** Remaining open items are implementation-owned only (§19: canonical-bytes golden fixtures, ingest benchmark, DDL mechanics, Claudron#145 answers).

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

- Doors become shims over `communication-send` (identical interfaces, canary-able per bot). `pane_send_verified` emits transport-attempt evidence when handed a `msg_id`; otherwise ledgers `raw_control` with caller provenance.
- **Raw rate is tracked by caller class** (dispatch/report vs keepalive/pulse/briefing/sweep control-plane) — control traffic is classified, not zeroed; a global zero was the wrong target. Phase 0 re-inventories every callsite against current main.
- **Dual-write canary:** one durable id minted before either sink; the sink that determines command success is declared; per-sink results recorded; reconciliation by id; mismatches exposed — never inferred equivalence from timestamps+text.
- **Backfill:** compare a clean semantic epoch (old ledgers as read-only legacy history; import only where provenance is adequate) against broad first-boot backfill — deterministic import ids (file identity + offset + content hash), `source=legacy`, batch + confidence, malformed records preserved as gaps, restartable/idempotent, dry-run counts, **never silently infer task closure**. Decision falls to the implementation plan with a stated default: clean epoch + selective import.

## 14. Performance & dependency gates

Before locking implementation: cold/warm emit p50/p95 on the Pi · 20–25-bot burst · concurrent system-event + communications writers · `busy_timeout`, transaction mode, retry budget, WAL checkpoint policy, `synchronous` mode specified · disk-full behavior · startup/import time. Dependency/capability matrix for FastAPI/Pydantic/ttyd/Tailscale/frontend: optional UI features degrade without disabling the core ledger. Thumbnail capture: one bounded backend sampler per pane, cached — never multiplied by browser count.

## 15. Schema compatibility, testing, trust metrics

- Shell doors are the highest-risk clients: accepted schema versions (N/N-1), unsupported-future behavior, stable JSON input, exit codes, fixtures, downgrade refusal, pre-migration backup + restore verification. TS-type regeneration CI-verified.
- **Test matrix (pre-substrate, not post-UX):** empty-db migration · migration from every released schema (obligation begins at cutover — pre-cutover schemas are never "released") · downgrade refusal · append-only enforcement · envelope constraints · duplicate `event_id` replay · concurrent emitters · timestamp ties · out-of-order producer clocks · complete-vs-incomplete scans · tombstones · canonical-hash golden fixtures · **every communication crash boundary** (send-succeeded/record-missing; record-exists/send-not-attempted; unknown; ack; duplicate suppression) · `SQLITE_BUSY` · disk full · malformed spool item · poison/dead-letter · drainer restart · dual-write mismatch · idempotent legacy import · projection rebuild parity · OTel duplicate/delta fixtures · collector outage · real shell door → CLI → SQLite → reader integration.
- **Trust metrics:** emit attempts, committed, spooled, rejected, duplicate ids, oldest spool age, replay failures, dual-write mismatches, raw-by-caller, unacknowledged-delivery age, projection lag, migration integrity failures.

## 16. v1 UI (F8)

Three questions: *What needs me now? What happened, and why? What changed or became untrustworthy?*

- **Attention queue:** failed dispatches, unacknowledged tasks, overdue, orphaned, broken emitters/spool.
- **Lifecycle timeline — the channel:** the communications stream threaded by work item / reply chain; full bodies per F7; FTS; one message's complete lifecycle (intent → attempts → ack → reports → closure) inspectable end to end.
- **Trust/gaps surface:** gaps counter (every gap: which caller, why unclassifiable, which door should replace it, communication vs machinery), malformed counters, spool state, dormant emitters, provenance/freshness badges.
- **Thumbnail grid + one read-only live pane:** capture-pane sampler + ttyd `attach -r`; retained against review advice — substrate-independent, weekend-scale, and the founding ask ("is my fleet alive" precedes "what needs me").
- Minimal identity context (fleet/bot cards enough to navigate) — full org chart, library, equipment, utilization, PWA deferred to Phase 6.
- **Required panel states everywhere:** first load / legitimately idle / emitter disabled / stale / partial source / malformed / unreadable / daemon disconnected / reveal denied / unknown — with last-successful-observation, provenance, freshness, remediation. **Never render zero when the source is absent** (brief's degraded/omitted philosophy). Design pass owes: semantic state colors with non-color equivalents, stable ordering under live updates, preserved focus, reduced-motion, keyboard nav, WCAG contrast, accessible alternatives to ANSI. Mobile intent: **awareness/triage only** in v1.
- The plane consumes the `brief` service layer for its joins wherever brief already answers the question.

## 17. Operator experience

Golden path: `claudlobby plane init | start | status | doctor | open`. Clocks: existing fleet → useful dashboard < 5 min; returning operator after 3 months → health/URL/next action < 60 s; symptom → understood lifecycle < 10 min. `/healthz` + readiness: schema version, db path, last successful write/projection, spool depth + oldest age, migration state, collector/OTel status, stale-emitter detection, exact corrective commands. The plane never alerts through the fleet it observes; it has its own local service-health path ("browser does not load" is not failure observability).

## 18. Phased sequence

- **Phase 0 — reconcile facts:** ✅ mostly done in-session (baseline refreshed to `e3b6347`; capabilities re-verified; landed prerequisites removed). Remaining: callsite/state-store re-inventory against current main.
- **Phase 1 — lock the semantic kernel:** rule §19's items; ratify identity, envelope, ordering, privacy, vocabulary, communications model, task state machine, spool, schema compatibility, workstream migration. **No UI.**
- **Phase 2 — headless vertical slice:** `task contract → message intent → transport attempt → acknowledgement → progress → completed/failed/blocked/overdue/orphaned`, exposed via CLI + SQLite + one reader/service layer + doctor/status + projection rebuild. Dual-write canary beside JSONL.
- **Phase 2b — registry lane (the host/vault/fleet/bot walk made real):** migration 0002 adds the remaining construct tables `registry_snapshots` and `metric_samples` plus the `workstreams` construct (stream kinds — workstream/system/declaration — were declared in 0001's events table) (envelope-bearing, F16 pattern; writes ride the same emit spine). Pydantic payload contracts for the six entities — **field lists FINAL in §9b**, reflecting the review deltas: minted uids (F10), volatile fields OUT to presence/health (F12), `FleetPayload.groups` (F5), BotPayload separating actor/instance/session. Emitter moments: `generate` (cause=generate, carries `vault_rev`), the probe loop (cause=probe: hardware + system facet), `declaration_observed` on every newly seen vault revision even when resolved state is unchanged. Scan semantics: `scan_id` for audit grouping; tombstones only from complete enumerations (emitter-owned rule; partial scans self-heal via the hash gate). Projection loader rebuilds `reg_hosts/vaults/fleets/bots/projects/library_items` idempotently (hash-verified against files); SCD2 + `registry_changes` views. **Closes Phase 1's identity loop:** the generate-time pass confirms provisional actors (`provisional=0`) and mints `bot_instance_uid`s, so lazy-minted identities stop being provisional the first time the registry observes the declared roster. Doctor surfaces: provisional actors, composed-hash drift, reconcile-check failures. Sequenced after Phase 2 (the slice stays narrow) and **before Phase 4, which renders identity context from these projections**; Phase 3 can run in parallel with it.
- **Phase 3 — commodity telemetry pilot:** OTel + LangSmith on one canary bot; delete redundant roadmap surfaces on evidence.
- **Phase 4 — minimal operator plane:** F8's five surfaces over the proven slice.
- **Phase 5 — organizational learning:** deliberations, independent contributions, synthesis, preserved dissent, decisions, outcome joins — only then consensus-learning claims.
- **Phase 6 — broader cockpit:** org/interaction graph, equipment, library, utilization, PWA, management verbs (equip first, through git), terminal breadth.

## 19. Phase-1 items — status after the 2026-08-19 ratification pass

Operator-ruled (now locked as forks F16–F18): ~~envelope physical form~~ → **typed tables + shared `ingest_seq`** · ~~`blocked` semantics~~ → **two events, legacy maps terminal** · ~~backfill posture~~ → **clean epoch + selective import**.

Remaining — technical, owned by the implementation plan, no operator ruling required:

1. **Canonical-bytes spec** — full definition (encoding, Unicode normalization, numeric representation, null/default inclusion, path normalization, key ordering, serializer version, hash algorithm) + golden fixtures.
2. **Ingest implementation** — direct writer vs Unix-socket daemon, decided by the §14 Pi benchmarks; CLI contract identical either way.
3. **Exact DDL for every Lane-B family** — mechanical consequence of F16 + the model sections. *Schema-change regime — **disposable-until-cutover** (2026-08-20 walk, follows from F18):* pre-cutover the db is throwaway — JSONL remains the record (dual-write), rows are validation exhaust, and a schema change is DELETE-AND-RECREATE with schema files edited freely; no migration ceremony, no compatibility obligations. **Cutover (the F18 epoch start, per fleet) is the explicit event where discipline activates**: applied files freeze, changes become new numbered files, downgrade-refusal arms, and §15's migration-test obligations begin. What exists from day one is only the version stamp + ~30-line runner — the same code in both regimes (fresh db: replay files in order = create the schema), kept so cutover has a hook instead of a retrofit. No autogen, no down-migrations, no data transforms, ever. Split by phase: kernel = **migration 0001** (ingest ledger, identity, communications, work_items, assignments, the `events` stream with all five kinds declared), owned by the Phase-1 plan; remaining constructs (`registry_snapshots`, `metric_samples`, `workstreams`) = **migration 0002** (stream kinds need no DDL — declared in 0001), owned by the Phase-2b plan (§18) — entity payload FIELDS are final in §9b.
4. **Claudron confirmations** — `fleets/` namespace tolerance; multi-vault capability. Filed: Claudron#145.

## 20. References

v1 draft (audit trail): `documentation/plans/2026-08-17-observable-plane-design.md`. External review: reconciled 2026-08-18; all factual claims verified (repo: `git log` on the five named commits; ecosystem: LangSmith trace-claude-code docs, Deep Agents async-subagents docs, code.claude.com monitoring-usage). Research memo: artifact "The Observable Plane" (2026-08-08) — note its §span-tracer claims predate the LangSmith plugin and are superseded by §12 here.
