# Plane cutover (F18) — design walk

**Status:** walk for the operator's ruling (2026-09-02). Tracking: #1444. Nothing here is built.

## Why this is the largest remaining piece

Every new surface — registry, presence, inventory, org, utilization, attention — reads the plane's SQLite db, and it has been recording production data since 2026-08-28. But the fleet's **system of record is still the legacy JSONL**: the doors dual-write (spec F18, "disposable-until-cutover"), and the readers that *act* on fleet state — the brief a manager boots with, the pulse watchdog that pages, the resolver that closes a dispatch when a worker reports — still read files. Until they move, the plane is a mirror, not the record; and the mission ("redo the data model underneath the framework and then use that to power the observability plane") is only half done.

## What the design already ruled (not re-opened)

- **F18 — clean epoch + selective import.** A new trustworthy epoch at cutover; task-id-bearing dispatch-log + report-back rows imported with `origin='legacy'`, deterministic import ids, `import_batch`, confidence markers; ambiguous history stays read-only legacy; **task closure is never inferred**. The envelope has carried `origin`/`import_batch`/`confidence` since v2.1, so the importer needs no schema change.
- **Ledger rows are never deleted**; retention is family-scoped (§10). §11's retention/privacy windows for the non-sample families become real at cutover.
- **Status is always derived** (contract × events × clock × policy); `workstreams.json` is already a Lane-C projection the shim regenerates.
- **Dual-write canary verdict instrument:** `lib/plane-parity.py` (legacy ↔ plane join via `source_ref = "<ledger>:<id>"`).

## The reader inventory (measured 2026-09-02)

| Reader | Reads | Class | Plane door today |
|---|---|---|---|
| `lib/dispatch-overdue.py` (`--open`, `--open-task`, `--orphans`, `--all`) | dispatch-log.jsonl, report-back.jsonl, `data/.spawn` | task facts | none for "open per bot" / orphans; `TASK_STATUS_SQL` covers status |
| `lib/report-back.sh` (the #835 resolver) | via `dispatch-overdue --open-task` | task facts | — (follows the above) |
| `claudlobby/brief.py` | dispatch-log, report-back, fleet-state | task facts + one **write** (the per-viewer `--ack` cursor) | `ATTENTION_SQL`, `TASK_STATUS_SQL`; no unacked-per-viewer door |
| `lib/fleet-pulse.sh` | report-back, events/fleet-*.jsonl | task facts + alerting | `ATTENTION_SQL` (overdue), no orphan door |
| `lib/who-reviewed.py` | report-back | task facts | none (a join on `pr_url` + timestamp tolerance) |
| `claudlobby/source_state.py` | report-back (probe) | rule, not a reader | moves with brief |
| `claudlobby/workstreams.py` | workstreams.json | projection | `WORKSTREAM_STATUS_SQL` |
| `claudlobby/uptime.py`, `status.py`, `lib/install-cron.sh` | keepalive.log | presence | `LATEST_HEARTBEAT_SQL`, `bot.session_up` samples |
| `claudlobby/utilization.py`, `lib/fleet-utilization.sh` | fleet-state + keepalive | presence | `claudlobby/plane/utilization.py` (shipped, one definition) |
| `composer.py`, `status.py`, `utilization.py` | **fleet-state.json** | supervision state | **not plane data — stays** |
| `lib/dispatch-task.sh`, `lib/bot-vitals.sh` | write dispatch-log / events | **doors** | dual-write until the last reader moves |
| `lib/data-sweep.sh` | events/*.jsonl names | infra | rename at retirement |

Two things the table settles: supervision state (`fleet-state.json`) is out of scope — the plane never modelled it; and the **task-fact readers are the cutover**, with `dispatch-overdue.py` the sharpest because `report-back.sh` closes dispatches through it.

## Junction 1 — Which reader moves first

| Option | For | Against |
|---|---|---|
| A. `dispatch-overdue.py` (open / open-task / orphans) | The root: brief, pulse and report-back all consume it, so moving it moves the most; its three failure classes (#1187, #1232, #1014) are exactly the unreachable-vs-empty hazards the plane's `source_state` posture was built for | The #835 resolver depends on `--open-task`; a wrong answer stamps a false closure — the highest-stakes reader |
| B. `brief.py` | Read-only but for one cursor; failure is a stale brief, not a wrong closure | Consumes the #835 doors, so it cannot fully move before A |
| C. presence readers (`uptime`, `status`, `utilization`) | Data already flows; `plane/utilization.py` exists | Not on the record-of-truth path; moving them first changes nothing about authority |

**Recommendation: A, behind a parity gate, in list-mode first.** Move `--open`/`--all`/`--orphans` (reads) before `--open-task` (the resolver's input), and keep `--open-task` on the JSONL until a week of parity shows the plane's open set equals the ledger's on every bot. The plane needs one new Lane-C door for it: **open assignments per assignee** (non-terminal assignment, ordered by dispatch) and **orphans** (assignment whose assignee's *current* `proc_uid` differs from the one at dispatch — the session-start hook has recorded `proc_uid` since PR-B, so the `.spawn` file join becomes a plane join).

## Junction 2 — How the legacy write ends

| Option | For | Against |
|---|---|---|
| A. Door by door, once its last reader moves | Smallest blast radius; each door's legacy write is a few lines behind a flag | Long dual-write tail; two records diverge silently if a door is missed |
| B. A single fleet-level flag (`PLANE_AUTHORITATIVE=1`) that stops every legacy write at once | One switch, one epoch, one parity check | All-or-nothing: one unmoved reader goes blind at the flip |

**Recommendation: A for the writes, but B for the *declaration*** — the epoch is a per-fleet declared instant (a `declaration` event, like `scan_completed`), and a door may stop its legacy write only after the epoch is declared *and* its readers have moved. The flag names the epoch; the doors retire on evidence.

## Junction 3 — The epoch and the import

- **Epoch:** one `cutover_declared` declaration per fleet carrying the instant and the import batch id. Everything the plane recorded live before it is already trustworthy (it went through the contracts); the import fills the *history* the readers need beyond what the plane has recorded (the JSONL holds months).
- **Import:** task-id-bearing rows only (dispatch-log rows with an id; report-back rows that name one). Deterministic ids (`ev_` + sha256 of ledger name + line identity) so a re-run is a duplicate, never a second row — the same idempotency the attention sweep uses. `origin='legacy'`, `import_batch=<id>`, `confidence` per row (a row whose task id matches a live assignment: high; a bare summary: low). **No task closure is inferred**: an imported `completed` row is a fact; an absent one is not a failure.
- **Not imported:** keepalive/event JSONL (presence is a live derivation and the retention window is 30 d anyway), anything id-less.

## Junction 4 — Verification, per reader

1. `lib/plane-parity.py` over a real day: rc 0, malformed counts disclosed.
2. A **shadow mode** for the reader: compute both answers, log a diff, serve the legacy one — a week clean on the Mini before the flip (the #835 resolver gets this longest).
3. The standing gauntlet loop per chunk (build → lens rounds → two-leg gate → CI → admin-merge → deploy → live verification).
4. The Pi joins only after federation (its plane is a second source).

## What this walk asks the operator to rule

1. Junction 1's order (A first, list-mode before the resolver).
2. Junction 2's split (doors retire on evidence; the epoch is a declaration).
3. Junction 3's import scope (task-id-bearing rows only; nothing inferred).
4. The shadow-mode week as the flip gate.

Deferred, named: supervision state in the plane (a new construct — separate walk); cross-host federation; the `who-reviewed` join (a small door once report rows are in the plane).
