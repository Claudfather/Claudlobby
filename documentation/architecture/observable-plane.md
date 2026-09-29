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
| `state/plane/capture.json` | per-fleet capture policy: `full` (the shipped default — bodies recorded) or `metadata` (the opt-out — bodies stripped at the door, proof triple kept). A named fleet beats `*`; a malformed file fails loud and resolves to no mode at all |
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
| `events` | everything that happens TO a construct | `kind` = task (22 tokens, `TASK_EVENTS`: progress, completed, failed, returned_blocked, cancelled, superseded, reassigned, expired …, plus the task loop's two HUMAN acts — `escalated` (a manager raises the task for human guidance; NON-terminal, so the task stays open while the human decides) and `nudged` (the operator asks the manager to act) — each carrying its text in the detail (`question` / `reason`, CONTENT-capped) and the person in `by`), transmission (send_attempted, pane_submitted, carrier_queued, carrier_accepted, recipient_acknowledged, failed, duplicate_suppressed, unknown — `ATTEMPT_STATES`), workstream, system (registry-stamped severity; a DIAGNOSTIC cap of 16 KB on `detail`, `detail_truncated` when it hit) |
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
("mixed state") rather than half-applied. Public ingest uses
`claudlobby plane emit FAMILY --file FILE|-` or
`claudlobby plane emit-batch --file FILE|-`; `--json` selects the common result.
Exit 0 means committed or already present, while exit 6 means durably spooled
and still pending. Conditional writes can use `--require-commit` to refuse
spooling; an uncertain commit must be reconciled by event ID before retrying.
The hot bash path stays on `lib/plane-emit.sh`: its stdlib socket client
pre-mints event IDs and durably stages an unacknowledged batch for daemon
replay without starting the full CLI. The daemon (`plane serve`, composed as the dormant `claudlobby-plane-daemon`
host service) owns INGEST AND NOTHING ELSE. `PLANE_EMIT_DISABLED=1` is the
harness exemption (a byte-identical no-op); every door calls the shim `|| log`
and never blocks its real action on it.

**What does NOT fall down the ladder** is decided by one question — *would the
cold rung repeat this refusal?* A contract violation would (same validator); a
total failure would (same db). A **downgrade would not**, and treating it as a
verdict is what cost the estate 261 heartbeat samples across 18 bots in ~15
minutes on 2026-09-06 (#1485). A downgrade says the db is newer than the code
**the answering process loaded**, and the daemon is a long-lived process on an
editable install: a `git pull` swaps the files, the daemon keeps its old
modules, and the first migrating door (that day, `plane doctor` applying 0010)
leaves the daemon the only stale thing on the host. The cold rung is a fresh
interpreter on the install's *current* code and commits. So
`plane-socket-client.py` maps a `downgrade` reply to exit 5 — the shim's
fallback trigger — and the daemon itself **exits 4** on the condition, at
startup (immediately after bind, as the first statement inside the serve
loop's own try/finally, so the exit unlinks the socket and drops the lifetime
lock on its way out and nothing that WRITES the db runs first), on the first
request that hits it, or at an interval drain. Its supervisor relaunches it on
the current install; if the install was never updated it exits again and the
supervisor's throttle sets the cadence (systemd `RestartSec=5`, launchd ~10s),
while the cold rung keeps recording.

Two consequences worth stating plainly. The rc 5 also **arms the shim's wedge
marker**, so for the next `PLANE_WEDGE_COOLDOWN_S` (60s default) every
emission — every door's, not just this one's — skips the socket and goes
straight to the cold rung, then the socket is retried: slower, disclosed,
nothing dropped. And the client returns only 0/2/3/5, so the shim's
passthrough arm carries **2 and 3 only**; a `4` there was dead code. A
**cold-rung** downgrade still exits 4, at the tail where the shim returns the
CLI's rc verbatim — there the install itself is behind the db and no rung can
help.

**Cooldown staging (#1657).** Under load that cooldown did not damp: each
diverted emission spawned the package-importing cold CLI (`claudlobby --help`
alone took 1.8–4.9 s at load ~20 on the Pi), and those spawns kept the CPU the
daemon needed pegged, so it kept missing its deadline. The three emitters that
never read the result — `plane_emit_bounded` (every `emit_fleet_event`),
keepalive's heartbeat and the host probe — set `PLANE_EMIT_COOLDOWN_STAGE=1`,
and in a cooldown `plane-socket-client.py --stage-to` leaves their finalized
batch in `state/plane/staged/` instead (rc 6: durable, not yet in the plane).
The daemon replays staged batches on each loop tick through the same
`emit_batch()` a socket request runs, never through the spool's `drain()`,
which ingests its entries as-is because they are stored policy-applied and so
would skip the capture policy for a raw batch. The client stages only when that
directory exists (the daemon creates it at startup, so an older daemon never
gets one) and a connect probe finds a listener; otherwise it takes the cold
rung as before. Doors that read a non-zero rc as "not recorded" never opt in.
Staged depth is not yet a `plane doctor` rung. A stage killed before its rename
leaves `.<event id>.tmp` (plane_emit_bounded's 10 s reaper, inside the stage's
fsync), and the daemon replays one once it is an hour old: it is a finished
batch with pre-minted ids (#1657).

**The deadline follows who waits (#1693).** The client's total deadline is
1.0 s unless the caller's class says otherwise. `PLANE_EMIT_CLASS` is `hook`
(a live turn waits), `background` (nothing reads the result;
`plane_emit_bounded`'s default) or `door` (the outcome turns on the result),
and `PLANE_SOCKET_DEADLINE_HOOK_S`, `_BACKGROUND_S` and `_DOOR_S` set that
class's deadline; all three are unset by default. On one SD card both rungs'
commits wait for the same device, so a caller that misses during a stall gains
nothing from the cold rung but a process spawn, and costs every other door on
the host 60 s of cooldown. A caller that can afford to wait should. Every
exit 5 from a socket attempt is recorded in `state/plane/.socket-arms` with
its class, deadline, elapsed time, caller and cause: the marker itself holds
only a time.

There is no separate startup check: the daemon's first writes after bind —
the lifecycle receipt and the startup spool drain — go through `migrate()`,
which refuses a db newer than the code before writing anything, and that
refusal is what exits the daemon. Everything that touches the db runs after
`_bind()`, so a `plane serve` REFUSED for a bad `--socket` parent, or because
another daemon already holds the lock, never touches the plane at all. The
first build ran `migrate()` BEFORE bind, and a refused serve from a newer
checkout migrated the live plane on its way out — the very act that makes a
running daemon stale, with 0010's seconds-long write lock taken outside the
daemon lock.

**Deploying a migration, or a registry change.** A pull that carries one leaves
every resident process on the old modules. Since #1485 the ingest daemon repairs
itself, but bouncing it explicitly is still the fast path and is the only remedy
for a daemon predating that fix. **A registry change never repairs itself:**
there is no schema bump to refuse on, so the daemon keeps stamping severity from
the registry it loaded at start. A new critical type is stored with NULL
severity, invisible to every critical read, until the bounce (`crash_loop`,
#1774; rows stored meanwhile stay NULL):

```
launchctl kickstart -k gui/$UID/claudlobby-plane-daemon    # macOS
systemctl --user restart claudlobby-plane-daemon           # Linux
```

Confirm the bounce took (Linux): the timestamp must read later than the pull. A
new critical type's first row cannot be waited for, so nothing else shows it:

```
systemctl --user show -p ActiveEnterTimestamp claudlobby-plane-daemon
```

**Where the loop shows.** Not `plane doctor`: a newer db makes every
migrating door REFUSE at 4 through `_guarded` *before* a single rung prints,
so its schema rung is unreachable in exactly this condition. What an operator
gets is (a) the `REFUSED — plane.db user_version=N is newer than this code
(supports <=M)` line any migrating door prints (`plane status`, `plane
doctor`), which names both numbers, and (b) the daemon's own exit line, once
per relaunch, in `<root>/state/plane-daemon.log` (launchd, appended by the
composed plist) or the journal (systemd, `journalctl --user -u
claudlobby-plane-daemon`). Nothing records it on the plane — a process that
refuses the db cannot write a row about refusing it — and that log grows for
as long as the loop runs, which is until the install is updated.

There is deliberately **no doctor rung counting the loop**: it would render
only where the condition is absent, which is the dead-signal shape this
program keeps refusing.

## The doors — who writes what

| Door | Records | Silenced by |
|---|---|---|
| `lib/dispatch-task.sh` | for a TRACKED shape (a `task`, and a raw-text send which inherits type=task) work_item + assignment + communication; for a CONTROL type (`query` / `cancel` / `compact` / `restart`) the communication ALONE — an open assignment for a note that asks nothing is a row no report can close and it blanks the resolver head (#1491), so a control type mints none. Both carry the `pane_submitted` / `carrier_queued` / `failed` transmission after the send, and `--supersedes` sets `supersedes_msg_id` and a terminal `superseded` on the retired assignment whatever the type (the note retires its target even though the note itself is untracked); a raw-text dispatch is keyed by the content hash of its dispatch row | `PLANE_EMIT_DISABLED=1` only — always on since F18 R1 |
| `lib/report-back.sh` | the report as a communication; task events on the assignment the legacy task id resolves to (`lib/plane-lookup.py`); an id-less terminal report closes the bot's open id-less dispatches | `PLANE_EMIT_DISABLED=1` only — always on since F18 R1 |
| `lib/keepalive.sh` | `bot.heartbeat` + `bot.session_up` metric samples per tick (presence's recorded half) | `PLANE_EMIT_DISABLED=1` only — always on since F18 R1 |
| `lib/plane-telegram-in.sh` / `-out.sh` / `plane-rc-relay-out.sh` (hooks) | the operator's inbound messages, the bot's replies, RC-relayed final answers, with honest transmission states (carrier `telegram-bridge`) | `PLANE_EMIT_DISABLED=1` only — always on since F18 R1 |
| `lib/plane-dispatch-in.sh` (UserPromptSubmit hook) | a `received` transmission for a tracked dispatch, report, or briefing whose final-line plane marker reaches the receiving bot; records the received wire byte count and hash so delivery can be checked against the sender's proof, while ordinary prompts record nothing | `PLANE_EMIT_DISABLED=1` |
| `lib/tg-post.sh` | a `notice` communication + its transmission for every fleet post to Telegram (carrier `telegram-tgpost`, intent before the send, the outcome after) | `PLANE_EMIT_DISABLED=1` only — always on since F18 R1 |
| `lib/plane-session-start.sh` (hook) | the session uid + a per-process uid to `$BOT_DIR/data/.plane-session` | `PLANE_EMIT_DISABLED=1` only — always on since F18 R1 |
| `lib/plane-host-probe.sh` (host timer) | `host.*` metric samples (load, RAM, disk, Pi thermals) | `PLANE_EMIT_DISABLED=1` only — always on since F18 R1 |
| `lib/transcript-digest.sh` (SessionEnd hook) | one `session_digest` system event per finished session on the bot's actor — the capture rubric (context/worked/failed/would_change/reusable), session id + uid, model, turn/tool counts, all in `data`; a `skipped` variant at zero model cost, distinct from an `ok` with empty fields (#1503 moved it off `transcript-digest-<date>.jsonl`, the last production JSONL data record) | `SESSION_DIGEST_ENABLED=1` per fleet (dormant by default); `PLANE_EMIT_DISABLED=1` |
| `claudlobby generate` (`registry_emit.py`) | registry keyframes for every composed entity; the `scan_completed` declaration that validates its tombstones | dormant until `PLANE_EMIT_ENABLED` in the fleet `.env` tier (the tier cascade, not `fleet.yaml env:`) — the flag's only meaning since the closure |
| `lib/workstream-update.sh`, `lib/briefing-trigger.sh` | workstream construct + verb events; briefing communications | `PLANE_EMIT_DISABLED=1` only — always on since F18 R1 |
| `claudlobby plane expire` (host timer) | a terminal `expired` on assignments overdue past the horizon — a Lane-B fact through normal ingest | `PLANE_EXPIRE_ENABLED` |
| `claudlobby task withdraw` / `task escalate` | Fleet-owned acts on a canonical work item: a withdrawal records a terminal `cancelled` task event, and an escalation records a non-terminal question that remains visible while the work is open. Both accept queued work and use a durable request UUID; historical dispatch display and assignment IDs are not aliases. The granted bot forms use `claudlobby --json task ...`. | No action is claimed if the Plane write fails; a replay with the same request UUID returns the prior result. |
| `claudlobby task nudge` | non-terminal `nudged` on canonical open work, including queued intake, plus a linked request from the actual caller to the selected fleet's implicit manager; both commit before shared native notification. `--by` is provenance. Request replay never resends | Recording or request persistence unavailable: refuses before notification; a later transport failure leaves the committed fact intact |
| `claudlobby task recheck` (fleet timer invokes the selected CLI) | One digest to the current implicit fleet manager for up to eight overdue or ageing canonical open tasks, including queued work. The actual caller records one ask per named task before transport; only the primary digest message has a transmission and receiver proof. A request UUID freezes the selection, and a no-op sweep records a system fact so replay cannot acquire new work. | `TASK_RECHECK_ENABLED=0` skips the timer tick loudly; recording failure prevents the send. |

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
  rungs (schema, provisional actors, tombstone validity, reconciliation, the
  WAL against its ceiling).
  These diagnostics open the database read-only and require the selected schema;
  they do not migrate it. `plane prune` and `spool retry` are explicit
  maintenance mutations; a newer db refuses them (`DowngradeError`, rc 4).
  Both support `--json` with the common command result. A spool retry reports
  committed, duplicate, quarantined and still-pending entries separately;
  quarantine or pending entries do not return a clean success.
- **The stdlib readers** (`lib/plane-readers.py`, `lib/plane-lookup.py`) — the
  plane answered from bash doors without paying the package import: the open
  list and the overdue set (SQL pinned byte-identical to
  `queries.OPEN_ASSIGNMENTS_AT_SQL`), the resolver, the legacy-id join, the
  divergence check, the retirement. One open shared by all: the `mode=ro`
  URI first, and on CANTOPEN a plain connection held read-only by `PRAGMA
  query_only` — under the system `python3` the doors run, a read-only URI
  cannot open a WAL database whose writer has closed (it cannot create the
  shared-memory file), which is what a daemon restart looks like. Review
  attribution now lives in `claudlobby task reviews` (`claudlobby/review_queries.py`).
- **A read pins the WAL while its statement is open (#1905).** The daemon's
  checkpoint cannot reset the WAL past a reader's snapshot, and a loop over a
  live cursor keeps its statement open for the whole loop, so readers fetch
  their rows first and do the per-row work after
  (`tests/test_plane_reader_snapshots.py` fails a loop that queries, yields or
  writes per row). A reader that never lets go, such as an interactive
  `sqlite3` session or a hung process, still grows the WAL, and the daemon
  cannot end another process's transaction. So it is reported: the host probe
  records `host.plane_wal_bytes` every minute (the Host card shows it), and
  `plane doctor`'s `wal` rung turns ATTENTION past the 4 MiB ceiling accepted
  on #1693, naming the holding process from `/proc/locks` on Linux.

## The task loop (#1481) — in the operator's words

A dispatch used to be a one-way street: you sent it, and either a report came
back or the row sat there. The loop closes it, and every step is a plane fact
already described above — nothing new is stored, and there is no second
bookkeeping surface to reconcile.

1. **Every id'd dispatch gets a deadline** (24h by default, per fleet), so the
   watchdog and the re-check have a clock.
2. **The manager can end a row without a report.** `claudlobby --json task
   withdraw TASK_ID --reason "…" --request-id UUID` closes canonical work
   (`cancelled`, terminal for every reader); a
   re-dispatch with `--supersedes` retires it and opens the replacement.
3. **The manager can ask you a question about a task** — `claudlobby --json task
   escalate TASK_ID --question "…" --request-id UUID` — and the work STAYS OPEN while you decide, and is EXEMPT from the
   re-check timer below (item 5) once assigned: it is the human's to answer, not the
   manager's to be nagged about (the M-B fold's F5). Queued work can be escalated too. Each recorded raise is paged
   to the fleet's Telegram chat exactly ONCE, by fleet-pulse, as
   `NEEDS YOU (<fleet>): task <id> escalated by <manager>: <question>`, keyed
   by event id in a PER-FLEET seen-file (`state/pulse/<fleet>.escalated`
   — the fold's F1: `state/pulse/` is host-global, one root composing several
   fleets, so a single shared marker directory let one fleet's forget-loop
   erase another's markers and re-page its whole backlog). The page is keyed
   by the raise, not by a clock: it goes quiet when a later task act clears the raise
   (progress, a report, a withdrawal, a supersede) and speaks again if the
   manager raises the row afresh. A nudge does not clear it.
4. **You can poke open work** — `claudlobby --json task nudge TASK_ID
   --reason "why" --request-id UUID` records the nudge and asks the selected
   fleet's manager to revisit it. From Telegram, ask the manager to run it
   with your provenance in `--by`; the bot remains the actual caller.
5. **The clock pokes for you.** Where a fleet arms `task-recheck`, every 6h
   each manager gets ONE message listing their rows past deadline or older
   than 48h — id, title (clipped to ~80 chars, the fold's F6: the id already
   carries the row's full identity), assignee, age, deadline, last progress,
   and whether anyone nudged it — with the four verbs and their exact
   commands, and is asked to report what it did per row. An escalated row is
   never named (item 3); a "waiting on the human: N row(s)" footer names the
   count where a digest is already going out for other reasons. A row already
   named inside the repeat window (24h) is skipped, and that skip is a PLANE
   READ: the ask itself is recorded per row, stamped
   `source_ref = task-recheck:<task_id>:<current_assignment_id|queued>:<request_id>`,
   so there is no timer state file to lose. Each named row has a committed
   communication fact, but only the primary digest has a physical transmission;
   the other rows have no individual delivery proof. A known failed send is
   eligible on the next tick. A submitted digest holds rows for the repeat
   window; an uncertain attempt or missing readable receipt holds them for
   inspection rather than implying delivery. Replaying the same request UUID
   never sends again.
6. **The same list by hand.** `claudlobby brief --bot <manager>` renders, under
   its own `dispatched` heading, the rows the manager assigned that are still
   open, with those facts, and prints the same four verbs once under that
   heading — the fold's F2: this used to be described here as the bot's own
   `open`/`overdue` rows (the ASSIGNEE's axis), which is a different question
   and read empty for a manager holding no work of its own. A bot's own
   open/overdue rows, if it also carries work as a worker, are the separate
   `open`/`overdue` headings in the same brief.

The re-check is deliberately a COMMUNICATION and never a task: an id'd
re-check would open a row nobody closes, which is the defect the loop exists
to remove.

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

**Switches — what is on, what is opt-in, how to turn a door off.** Since the
defaults flip (chunk N) **every plane door ships ON**, under the estate rule: a
job or door is on by default unless it deletes data, spends money, mutates
operator source, or sends outbound to people at scale. Nothing a plane door
does is any of those — it records, it reads, and its one DELETE is family-scoped
metric-sample retention.

<!-- BEGIN GENERATED: switches -->
<!-- Generated from claudlobby/switches.py — do not hand-edit. Regenerate: claudlobby host doctor --switches --markdown -->

| Switch | Ships | Scope | Carrier | Flip it with |
|---|---|---|---|---|
| `manager-checkin` | **off** — model spend — one manager turn per idle beat — and it injects into a live session | fleet job | fleet.yaml | defaults.jobs.manager-checkin.enroll: true in fleet.yaml, then config plan, config diff PLAN_ID, and host activate PLAN_ID |
| `plane-prune-system-events` | **off** — deletes data — and unlike the sample lane beside it, this one could delete a RECORD rather than a sample, which is why it is an allowlist: a wrongly-pruned type breaks selfstart-snapshot.sh's boot gate, which fails closed on an unreachable receipt read but reads an ABSENT receipt as a certain no-receipt | door | host/root .env | PLANE_PRUNE_SYSTEM_EVENTS_ENABLED=1 in the host or root .env |
| `plane-daemon` | **on** | host service | system.yaml enroll | host.jobs.plane-daemon.enroll: false in this host's override, ~/.config/claudlobby/system.yaml, then config plan + config diff + host activate (removes the installed unit) |
| `plane-expire` | **on** | host job | host/root .env | PLANE_EXPIRE_ENABLED=0 in the host or root .env |
| `plane-host-probe` | **on** | host job | system.yaml enroll | host.jobs.plane-host-probe.enroll: false in this host's override, ~/.config/claudlobby/system.yaml, then config plan + config diff + host activate (removes the installed unit) |
| `plane-prune` | **on** | host job | host/root .env | PLANE_PRUNE_ENABLED=0 in the host or root .env |
| `plane-recording` | **on** | door | fleet .env | PLANE_EMIT_DISABLED=1 in the fleet-tier .env — the ruled harness exemption; silences EVERY door at once |
| `plane-view` | **on** | host service | system.yaml enroll | host.jobs.plane-view.enroll: false in this host's override, ~/.config/claudlobby/system.yaml, then config plan + config diff + host activate (removes the installed unit) |
| `registry-scan` | **on** | composition | fleet .env | PLANE_EMIT_ENABLED=0 in the fleet-tier .env |
| `task-recheck` | **on** | fleet job | fleet .env | TASK_RECHECK_ENABLED=0 in the fleet-tier .env |

<!-- END GENERATED: switches -->

Every plane door itself still ships on; the one opt-in row above is a
spending job that merely reports through the plane, not a plane door
declining to record. `claudlobby plane doctor` prints this table
with each row's live state and the tier that set it — for the fleet it was
given; without a `--fleet` the fleet-scoped rows read `unknown` and say so
rather than reporting a scope nobody read. `claudlobby host doctor --switches`
prints the whole estate's. `plane-view` needs the `[plane-ui]` extra: where it
does not import, no unit is composed at all and the table's arm line is the
`pip install` — a supervised unit that cannot start is a crash loop, not an
honest failure.

**Carriers.** Each row's `Carrier` column names the one that reaches THAT
door, because they do not reach the same places. A flag in the fleet (or
host/root) `.env` tier is read by `generate`, stamped onto timer units as
`Environment=` lines, and — for the estate silencer — bridged into `bot.conf`
by the composer, which is what makes silencing a fleet silence its own timers
rather than only its sessions. A tier assignment on its own never reaches a
session: `start-bot.sh` sources the tiers before `set -a`. `fleet.yaml`'s
`env:` (per bot) reaches `bot.conf` and therefore the session — never
`generate`, never a timer — which is the only carrier a door running inside a
bot's Claude session can be reached by. Since these are now **opt-outs** the
composer stamps the tier's **resolved value**, `0` included: a host timer
sources no `.env`, so an off switch that never reached the unit would not be
an off switch at all. Only an exact `0` disarms (an empty assignment wins at
its tier, #1213, but is not a `0`), and a disarmed door no-ops **loudly** —
a silent skip is indistinguishable from a broken timer. `env_tiers.resolves_to`
is the one definition of what a flag value means; the registry of switches is
`claudlobby/switches.py`, and every surface above derives from it.

**Migrations** — `claudlobby/plane/migrations/NNNN_*.sql`, `user_version`-gated
(`migrations.py`); `migration apply` owns initialization and schema changes.
The daemon, normal writers and Plane diagnostics require a prepared schema;
they do not migrate as a side effect. The early migration history is:
0001 kernel · 0002 task-status index · 0003/0004
the fleet room · 0005 FTS · 0006 the registry lane · 0007 `assignments(source_ref)`
(the legacy join) · 0008 `events(actor_uid, occurred_at)` (progress grace, the
resolver's guard) · 0009 `events(fleet_uid, occurred_at) WHERE kind='system'` (Phase B: the fleet-events readers and the escalation window) · 0010 the task vocabulary widened for `escalated` and `nudged` (chunk M-A). A newer db refuses older code (rc 4), never downgrades — and a refusing *daemon* exits so its supervisor relaunches it on the current install (#1485, the write-spine section above). **0010 is the estate's first table REBUILD** — SQLite cannot ALTER a CHECK, so widening the task-event list means the documented 12-step copy of `events`, paid by explicit migration before activating the upgraded writers (it needs the table's size again in free space while it runs — and on a WAL database that means the WAL's copy TOO: an 80 MB plane whose `events` is 67 MB needs ~67 MB of WAL on top of the new table's ~67 MB, so a host at 90% full passes the naive check and fails the real one). It also holds the write lock for SECONDS rather than the milliseconds every earlier migration took, which is long enough for a second migrator's `BEGIN IMMEDIATE` to exceed `busy_timeout` and raise on a benign race — `migrate()` therefore re-reads `user_version` after WAITING for the write lock, so the loser no-ops on the winner's result. The O(1) alternative, a `PRAGMA writable_schema` edit of `sqlite_master`, corrupts the schema outright when the SQL is wrong, which is a worse failure than a slow start on the one database the estate keeps its history in.

**Retention** — `plane prune` ages `metric_samples` past 30 days by
`ingested_at` (the incident-join window). The separately armed system-event
lane deletes only its two allowlisted event types in the same transaction;
neither lane touches the ingest ledger. There is no VACUUM against a live
daemon. `plane expire` is the attention queue's aging
sweep (7-day horizon), idempotent by construction.

**The rule every reader follows** — unreachable is not empty. A missing or
unopenable db, a fleet the plane has never seen, or a plane that holds no bot
of the fleet is REFUSED with a reason on stderr and a nonzero rc; an existing
source with zero rows is an answer. The refusal never rides stdout, because
`report-back.sh` and `fleet-pulse.sh` parse it.
