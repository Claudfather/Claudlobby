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
| `ingest_ledger` | one row per accepted batch | the dedupe horizon; `plane parity` and the view's SSE cursor read it |
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
| `claudlobby generate` (`registry_emit.py`) | registry keyframes for every composed entity; declaration events | dormant until `PLANE_EMIT_ENABLED` in the fleet `.env` tier (the tier cascade, not `fleet.yaml env:`) — the one place the flag still means something (R3 decides its fate) |
| `lib/workstream-update.sh`, `lib/briefing-trigger.sh` | workstream construct + verb events; briefing communications | `PLANE_EMIT_DISABLED=1` only — always on since F18 R1 |
| `claudlobby plane expire` (host timer) | a terminal `expired` on assignments overdue past the horizon — a Lane-B fact through normal ingest | `PLANE_EXPIRE_ENABLED` |
| `claudlobby plane cutover --reader` | `cutover_declared` (the epoch) | the operator's hand |
| `claudlobby plane import --apply` | the parity gap, `origin=legacy` under an `import_batch` | the operator's hand |

Dormancy is a compose-time fact where the composer can make it one (an unarmed
`unit: service` job composes NO units) and a self-gate where it cannot (host
timers are enrolled regardless, so `plane-prune.sh` and `plane-expire.sh` check
their own flag). The read side (`brief`, `plane view`) needs no flag.

## The read side

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
  rungs (schema, provisional actors, tombstone validity, reconciliation,
  each cutover flag against its declaration). **These RUN
  `migrate()` and are therefore not read-only — and so do `plane registry`,
  `plane prune`, `plane expire` and `spool retry`** — a newer db refuses them
  (`DowngradeError`, rc 4) and an unmerged package's doctor (or registry read)
  will migrate a live db. Verify a branch on a live host only through the
  doors that open read-only: `plane parity`, `plane import`
  (dry-run), `plane cutover` (it refuses before writing), `brief`, `plane view`,
  and the stdlib readers below.
- **The stdlib readers** (`lib/plane-readers.py`, `lib/plane-lookup.py`,
  `lib/who-reviewed.py --source plane`) — the
  plane answered from bash doors without paying the package import: the open
  list and the overdue set (SQL pinned byte-identical to
  `queries.OPEN_ASSIGNMENTS_AT_SQL`), the resolver, the legacy-id join, the
  divergence check, the retirement. One open shared by all: the `mode=ro`
  URI first, and on CANTOPEN a plain connection held read-only by `PRAGMA
  query_only` — under the system `python3` the doors run, a read-only URI
  cannot open a WAL database whose writer has closed (it cannot create the
  shared-memory file), which is what a daemon restart looks like.

## The cutover (F18) — from the JSONL ledgers to the plane

The legacy ledgers (`state/dispatch-log.jsonl`, per-fleet
`runtime/report-back.jsonl`) were written by every door while the plane was
read instead of them one READER at a time, each reader's flip a recorded state
machine (steps 1–5; history now — the closure that follows removed the
writers):

1. **Shadow** *(retired with the closure, R2a — there is no legacy side left to grade).* `plane shadow --record` compared, per bot and per reader, the
   legacy answer (the install's own `lib/dispatch-overdue.py`, through
   `brief`'s seam) with the plane's, classifies every divergence (`skew`
   inside the emit grace, `legacy_supersedes_pre_cutover`, `plane_superseded`
   and `plane_idless` — the two shapes where the plane knows MORE than the
   ledger: a supersession from another bot's dispatch, which the legacy matcher
   does not honour, and a sha-keyed construct the ledger cannot name; the heads
   are compared past every structurally explained row —
   `legacy_malformed_deadline`, `intentional`, else `divergence`) and records
   the comparison as a system event. The `plane-shadow` fleet timer did this
   every 10 minutes while armed; `--replay-hours N` front-loads it from history
   at the top-of-hour marks. Readers: `open` (the deadline-blind list),
   `overdue` (the watchdog's set), and `open_task` (the resolver — a streak
   mode over the open records, which carry the resolver's answers on both
   sides).
2. **Gate** *(retired with the shadow, R2a).* `plane shadow --gate` wanted 20 consecutive clean comparisons with at
   least one open→closed transition per (bot, reader); the resolver needs 200
   agreeing non-empty answers and a change (a stale head is a false
   completion). An idle fleet cannot meet it: a set that never changed proves
   nothing. `plane doctor` carried the rung; `lib/plane-shadow-check.py` was
   the watchdog's question (the fleet-pulse bridge paged a diverged latest
   comparison). All of it is deleted.
3. **Declare.** `plane cutover --reader open|overdue|open_task|unassigned|events`
   lands `cutover_declared`, anchored on the fleet's identity, as a DIRECT
   MOVE (operator ruling 2026-09-03: no backward compat, fix forward; the
   shadow gate went with the closure, R2a — `shadowed: false`,
   `gate_met: null`, the ruling recorded as the reason). A flip was TWO
   facts, this declaration AND the flag, for the readers that read one.
4. **Flip.** `PLANE_READ_OPEN=1` / `PLANE_READ_OVERDUE=1` /
   `PLANE_READ_OPEN_TASK=1` / `PLANE_READ_UNASSIGNED=1` / `PLANE_READ_EVENTS=1`
   in the fleet `.env` tier; `generate` composes the armed flags into
   `bot.conf` (the session carrier — `start-bot.sh` exports `bot.conf` and
   sources the tiers WITHOUT export) and stamps them on the timer units
   (`FLEET_JOB_ARMING`). **Since R2a the matcher reads no flag and no
   declaration: it reads the plane alone**, and an unreachable plane REFUSES
   (rc 3, empty stdout) — never a silent fallback — while the watchdog pages a
   refused reader. **R2b-1: every Python reader reads the plane alone** —
   `claudlobby report-back` / `events` / `workstreams` / `uptime` / `status`,
   `who-reviewed.py`, and brief's reports, alerts and workstreams sections
   (one rule, `brief.plane_conn`: no flag, no declaration, no retirement
   fact, no file; unreachable is not empty). Only fleet-pulse's escalation
   still reads `PLANE_READ_EVENTS` + the declaration; R2b-2 moves it, and R3
   removes the flags and this door.
5. **Retire the writes.** `plane cutover --retire-writes` refuses unless every
   reader is declared and records `legacy_write_retired`. Since R1 no door
   writes a ledger regardless — the plane is the only recorder and
   `PLANE_EMIT_DISABLED=1` the one silencer — so what the record still
   governs is the readers of a RETIRED ledger, which follow the retirement
   fact itself (chunk C3, `brief.plane_retired_conn`: `legacy_write_retired`
   naming the door, read on the plane — no flag, because a frozen ledger is
   wrong on the day it freezes) — until R2b-1, which moved `claudlobby
   report-back`, brief's unacked reports + `--ack`, `who-reviewed.py` and the
   workstream readers to the plane outright (the retirement fact now governs
   nothing; `retired_doors` goes with R3). Chunk A2 had already closed the
   last file-backed record: the workstream registry is materialized from the
   plane and its verb events are the writes.
6. **The closure (F18 R1, 2026-09-04 — operator ruling: no JSONL, no shims,
   no backwards compat).** With both fleets retired on every door, the legacy
   WRITERS were removed outright: no door writes a ledger, a per-bot event
   file, a keepalive file or a registry file; the four-fact machinery
   (`plane_write_retired`, `plane_retirement_covers`, the tier read, the
   detached emission) went with them, and `PLANE_LEGACY_WRITE_*` means
   nothing to a door (the composer still stamps it until R3). The plane is
   the ONLY record and it is ALWAYS ON: `plane_armed` arms unless
   `PLANE_EMIT_DISABLED=1` (the harness exemption), because an env-gated
   arming loses records the day the file is gone — measured on the data
   flip, where every restarted bot's pre-stop hook ran with no flag in its
   environment and wrote its `handoff_skipped` into a file the retirement
   had frozen. An unrecorded dispatch or report is said LOUDLY on stderr and
   the send proceeds (the send is the mission); an unrecorded workstream
   verb REFUSES (there is no file to fall back to). The workstream door
   materializes its registry from the plane inside its lock, starts a fresh
   fleet from the empty registry (`--or-empty`) and dedups against archived
   ids. The readers that still know how to read a file (R2) and the cutover
   machinery itself (R3) follow.

**Phase B — the bot-events ledger** (`data/events/fleet-*.jsonl` per bot,
`state/events/` for the fleet) moves as a direct move, no shadow.
`emit_fleet_event` (lib-common, every script's door) lands each fleet event
on the plane FIRST as a system event anchored on the bot's actor — or the
fleet, for a fleet-level receipt — by ALIAS (resolved at ingest), stamped
UTC, with the PROVENANCE `source_ref fleet-events:sha:<content key of the
legacy line>` and the detail `{source, legacy_ts, data}` so the plane
re-renders the legacy row byte for byte; every `emit_fleet_event` type is
registered with the severity `CRITICAL_TYPES` implies (critical pages,
notice records). The readers — `claudlobby events`, brief's alerts,
fleet-pulse's escalation loop, summary and read-back — select fleet events
by that provenance (never an event-name list) and serve the plane on
`PLANE_READ_EVENTS` AND the `events` declaration (the bash readers ask
`plane-lookup.py --declared events` and READ its rc; the rows come back
through `plane-readers.fleet_events` / `escalation`, one row rendering for
every reader, fleet-pulse in ONE read per window). An unreachable plane
under the flag is a third state, never the files: `claudlobby events` rc 3,
brief's alerts omitted with a `degraded[]` entry, fleet-pulse `not judged
this pass` + a debounced page + `unknown` in the summary — after the
retirement the files hold nothing, so a fallback would read an outage as a
quiet fleet. The JSONL append retired behind `PLANE_LEGACY_WRITE_EVENTS=0`
on the same four facts as the other doors until R1 removed it: the door now
writes the plane and nothing else, waited on only to
`FLEET_EVENT_EMIT_TIMEOUT_S` (a failed or reaped emission is disclosed as
"not recorded"; the shim's spool is the durability below that), because the
door runs inside every hot path and a wedged rung must never hold a tick.
Chunk B2 closes the two writers that never went through the door: the
keepalive tick's TRANSITIONS are fleet events (`keepalive_restart`,
`bridge_heal`, `keepalive_skip`, `keepalive_reload`; the per-tick verdicts ride
the heartbeat sample), the vitals hook lands `tool_call` / `session_event`
through `emit_fleet_event`, the reader-less `keepalive-<day>.jsonl` is gone
(R1), and `claudlobby uptime` reads the plane's heartbeat
samples + restart events once the events write is retired. `data-sweep.sh` and
`tail-fleet.sh --events` keep aging/tailing whatever files remain (hygiene over
files, not readers of record).

`plane parity` is the reconciliation door (matched / pre-go-live / unstamped /
stamped-lost per row, with the multiplicity a report legitimately has) and
`plane import` closes the gap (dry-run by default; attribution by the fleet's
own report ledger only, never guessed).

## Operations

**Arming carriers** — `PLANE_EMIT_ENABLED` (the generate-time registry scan
only: every runtime door is always on since F18 R1, and `PLANE_EMIT_DISABLED=1`
is the harness exemption that silences one), `PLANE_EXPIRE_ENABLED`, `PLANE_PRUNE_ENABLED` (the two
host sweeps self-gate), `PLANE_READ_*` (the flips). Put them in the fleet
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
