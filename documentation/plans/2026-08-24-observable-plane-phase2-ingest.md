# Observable plane — Phase 2: the ingest daemon and the doors

**Status:** PLAN (2026-08-24). Execution gated on: PR #1341 merged; this plan reviewed per the
operator's normal flow. Parent spec: `2026-08-18-observable-plane-design-v2.md` (authoritative on
every model/semantic question; this plan is transport, rollout, and door work only). Door facts:
`2026-08-25-phase0-door-inventory.md` (measured @ `e9311da`).

## 1. Decisions carried in (all ruled; provenance noted)

| Decision | Ruling | Where ruled |
|---|---|---|
| Ingest transport | **Unix-socket daemon** — Pi cold-emit p95 534–574ms and an independent fast-host 342.8ms both exceed the pre-registered 300ms ceiling; converges with F22's authenticated-emitter path | §5/§14 fork, closed on PR #1341 (binding Pi runs, both tips) |
| Hardware's role | Benchmarks inform **budgets and process choices, never model shape**; a bench miss opens a defect investigation | Spec §19.8 (operator ruling 2026-08-24) |
| Daemon scope tripwire | The daemon owns **ingest and nothing else** — it never schedules, never supervises, never acts on the world. The Phase-4 UI is a separate consumer process. If something wants to "live in the daemon," that want is the tripwire firing | Operator conversation 2026-08-24; twin of §8's rebuilding-Linear tripwires |
| Placement | Source `claudlobby/plane/daemon.py` (package code — the write authority is part of the kernel; the §19.6/`plane/__init__` "daemon is a consumer" line meant the UI daemon and gets a one-line disambiguation) · launcher = thin `lib/` wrapper (lib-common, `.env` tier cascade, exec the CLI) · units **composed from `system.yaml` at the HOST tier** (db is one-per-root; fleets are rows, not databases) — the first host *service* where today only host timers exist | Operator conversation 2026-08-24 |
| Dormancy | **Dormant by default.** A root pull must never start a new resident process on a host; armed per host via `system.yaml` (`host.jobs.plane-daemon.enroll: true` shape). Doors fall back cleanly when unarmed | No-silent-switches rule |
| Ack semantics | **Synchronous, honest**: the daemon replies only after validate + commit (or spool). ~100ms warm on the Pi today. Async accept is REJECTED for v1: an ack that precedes validation is a receipt that isn't — the exact F9/F6 hazard class the whole program exists to close. Flip condition: measured door-side pain in real operation AND a designed spool-grade durability story for pre-validation acceptance | Operator conversation 2026-08-24 |
| Reader posture | Phase 2 is **dual-write, flight-recorder passive**. Every legacy reader (dispatch-overdue, brief, who-reviewed) stays on JSONL; plane readers are additive. Reader cutover is its own later phase with its own canary | Spec §16 |

## 2. The daemon

**Protocol — one contract, three transports.** The wire shape is the existing `EmitRequest`
(already versioned via `schema_version`), newline-delimited JSON over `SOCK_STREAM`:
request = `{"events": [...]}` (the emit-batch shape; a single event is a 1-list), response = one
JSON line `{"ok": bool, "results": [{event_id, status, detail?}...], "error"?}` mirroring the CLI's
outcome + exit taxonomy (`contract_violation` ↔ 2, `total_failure` ↔ 3, `downgrade` ↔ 4). The
daemon runs the SAME `emit_batch()` path the CLI runs — capture policy, raw-then-transformed
validation, spool-on-retryable — so transport can never change semantics. No second parser, no
second taxonomy.

**Idempotency across transports — the shim always pre-mints `event_id`.** The hazard: door sends
over the socket, daemon commits, ack is lost, door retries via CLI fallback → fresh ids → the same
fact stored twice under different identities. Pre-minting (bash-cheap:
`printf 'ev_%s' "$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n')"`) makes every retry hit the
ledger's unique constraint and classify as duplicate — F6's finalize-before-first-attempt logic
extended to transport retries. The shim owns the mint; tests pin that a socket→CLI retry of the
same payload yields `duplicate`, never a second row.

**Lifecycle.** Bind `state/plane/ingest.sock` (0700 dir already; note the ~104-char macOS
`sun_path` limit — root-scoped placement keeps paths short, and the launcher refuses a too-long
path loudly rather than truncating). Stale socket on start: try-connect; refused → unlink, rebind.
On start and every N minutes: drain the spool (replay is ingest — inside the tripwire; retires the
manual-only `plane spool retry` as the sole drain path). Serve requests serially — SQLite is a
single writer; per-connection batches are the concurrency unit and the kernel's listen backlog is
the queue. Clean shutdown drains in-flight, closes the db. The daemon emits its own lifecycle as
`system` events (`daemon_started`, `spool_drain_completed{ingested,duplicates,quarantined}`,
`daemon_stopping`) — machinery-logs ruling; `plane doctor` gains a daemon rung (socket present +
connectable + age of last lifecycle event).

**Supervision.** v1 on BOTH platforms: self-bound socket + standard always-restart unit
(systemd `Restart=always` / launchd `KeepAlive`), enrolled by `setup-system`, audited by
reconcile. Socket ACTIVATION (init owns the socket, spawns on first connect, kernel queues during
restart) is a systemd-only enhancement fork, deferred: launchd requires `launch_activate_socket`
(C API, ctypes from Python) and a platform-asymmetric v1 buys complexity before the simple shape
has run anywhere. Honest correction of the earlier conversation claim: activation is native on
systemd, fiddly on launchd — v1 takes neither.

**Trust posture (F22, unchanged).** Same-user local trust: 0700/0600 enforce the boundary;
`SO_PEERCRED`/`LOCAL_PEERCRED` uid==euid check on connect is cheap and ships v1 (refuse other
uids loudly). Authenticated multi-emitter and structurally-append-only db file ownership remain
the documented upgrade path, not v1.

## 3. The shim and the doors

**One shim, `lib/plane-emit.sh`**: sourced/invoked by doors; owns pre-mint, payload assembly, and
the fallback ladder — **socket → cold CLI → (CLI's own spool) → total-failure exit 3** — each rung
disclosed on stderr, never silent. Unarmed daemon = the ladder starts at rung 2 and Phase 2 doors
still work on any host. The shim NEVER blocks a door's real action: plane failure degrades to
"acted but unrecorded, said so loudly" (the door's legacy JSONL write still happened — dual-write
means the record is not yet load-bearing).

**Doors in scope (semantic communications per the inventory):** `dispatch-task.sh`,
`report-back.sh`, `tg-post.sh`, `workstream-update.sh`, plus `briefing-trigger.sh` (the inventory's
judgment row: mint it a communication). Each emits the intent/construct rows + transmission events
per §7–8; the inventory's per-door verb-mapping table (workstream flags → workstream event tokens;
report-back's open-task join; tg-post exit-3 → transmission `failed`) is written INTO each door
task below, not re-derived. **Control-plane raw sends and harnesses stay out** (exemption
mechanism: the shim no-ops under `PLANE_EMIT_DISABLED=1`, which harnesses export — mechanism now
ruled: env flag, not socket namespace, because harnesses already own their env and a namespace
would need daemon cooperation).

**Arming (ruled at PR-B kickoff, 2026-08-26): door emission is DORMANT by default behind
`PLANE_EMIT_ENABLED=1` in the fleet's `env:`** — the `SESSION_DIGEST_ENABLED` /
`SPINDOWN_RECEIPT_ENABLED` precedent exactly: lib/ is a shared install that cannot be staged
per-fleet, so a root pull must never activate new door behavior estate-wide, and an UNARMED
fleet's doors must pay zero latency (not a ~400ms cold-CLI toll per dispatch on hosts without
the daemon). This knob is what makes §4's one-fleet canary ladder real. `PLANE_EMIT_DISABLED=1`
remains the harness override on top (wins over ENABLED).

**The one ordering change, named as the canary's sharp edge:** report-back today SENDS then
ledgers (`:152`/`:184`); the shim records intent BEFORE transport (F9). That flips its crash
exposure — a crash between record and send leaves an intent with no transmission (visible,
reconciliation's exact job) where today it leaves a sent report with no ledger row (invisible).
The flip is the point, but it is a *behavior change under crash* and the canary watches for it
explicitly.

**Deferred to a bridge sub-phase (2c):** the Telegram bridge's per-chunk transmissions need an
adapter at the plugin's send loop (inventory disproof-target 3) and the inbound side is still
unmeasured — measure first, adapt second. Nothing in Phase 2 touches the plugin.

## 4. Dual-write canary and rollout

1. **Parity instrument first** (`lib/plane-parity.py`, stdlib-only, `dispatch-overdue.py`
   precedent): joins a window of legacy JSONL rows ↔ plane rows (by task_id / pre-minted event_id
   / msg refs), reports missing-in-plane, missing-in-legacy, field mismatches; UNREACHABLE-vs-empty
   per `source_state.py`'s rule. It is the canary's verdict tool and a permanent reconciliation
   door afterward.
2. **Rollout ladder** (canary-rollout protocol): arm daemon on ONE host → shim into
   `dispatch-task.sh` on ONE fleet → N days parity-clean → `report-back.sh` (watch the ordering
   edge) → `tg-post.sh` + `workstream-update.sh` + `briefing-trigger.sh` → remaining fleets.
   Rollback at every rung = disarm the shim (doors revert to legacy-only writes; no data loss —
   legacy never stopped writing).
3. **Empirical gate (mandatory, runtime change):** extend `validate-bot-change.sh` with the
   plane leg — throwaway bot dispatches through the real shim against a real daemon on a temp
   root; asserts rows land, ladder degrades correctly with the daemon stopped, and
   `PLANE_EMIT_DISABLED=1` produces byte-identical legacy behavior. Cite the run in the PR.

## 5. Named deliverables absorbed into this phase

- **SessionStart hook + `process_uid` minting** and their tests (spec §19.6 assignment): hook
  derives `session_uid` via `derive_session_uid`, mints `process_uid`, exports both; empty
  platform id rejected.
- **Capture-validation cost optimization** (PR #1341 disclosure: warm emit 62→106ms from the
  validate-twice fix): re-validate only capture-touched fields on the second pass, semantics
  pinned by the existing finding-3 battery. Budget, not shape.
- **Door-side latency budget:** p95 ≤ 200ms per shim emit on the Pi (warm 106ms + socket + bash
  overhead, headroom for the optimization to reclaim). Measured by a bench extension that drives
  the SHIM, not `emit()` — the number a door actually feels. §19.8 applies: a miss is a defect
  investigation. **Known lever named in advance (2026-08-27, from the Mac smoke):** the
  shim's rung-1 client is a python3 spawn (~150-250ms interpreter startup on the Pi before any
  socket work) — if the Pi budget fails, the investigation starts there (a `nc -U`/bash-native
  send, or folding the pre-mint+send into one spawn), never at the model.
- **Ambiguous-success branch enumeration** from `pane_send_verified` (inventory Remaining):
  enumerate, map each to transmission `unknown` vs `pane_submitted`; the mapping ships as a table
  in the dispatch-door task with a test per branch.
- **`plane doctor` growth:** daemon rung, parity-age rung, corrective commands (§17 direction).

## 6. Task decomposition (contracts + test obligations; full code at execution, Phase-1 style)

| # | Task | Contract | Pinned by |
|---|---|---|---|
| T0 | `plane/daemon.py` + protocol | §2 wire contract; serial serve; spool drain; lifecycle events; peer-uid check; stale-socket recovery | socket integration tests on a temp root: commit, duplicate, violation-exit-2 mirror, daemon-restart mid-client, foreign-uid refusal (skip if unprivileged) |
| T1 | Launcher + composed host-service units | `lib/` conventions; `system.yaml` host-service shape (systemd service, launchd KeepAlive); dormant default; sun_path guard | unit-render tests (composer); launcher refusals; enroll/disenroll via `setup-system` on a throwaway root |
| T2 | `lib/plane-emit.sh` shim | pre-mint; ladder with disclosure; `PLANE_EMIT_DISABLED`; never blocks the door's action | bash suite: each ladder rung forced; retry-across-transport = duplicate; disabled = byte-identical legacy behavior |
| T3 | `lib/plane-parity.py` | §4.1; unreachable ≠ empty | fixture suites both directions + a mismatch battery |
| T4 | Door: `dispatch-task.sh` | work_item + assignment + communication + transmission rows per §8; ambiguous-branch table | extension of `tests/test_dispatch_type.py` patterns + parity fixture |
| T5 | Door: `report-back.sh` | intent-first ordering; open-task join preserved | crash-window test (kill between record and send → reconciliation surfaces it) |
| T6 | Doors: `tg-post.sh`, `workstream-update.sh`, `briefing-trigger.sh` | exit-3→`failed`; verb-mapping table; briefing as communication | per-door suites |
| T7 | SessionStart hook + `process_uid` | §19.6 | hook tests incl. empty-id rejection |
| T8 | Capture-validation optimization | semantics unchanged | finding-3 battery green before/after + bench delta recorded |
| T9 | `validate-bot-change.sh` plane leg + doctor rungs | §4.3, §5 | harness run cited in PR |
| T10 | Shim-level Pi bench + budget verdict | §5 budget | machine-checked exit, both hosts recorded |

## 6b. Domain-fit intake (fleet review of #1341, 2026-08-25 — Task B, measured on real ledgers)

Seven findings from the fleet's read-only classification of live traffic (2,344 dispatch rows,
324 reports, 55k events; review body on PR #1341). All non-blocking for Phase 1; each is now an
OBLIGATION of this phase, dispositioned here so PR-B executes against the document, not memory.

1. **Acknowledgement has no producer — the headline.** `recipient_acknowledged` has three
   consumers (`TASK_STATUS_SQL` `open` rung, `ATTENTION_SQL`, `RECONCILIATION_SQL`) and zero
   producers, and the runtime holds no such fact: tmux has no ack channel, `pane_send_verified`
   proves SUBMISSION. Inferring the ack from report text measurably fails the `open` rung — 69
   of 137 id'd dispatches have no progress row (first report is terminal), so the inference
   arrives at/after the terminal event; where explicit `Acked:` rows exist, 0 of 46 met the
   protocol's 5s (median 29s). **Disposition — activation derives from carrier-appropriate
   evidence:** for `tmux`, `pane_submitted` occupies the activation rung (submission is the
   strongest fact the carrier can ever yield); a true `recipient_acknowledged` row — produced
   ONLY when a door observes an explicit worker ack, recorded as inference-free fact — TIGHTENS
   the derivation where present. The recorder measures the ack protocol; it must never assume
   it (the flight-recorder thesis applied to itself). The worker-lifecycle 5s mandate and the
   unexecuted manager escalation rung are an OPERATOR decision, deliberately out of this plan's
   scope — this design works unchanged under either ruling. T-numbering: this revises the
   `TASK_STATUS_SQL`/`ATTENTION_SQL` ladders in this phase, with fixtures per carrier.
2. **Missing-producer queries must fail toward EMPTY.** `ATTENTION_SQL` currently inverts to
   all-alarm (every non-terminal assignment, `expected_by` dead) and `RECONCILIATION_SQL` pins
   at 100% when the ack fact is absent — the all-alarm inversion this estate keeps re-finding.
   Disposition: the rewrite in (1) removes the structural inversion; additionally each
   derivation gains a fixture asserting its zero-producer behavior reads empty/quiet, never
   everything.
3. **Coalescence — many messages, one pane submission (the weld).** Measured 2026-08-24: three
   payloads (boot resume + STARTUP_PROMPT + dispatch) submitted as ONE run-on message; each
   msg_id would read as a clean independent `pane_submitted`. Disposition: the tmux door
   records coalescence when `pane_holds_unsubmitted` was true at send — reusing the
   `part_no`/`part_count` seam inversely (n msg_ids sharing one submission group) per the
   reviewer's suggestion; exact shape decided in the door task, pre-cutover so cheap.
4. **`EmitRequest.fleet` is a scalar; 44.6% of dispatch traffic is cross-fleet.** Ruling
   needed before any door emits. **Disposition — `fleet` is the SENDER'S fleet** (the emitting
   door runs in the sender's context and knows it authoritatively; the recipient's fleet is
   derivable from the recipient alias `bot:<fleet>/<name>`), stated in §9b/§7 at execution.
   Legacy import must scope the host-global dispatch log per fleet BEFORE joining per-fleet
   report ledgers (the #526 shape).
5. **`command_type` provenance.** The ledger records payload text, never the `--type` flag, and
   the two provably disagree (1 of 227 measured). Disposition: `command_type` is DOOR-FLAG
   provenance only — populated from the flag at emit time, never parsed from text; for
   `origin="legacy"` import it is NULL (unrecoverable), stated in §9b.
6. **Vocabulary intake:** `supplied-id-not-open` (4 real rows — a join fact our tooling already
   records deliberately) joins TASK_EVENTS as a first-class token at the door task; legacy
   `blocked`→`returned_blocked` confirmed 6/6; `failed`/`blocked_waiting`/`resumed` noted as
   empty-by-protocol on this estate (metrics keyed on them read clean by construction — doctor
   discloses, not deletes); the retraction shape (a STOP sent as a fresh task) is recorded as a
   known gap riding `supersedes_msg_id`, whose emptiness in practice (#1032) is already tracked.
7. **`pane_submitted` means TUI-ACCEPTED, not turn-consumed.** A send queued behind a busy
   turn verifies clean and waits invisibly (93/129-min stuck turns measured same day).
   Disposition: the door emits `carrier_queued` (new ATTEMPT_STATES token) when the busy/queued
   branch is detectable at send time, and `pane_submitted`'s §7 definition gains its honest
   bound; the overdue overlay stays evidence-based rather than pretending to know arrival.

## 7. Explicitly out of scope

Reader cutover off JSONL · registry lane / migration of `registry_snapshots`+`metric_samples`
constructs (Phase 2b, owns migration 0003 — renumbered from the spec's pre-0002 "migration 0002"
note in §19.3) · the UI daemon (Phase 4) · bridge adapter + inbound measurement (2c) · async
socket ack (rejected v1; flip condition in §1) · socket activation (enhancement fork, systemd
first) · any schema change not independently domain-justified (§19.8).

## 8. Exit criteria

Daemon supervised + armed on the canary host · five doors dual-writing on the canary fleet,
parity-clean over the agreed window · legacy readers untouched and green · shim bench budget met
on the Pi · validate-bot-change plane leg cited · zero un-disclosed fallback rungs observed in
canary logs.
