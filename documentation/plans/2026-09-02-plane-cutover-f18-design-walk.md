# Plane cutover (F18) — design walk

**Status:** RULED (2026-09-02 evening). Each junction got its own evaluative session — go/no-go against the intent (*off JSON as soon as possible; the plane becomes the record*) and its target purpose. **All four: GO-WITH-CHANGES.** The changes are folded below as corrections to this walk, each verified in the code before being written here. Tracking: #1444. The build queue is at the end; chunk 1 is in flight.

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


---

## Evaluative-session verdicts (all four GO-WITH-CHANGES) — corrections to the walk above

### J1 — reader order: GO-WITH-CHANGES
- Dependency claims verified in code (`report-back.sh:118` resolves through `--open-task`; `brief.py` imports the matcher as a **module**, so brief's dispatch section *is* the matcher and they migrate together; `fleet-pulse.sh` shells `--all`/`--orphans`/`--unassigned`).
- **Correction — orphans cannot move to a `proc_uid` join.** `plane-session-start.sh` mints a process uid but it is deliberately NOT attached to plane events; there is no process kind and no dispatch-time proc uid. Orphans stay a **hybrid** door (plane data + the host-local `.spawn` join).
- **`--supersedes` never reached the plane** (JSONL-only; 14 of 189 closed rows historically), so the plane's open set over-reports until `supersedes_msg_id` is set and a terminal `superseded` task event is emitted. Wired in chunk 1.
- Unreachable-vs-empty: `db.connect()` auto-creates the file; doctor's exists-before-connect probe must become a shared helper. A dead ingest daemon with a spool reads as "quiet" — the stale gap.

### J2 — how the legacy writes end: GO-WITH-CHANGES
- **Hard precondition (verified `report-back.sh:183-215`):** report-back recovers the plane ids for its *own* emission by grepping `dispatch-log.jsonl`. Retiring the dispatch door's legacy write first would silently unlink every report row. Chunk 1 gives it a plane-side lookup with the grep as fallback.
- Per-door flags (`PLANE_LEGACY_WRITE_<DOOR>=0`), never one fleet-level flag — that reproduces the all-or-nothing failure one level down.
- `tg-post.sh` writes no ledger (nothing to retire). `workstreams.json` is one object the parity reader cannot parse (per-line JSON) — an adapter is owed. `briefing-trigger.sh` stamps no `source_ref` — parity is unmeasurable until it does.
- Doc pass owed: the fleet-pulse and code-audit-sweep skills and `protocols/dispatch.md` tell bots to read the JSONL directly.

### J3 — epoch + import: GO-WITH-CHANGES
- **Correction — "the JSONL holds months" is false.** Both ledgers self-rotate at `OBSERVABILITY_REAP_DAYS` (7, pinned by every fleet; `dispatch-task.sh:633`, `report-back.sh:323`). Dual-write began 2026-08-28, so the pre-dual-write tail is about two days and evaporating. The importer's permanent job is **parity-found gaps**, not archaeology.
- **A stamped `plane_*` id is not proof the row exists**: the dispatch door stamps the ledger *before* it emits (`:632` then `:641`, rc discarded) and a spooled emit exits 0. The true gap is what `plane-parity.py` reports missing over the id-bearing rows too — and parity has **never been run** against a live ledger.
- A dispatch import needs **four** events (work_item, assignment, communication, `pane_submitted` transmission), or `TASK_STATUS_SQL` renders every imported dispatch `created_not_sent` (`queries.py:89,102`). `blocked` maps to `returned_blocked`.
- **Fleet attribution:** the dispatch log is host-global with no fleet column; the only sound signal is a matching row in that fleet's own report-back ledger. A roster guess is unsound (#526, `move-bot`). Unattributable rows stay legacy-only, never guessed.
- Import ids hash **content** (ledger name + raw line + role suffix), never position — rotation rewrites lines.
- The epoch rides as a **`system` event** (open vocabulary; `fleet` is a valid subject kind) — zero contract change; widening `Declaration` is a fine follow-up. No per-row confidence field for v1.

### J4 — verification and the flip gate: GO-WITH-CHANGES
- **Correction — "a week" is a timer, not a proof.** Gate = **per bot, per reader**: 20 consecutive clean shadow comparisons with at least one open-to-closed transition observed; `--open-task` alone: **200 real resolutions, zero divergences of any class**. Recorded history is **replayed** through the comparator on day one to front-load evidence.
- Shadow diffs ride as **system events** (`shadow_parity_clean` / `shadow_parity_diverged`, registry-governed severity) — never a side file. Cheap readers → `notice` + a doctor rung; any `--open-task` mismatch or unclassified case → `critical` on the existing FLEET ALERT path.
- Three legitimate divergence classes, pre-declared: clock skew (reuse parity's `--skew-grace`), plane-more-right, and an **intentional derivation change** declared before shadow starts.
- The `--open-task` comparator is **structurally unwired** from the resolver during shadow — no flag can route the plane's answer into a write path.
- Rollback = the legacy write stays alive, unread, for a burn-in window; no legacy-read fallback is built.

## Build queue (cross-junction, ordered; each chunk under the standing loop)
1. **Task-id lookup door** (`lib/plane-lookup.py`, stdlib, by `source_ref`) + `--supersedes` wired to the plane + report-back plane-first id recovery with the JSONL grep as fallback. *(in flight)*
2. **Parity, run for real** on the Mini + the id-bearing-missing-in-plane gap report; the parity-gap **importer** (`plane import --fleet --dry-run`: four-event dispatch mapping, content-hashed ids, fleet via the report ledger, epoch as a system event).
3. `OPEN_ASSIGNMENTS_FOR_ASSIGNEE_SQL` + a shared exists-before-connect probe + the stdlib reader; the **shadow-diff primitive** (system events + doctor rung + FLEET ALERT for critical).
4. Shadow mode on the list modes → brief → fleet-pulse; the count gate; history replay.
5. Flip the list-mode readers; per-door legacy-write flags; retire `workstreams` (after the parity adapter) and `briefing-trigger` (after `source_ref`).
6. `--open-task` shadow (unwired) → 200 clean → flip; retire the dispatch and report-back JSONL writes; `who-reviewed` plane join; the doc pass.
