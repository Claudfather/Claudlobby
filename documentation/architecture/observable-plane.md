# The observable plane

The plane is the fleet's flight recorder: one append-only SQLite database per
host that records what the fleet did — every dispatch, report, transmission,
heartbeat, registry keyframe and operator message — as typed facts with minted
identities, and derives everything else (open work, attention, presence,
utilization) from those facts at read time. The operator plane (`claudlobby
plane view`) renders it; `brief` answers from it; the legacy JSONL ledgers are
gone — retired reader by reader (the F18 cutover, below) and then removed
outright with the F18 closure (R1: no door writes a file any more).

Design of record: `documentation/plans/2026-08-18-observable-plane-design-v2.md`
(the forks F1–F18 are LOCKED there; this page describes what shipped). Phase
plans: `2026-08-2x-observable-plane-phase-*.md`; the cutover walk:
`2026-09-02-plane-cutover-f18-design-walk.md`. The view daemon's runbook:
`documentation/runbooks/plane-view.md`.

## Where it lives

| Path (under the host root, `CLAUDLOBBY_ROOT`) | What |
|---|---|
| `state/plane/plane.db` (+ `-wal`, `-shm`) | the database, WAL mode |
| `state/plane/capture.json` | per-fleet capture policy: `metadata` (default, bodies stripped) or `full` |
| `state/plane/ingest.sock` | the ingest daemon's socket (`claudlobby plane serve`) |
| `state/plane/spool/` | the filesystem spool — the valve that must not depend on the db it protects |

One database per host. A fleet is a partition inside it (the `fleet_uid`
column on every row); cross-host federation (the Pi's plane joining the
Mini's) is deferred until the Pi's SSD fix.

## The families

Ten tables, one per family, all sharing the common envelope (`origin`
live|legacy, `import_batch`, `confidence`, `source_ref`, `ingest_seq` — the
ordering authority — plus host/fleet uids, `occurred_at`/`observed_at`/
`ingested_at`, correlation and trace ids). The vocabularies are CLOSED enums
enforced by the pydantic wire contracts (`claudlobby/plane/contracts.py`); an
unknown token is a `ContractViolation`, never a silently stored string.

| Table | Family | Notes |
|---|---|---|
| `communications` | a message between actors | `message_class` (task_request, report, question, answer, alert, notice, briefing, nudge, acknowledgement, chat, config_change, raw_control), `command_type` for dispatches (task, cancel, compact, restart, query), reply/supersedes chains, `body` under the capture policy |
| `work_items` | a unit of work | created by a dispatch, linked from its assignment and communication |
| `assignments` | work assigned to a bot with a deadline | `expected_by`; `source_ref = dispatch-log:<task_id>` (or `dispatch-log:sha:<content key>` for an id-less dispatch) is the legacy join key |
| `workstreams` | the per-fleet workstream registry's plane twin | `workstream-update.sh` emits its construct + verb events |
| `events` | everything that happens TO a construct | `kind` = task (progress, completed, failed, returned_blocked, cancelled, superseded, reassigned, expired …), transmission (send_attempted, pane_submitted, carrier_queued, carrier_accepted, recipient_acknowledged, failed, duplicate_suppressed, unknown — `ATTEMPT_STATES`), workstream, system (registry-stamped severity; a DIAGNOSTIC cap of 16 KB on `detail`, `detail_truncated` when it hit) |
| `identity_registry` | aliases → uids | kinds host, vault, fleet, actor, bot_instance, session; provisional actors until a registry keyframe confirms them |
| `registry_snapshots` | entity keyframes × observed change | the SCD partition is (host, entity type, entity uid); `payload_hash` gates the write; a tombstone is the one stored operation |
| `metric_samples` | numeric time series | names from the `METRIC_NAMES` registry (`bot.heartbeat`, `host.load`, …); retention 30 days (`plane prune`) |
| `ingest_ledger` | one row per accepted batch | the dedupe horizon; the view's SSE cursor reads it |
| `comms_fts` | FTS over the channel | only permitted content (capture policy) |

`ingest_seq` orders everything; `occurred_at` is when the fact happened.
Derivations never write a table: status, attention, open sets, presence and
utilization are queries (`claudlobby/plane/queries.py` is the ONE definition of
each; `presence.py`, `utilization.py`, `inventory.py`, `orgchart.py`,
`expiry.py` are pure reads over them).

## Identity

Names are aliases; uids are truth. `bot:<fleet>/<name>` resolves to an actor
uid (and to a `bot_instance` uid per spawn); `fleet:<name>`, `host:<name>` and
`session` uids likewise. Resolution mints lazily at ingest, so the first
message from a bot the registry has not seen creates a PROVISIONAL actor that
the next `generate` (the registry scan) confirms or tombstones; `plane doctor`
counts the provisional ones. Session uids are transcript-stable
(`sess_` + sha256 of the platform session id — the bash derivation in
`lib/plane-session-start.sh` is pinned byte-identical to `ids.derive_session_uid`).

## The write spine

`emit()` / `emit_batch()` (`claudlobby/plane/emit_api.py`) is the one
programmatic write: validate the RAW envelope, apply the capture policy,
validate the captured form, then one transaction per batch (`ingest.py`) with
dedupe on `event_id` — a batch that mixes duplicates and new rows is REFUSED
("mixed state") rather than half-applied. Every bash door reaches it through
`lib/plane-emit.sh`, a ladder: the daemon's socket (`lib/plane-socket-client.py`
pre-mints event ids into a finalized file BEFORE the first attempt, so a commit
whose ack was lost classifies as duplicate, never a second row) → the cold CLI
(`claudlobby emit-batch`) → the spool (`claudlobby plane spool retry` drains
it). The daemon (`plane serve`, composed as the dormant `claudlobby-plane-daemon`
host service) owns INGEST AND NOTHING ELSE. `PLANE_EMIT_DISABLED=1` is the
harness exemption (a byte-identical no-op); every door calls the shim `|| log`
and never blocks its real action on it.

## The doors — who writes what

| Door | Records | Silenced by |
|---|---|---|
| `lib/dispatch-task.sh` | work_item + assignment + communication (+ the `pane_submitted` / `carrier_queued` / `failed` transmission after the send); `--supersedes` sets `supersedes_msg_id` and a terminal `superseded` on the retired assignment; an id-less dispatch is keyed by the content hash of its ledger line | `PLANE_EMIT_DISABLED=1` only — always on since F18 R1 |
| `lib/report-back.sh` | the report as a communication; task events on the assignment the legacy task id resolves to (`lib/plane-lookup.py`); an id-less terminal report closes the bot's open id-less dispatches | `PLANE_EMIT_DISABLED=1` only — always on since F18 R1 |
| `lib/keepalive.sh` | `bot.heartbeat` + `bot.session_up` metric samples per tick (presence's recorded half) | `PLANE_EMIT_DISABLED=1` only — always on since F18 R1 |
| `lib/plane-telegram-in.sh` / `-out.sh` / `plane-rc-relay-out.sh` (hooks) | the operator's inbound messages, the bot's replies, RC-relayed final answers, with honest transmission states (carrier `telegram-bridge`) | `PLANE_EMIT_DISABLED=1` only — always on since F18 R1 |
| `lib/tg-post.sh` | a `notice` communication + its transmission for every fleet post to Telegram (carrier `telegram-tgpost`, intent before the send, the outcome after) | `PLANE_EMIT_DISABLED=1` only — always on since F18 R1 |
| `lib/plane-session-start.sh` (hook) | the session uid + a per-process uid to `$BOT_DIR/data/.plane-session` | `PLANE_EMIT_DISABLED=1` only — always on since F18 R1 |
| `lib/plane-host-probe.sh` (host timer) | `host.*` metric samples (load, RAM, disk, Pi thermals) | `PLANE_EMIT_DISABLED=1` only — always on since F18 R1 |
| `claudlobby generate` (`registry_emit.py`) | registry keyframes for every composed entity; the `scan_completed` declaration that validates its tombstones | dormant until `PLANE_EMIT_ENABLED` in the fleet `.env` tier (the tier cascade, not `fleet.yaml env:`) — the flag's only meaning since the closure |
| `lib/workstream-update.sh`, `lib/briefing-trigger.sh` | workstream construct + verb events; briefing communications | `PLANE_EMIT_DISABLED=1` only — always on since F18 R1 |
| `claudlobby plane expire` (host timer) | a terminal `expired` on assignments overdue past the horizon — a Lane-B fact through normal ingest | `PLANE_EXPIRE_ENABLED` |

Dormancy is a compose-time fact where the composer can make it one (an unarmed
`unit: service` job composes NO units) and a self-gate where it cannot (host
timers are enrolled regardless, so `plane-prune.sh` and `plane-expire.sh` check
their own flag). The read side (`brief`, `plane view`) needs no flag.

## The read side

**Two fleets on one host (U, #1467).** The operator plane reads the fleet
dimension from the plane itself: `/api/fleets` from the registry's fleet
identities, every per-fleet route scoped through one fleet-axis predicate
(`queries.fleet_alias_range` in SQL, `inventory.fleet_of` in Python —
case-sensitive like the room's equality arms), a typed `unknown` state for a
fleet the plane does not hold, names qualified `fleet/name` wherever two fleets
meet, and an overview whose `open` is the matcher's own rule
(`OPEN_ASSIGNMENTS_AT_SQL`), so the strip and `claudlobby brief` can never
disagree on the same fleet. Details: `documentation/runbooks/plane-view.md`.

- **`brief`** (`claudlobby/brief.py`) — the fleet's one read door; its
  `degraded[]` envelope says which sections were LABELED or OMITTED and why
  (an unreachable source is never an empty answer — `source_state.py`).
- **`plane view`** (`view.py`, the `[plane-ui]` extra) — the operator plane:
  read-only by construction (`mode=ro` + `query_only`, no non-GET route),
  the channel threaded by work item, attention, identity cards, the thumbnail
  grid + one live pane, trust/gaps, SSE off the ingest-ledger cursor, `/healthz`.
  Composed as the dormant `claudlobby-plane-view` host service; Tailscale Serve
  fronts it.
- **`plane status` / `plane doctor`** — the health page and the pre-flight
  rungs (schema, provisional actors, tombstone validity, reconciliation).
  **These RUN
  `migrate()` and are therefore not read-only — and so do `plane registry`,
  `plane prune`, `plane expire` and `spool retry`** — a newer db refuses them
  (`DowngradeError`, rc 4) and an unmerged package's doctor (or registry read)
  will migrate a live db. Verify a branch on a live host only through the
  doors that open read-only: `brief`, `plane view`,
  and the stdlib readers below.
- **The stdlib readers** (`lib/plane-readers.py`, `lib/plane-lookup.py`,
  `lib/who-reviewed.py`) — the
  plane answered from bash doors without paying the package import: the open
  list and the overdue set (SQL pinned byte-identical to
  `queries.OPEN_ASSIGNMENTS_AT_SQL`), the resolver, the legacy-id join, the
  divergence check, the retirement. One open shared by all: the `mode=ro`
  URI first, and on CANTOPEN a plain connection held read-only by `PRAGMA
  query_only` — under the system `python3` the doors run, a read-only URI
  cannot open a WAL database whose writer has closed (it cannot create the
  shared-memory file), which is what a daemon restart looks like.

## The cutover (F18) — history

The plane replaced the JSONL ledgers (`state/dispatch-log.jsonl`, the per-fleet
`runtime/report-back.jsonl`, the per-bot event files, `keepalive.log`,
`workstreams.json`) one reader at a time between 2026-09-02 and 2026-09-05.
The transition machinery — the shadow comparison and its gate, the
`cutover_declared` / `legacy_write_retired` epochs, the `PLANE_READ_*` and
`PLANE_LEGACY_WRITE_*` flags and their composed stamps, the parity and import
doors — was deleted with the closure (#1467: R1 removed every writer, R2a the
matcher and the shadow, R2b the remaining readers, R3 the machinery). What
remains is the end state this document describes: **every door records on the
plane and nowhere else; every reader reads the plane and nothing else; an
unreachable plane refuses, never an empty answer.** The recorded
`cutover_declared`, `legacy_write_retired` and `shadow_parity_*` rows on a
host that lived through the transition stay registered so they still classify;
nothing emits or reads them. The per-chunk design record is the CHANGELOG and
the walk in `documentation/plans/2026-09-02-plane-cutover-f18-design-walk.md`.

## Operations

**Arming carriers** — `PLANE_EMIT_ENABLED` (the generate-time registry scan
only: every runtime door is always on since F18 R1, and `PLANE_EMIT_DISABLED=1`
is the harness exemption that silences one), `PLANE_EXPIRE_ENABLED`, `PLANE_PRUNE_ENABLED` (the two
host sweeps self-gate). Put them in the fleet
`.env` tier: sessions get them through `bot.conf`, timers through the composed
`Environment=` lines, and `generate` reads the same cascade. `env_tiers.armed`
is the one definition of "resolves to 1".

**Migrations** — `claudlobby/plane/migrations/NNNN_*.sql`, `user_version`-gated
(`migrations.py`); the daemon migrates at start, and so do `plane status` /
`plane doctor` / `plane registry` / `plane prune` / `plane expire` / `spool retry`. 0001 kernel · 0002 task-status index · 0003/0004
the fleet room · 0005 FTS · 0006 the registry lane · 0007 `assignments(source_ref)`
(the legacy join) · 0008 `events(actor_uid, occurred_at)` (progress grace, the
resolver's guard) · 0009 `events(fleet_uid, occurred_at) WHERE kind='system'` (Phase B: the fleet-events readers and the escalation window). A newer db refuses older code (rc 4), never downgrades.

**Retention** — `plane prune` ages `metric_samples` past 30 days by
`ingested_at` (the incident-join window); nothing else is ever deleted; no
VACUUM against a live daemon. `plane expire` is the attention queue's aging
sweep (7-day horizon), idempotent by construction.

**The rule every reader follows** — unreachable is not empty. A missing or
unopenable db, a fleet the plane has never seen, or a plane that holds no bot
of the fleet is REFUSED with a reason on stderr and a nonzero rc; an existing
source with zero rows is an answer. The refusal never rides stdout, because
`report-back.sh` and `fleet-pulse.sh` parse it.
