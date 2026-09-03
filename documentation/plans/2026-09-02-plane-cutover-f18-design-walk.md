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
1. **Task-id lookup door** (`lib/plane-lookup.py`, stdlib, by `source_ref`) + `--supersedes` wired to the plane + report-back plane-first id recovery with the JSONL grep as fallback. *(built: #1446. The spec lens caught the first version returning before it asked the plane whenever the dispatch log was ABSENT — the exact state the cutover creates; the pin that should have seen it only checked text order, so the doors are now pinned by driving the real function text.)*
2. **Parity, run for real** on the Mini + the id-bearing-missing-in-plane gap report; the parity-gap **importer** (`plane import --fleet --dry-run`: four-event dispatch mapping, content-hashed ids, fleet via the report ledger). *(built: #1447 — `plane parity` + `plane import`; the epoch declaration moved to chunk 5, the flip that consumes it, so a fact is not recorded before anything reads it.)* **First live run (Mini, 2026-09-02):** reports 253/253; dispatches 139/197 = 25 pre-go-live (rotates out ~09-04) + 33 from the unarmed fleet (artemis-data carries no `PLANE_EMIT_ENABLED`) + **0 lost emits** — the J3 emit-loss class is empty on the live estate, so the importer is a small recurring repair tool, and the plane also keeps one table per family (a msg id lives on `communications`, never `events`).
3. The **shadow-diff primitive** — *built: #1448 (`plane shadow`, `OPEN_ASSIGNMENTS_AT_SQL`, the recorded J4 gate over the declared roster, hour-mark replay, doctor rung, the dormant `plane-shadow` timer on its own carrier `PLANE_SHADOW_ENABLED`). Six lenses folded; the adversarial lens caught three blockers (microsecond replay instants, truncated divergences vanishing from the streak, a temp-dir leak) and the spec lens the roster-judged gate. The exists-before-connect probe landed in chunk 2 as `db.connect_ro`; the stdlib reader and the FLEET ALERT bridge stay with chunk 4/5.* **First live run (Mini, read-only, 2026-09-03): 9 bots × 7 instants — 28 clean, 35 diverged, every divergence `legacy_supersedes_pre_cutover`** on five bots: assignments the JSONL retired by `--supersedes` before chunk 1, which the plane still holds open; explained, but the heads differ, so no gate can be met until they close.
   - **3b** — *built: #1449.* `plane import` also emits the terminal `superseded` event for every legacy `supersedes` whose retired assignment the plane still holds open for this fleet's bot (attribution by the plane's own alias, `successor_id` when the plane has the superseding row, idempotent). The instrument found the gap; the importer is where the gap closes.
4. Shadow mode on the list modes → brief → fleet-pulse; the count gate; history replay. *(built: #1450 — the overdue reader mirrored rule for rule, `plane shadow --check` + the stdlib `plane-shadow-check.py` the watchdog runs, the debounced fleet-pulse bridge, brief's shadow section; the count gate and replay landed with chunk 3.)*
5. Flip the list-mode readers; the **epoch declaration** as a `system` event (`cutover_declared`, subject `fleet`) recorded at the flip; per-door legacy-write flags; retire `workstreams` (after the parity adapter) and `briefing-trigger` (after `source_ref`).
6. `--open-task` shadow (unwired) → 200 clean → flip; retire the dispatch and report-back JSONL writes; `who-reviewed` plane join; the doc pass.

### Chunk 3 scope — the shadow-diff primitive (written before the code, per the ruling that every GO is scoped first)

**Purpose.** The instrument that decides the flip: per bot, per reader, a RECORDED comparison of the legacy answer and the plane's answer, so the J4 count gate is derived from the plane itself and never from a timer.

- **`queries.OPEN_ASSIGNMENTS_SQL`** — the plane's open set for one assignee: non-terminal assignments (`NON_TERMINAL_CLAUSE`, the same clause attention and expiry use) joined to the identity registry on `assignee_uid`, the name part compared case-insensitively like the legacy matcher, returning (occurred_at, expected_by, legacy task id from `source_ref`, assignment_id) OLDEST FIRST — the exact tuple shape `open_dispatches` returns, so the two answers are comparable by construction.
- **`plane/shadow.py`** — `plane_open(conn, fleet, bot)`; `legacy_open(bot, paths)` through the install's `dispatch-overdue.py` module (brief's importlib seam, never a re-implementation); `diff(legacy, plane) -> ShadowDiff` with the pre-declared divergence classes: `clean`, `skew` (a row inside parity's skew grace), `legacy_supersedes_pre_cutover` (a row the JSONL retired by `--supersedes` before chunk 1, which the plane still holds — plane-more-wrong but explained, and it drains as those rows rotate), `intentional` (a derivation change declared before shadow starts), and `divergence` (everything else). The HEAD of each list — the `--open-task` answer — is compared as its own field, because that is the one a wrong answer writes into a ledger.
- **Recording** — one `system` event per (bot, comparison): `shadow_parity_clean` (severity `notice`) or `shadow_parity_diverged` (`critical`), subject the bot alias, `data` carrying both counts, both heads, the missing sets and the class of each divergence. Both tokens enter `SYSTEM_EVENT_SEVERITY`; an unknown token would ingest with NULL severity (F19), which is why they are registered rather than free-form.
- **Door** — `claudlobby --fleet F plane shadow [--bot B] [--record] [--gate] [--replay-hours N]`: prints the per-bot diff; `--record` emits; `--gate` exits 0 only when every bot meets the J4 bar (20 consecutive clean comparisons with at least one open→closed transition observed, derived from the recorded events) and 1 otherwise, naming the bots that fall short; `--replay-hours N` re-derives both answers at N past instants (both readers are deadline-blind, so the open set at instant T is dispatches ≤ T minus terminal reports ≤ T) and records them — the history replay J4 asked for, so the count gate does not start from zero on the day shadow arms.
- **Composition** — a dormant per-fleet timer `plane-shadow` (10-minute cadence) running `plane shadow --record` for every bot on the fleet roster; arming carrier `PLANE_SHADOW_ENABLED=1` through the host-timer arming pattern; a doctor rung summarising streaks and the last divergence per bot.
- **Structural invariants** — the comparator never writes a ledger, never routes the plane's answer into any door (`report-back.sh` is untouched by this chunk), and refuses (rc 3) when either ledger or the db is unreachable — a comparison against an absent ledger is not a clean comparison.
- **Not in chunk 3** — the FLEET ALERT bridge for `critical` divergences (chunk 4 wires fleet-pulse to `plane shadow --gate`), flipping any reader (chunk 5), retiring any write (chunk 5/6).

### Chunk 4 scope — shadow the OVERDUE reader, bridge divergence to the fleet, disclose in brief (written before the code)

Chunk 3 changed the shape of chunk 4: comparisons are RECORDED by a timer against the plane, so no reader is instrumented and no reader changes. What remains from "shadow on the list modes → brief → fleet-pulse; the count gate; history replay" is three things:

- **The overdue reader.** `dispatch-overdue.py --all` (the watchdog's input) is the open set filtered by `expected_by < now`, per bot. `plane shadow` gains `reader=overdue`: the plane's answer is `OPEN_ASSIGNMENTS_AT_SQL` plus `expected_by < instant`, the legacy answer the matcher's own `overdue_all` cut to the bot, the same divergence classes, its own recorded streak (`data.reader` already distinguishes them) and its own line in `--gate`. `--orphans` stays hybrid (J1) and `--unassigned` has no plane counterpart yet — both stated, neither shadowed.
- **The fleet-pulse bridge** (J4: "any `--open-task` mismatch or unclassified case → critical on the existing FLEET ALERT path"). `plane shadow --check`: rc 1 when a bot's LATEST recorded comparison for any reader is diverged with an UNEXPLAINED class or a head disagreement, naming the bots; `fleet-pulse.sh` calls it under `PLANE_SHADOW_ENABLED` and posts a FLEET ALERT through its existing debounced path — the plane never alerts through the fleet it observes on its own, the fleet's own watchdog does. Explained divergences never page.
- **brief.** One `shadow` field in the schema-1 envelope and one text line per bot: the streak (`clean_run`, `transitions`, `last_diverged_at`) per reader from the records, `degraded` when nothing is recorded — read-only, from the plane, never re-derived.

Structural: no reader flips, no write retires, no new carrier (the shadow's flag governs the check too), the `--open-task` bar stays chunk 6's. Not in chunk 4: the flip itself (5), the resolver's 200-resolution gate (6).

### Chunk 5 scope — the flip of the LIST readers, behind a per-reader flag, with the epoch recorded (written before the code)

**What flips.** The two list readers the shadow has been grading: `--open` (the deadline-blind open list) and `--all` (the watchdog's overdue set). **Not `--open-task`** — the resolver keeps its own 200-resolution bar (chunk 6). **Not `--orphans`** (hybrid, host-local `.spawn`) and **not `--unassigned`** (no plane counterpart).

- **Where the plane read lives.** `lib/dispatch-overdue.py` gains a plane SOURCE for those two modes: a stdlib sqlite read of the plane (`lib/plane-lookup.py` / `plane-shadow-check.py` precedent — the matcher is stdlib and every consumer already shells it), the SQL a cross-referenced twin of `queries.OPEN_ASSIGNMENTS_AT_SQL` and the overdue rules mirrored exactly as `shadow.plane_overdue` does them. The JSONL path stays callable by name (`source="jsonl"`) because the shadow keeps grading legacy against plane AFTER the flip; the flag only moves the DEFAULT every consumer gets — `report-back.sh`'s `--open` list, `fleet-pulse.sh`'s `--all`, `brief`'s dispatch section — so the readers flip together (J1: brief and the matcher are one migration).
- **The carrier, per reader.** `PLANE_READ_OPEN=1` and `PLANE_READ_OVERDUE=1`, resolved from the fleet `.env` tier and stamped by the composer on the units whose scripts read the matcher (the `FLEET_JOB_ARMING` table gains two rows; `bot.conf` carries them for the session doors). Never one fleet-level flag (J2). Rollback is the flag back to 0: the legacy write stays alive, unread, for the burn-in window; no legacy-read fallback is built into the plane path.
- **The epoch, recorded when it happens.** `claudlobby --fleet F plane cutover --reader open|overdue` REFUSES unless the shadow gate is met for that reader on every declared bot (`gate_summary`, the J4 bar), records a `cutover_declared` system event (subject the fleet; `data`: the reader, the per-(bot, reader) streaks at that instant, the flag it expects) and prints the flag line the operator adds. `--force <reason>` records the reason in the same event — a flip without the evidence is possible, never silent. `plane doctor` gains a rung: flag set with no declaration recorded, or a declaration with the flag off, is disclosed.
- **Structural.** No write retires in this chunk (chunk 6); the shadow timer keeps running after the flip, now grading the plane path that serves production against the JSONL it still writes; `--open-task` untouched.

### Chunk 6 scope — the resolver, the retirement of the legacy writes, and the doc pass (written before the code; split in three so each carries its own gate)

**6a — `--open-task` shadowed, then flipped.** The resolver is the door with the worst-shaped failure (a stale head becomes a false completion, #1418), so it gets its own reader and its own bar. `plane shadow --reader open_task` compares the two HEADS (the id the resolver would hand `report-back.sh`) and records `shadow_parity_clean/diverged` keyed `open_task`; the gate for this reader is **200 consecutive clean resolutions with at least one transition** (the walk's number — a head that has never changed proves nothing), not the list readers' 20. The matcher gains the plane source for `--open-task` under `PLANE_READ_OPEN_TASK` (the third per-reader flag; `cutover --reader open_task` refuses short of 200); the plane head is the FIRST row of the same open list the flipped `--open` serves, so 6a adds no SQL — it adds a reader, a bar and a flag. The chunk-6 follow-up on #1418 (ledger-absent-with-history → return nothing, never a stale head) lands here on the LEGACY side, because it is the resolver's last legacy defect and the shadow must not grade against it.

**6b — the legacy WRITES retired, per door, as the end of shadowing.** `PLANE_LEGACY_WRITE_DISPATCH` and `PLANE_LEGACY_WRITE_REPORT` (default `1`: keep writing) — `dispatch-task.sh` and `report-back.sh` skip their JSONL append at `0`. Retiring a write ENDS the shadow for every reader that read that ledger (there is no legacy side left to grade), so `plane cutover --retire-writes` refuses unless every reader is declared flipped (`cutover_declared` for open, overdue and open_task) and records `legacy_write_retired` (the doors, the instant, the declarations it stands on); the doctor's rung reads the write flags against that record. `who-reviewed.py` gains `--source plane` (the report facts carry `pr_url` in `data`; the join is the same `pull/<N>` + tolerance rule) so attribution survives the report ledger's retirement; `workstreams` and `briefing-trigger` writes are NOT in scope (their plane adapter is unbuilt — named as the residual, not retired blind).

**6c — the doc pass.** `documentation/architecture/observable-plane.md` (the line "Not yet described in `documentation/architecture/`" in CLAUDE.md is the debt): the families, the envelope, the lanes, the doors and their flags, the cutover state machine (shadow → declare → flip → retire) with the exact commands, and the rollback at each stage. CLAUDE.md's plane paragraph shrinks to a pointer. No code.

*(6a built: PR #1452 — the live id-less emission under the importer's key, `plane-readers.answering_idless`/`head`, `--open-task` under `PLANE_READ_OPEN_TASK`, `shadow.head_streak` with the 200-head bar, `plane cutover --reader open_task`, the #1418 legacy rule.)*

**Order and gates.** 6a needs the flipped `--open` in production long enough to accumulate 200 resolutions (the shadow timer, armed by the operator); 6b needs 6a declared; 6c can land any time and should land first.

### Chunk 6a ruling (before the code): id-less dispatches are emitted LIVE with the importer's content key

The chunk-5 fold found that an id-less dispatch (`query` / `cancel` / `compact` / `restart`) reaches the plane only as a communication — `dispatch-task.sh` emits its work_item + assignment only when a task id was minted — so the plane cannot see it as open work. That gap decides two things at once in 6a, so it is closed at the source rather than papered over per reader: the resolver's legacy guard (a terminal report landing after an unanswered id-less dispatch answers THAT, never the oldest id'd row — the #1418 false-completion class) needs a plane twin, and the watchdog's overdue reader must be able to page an overdue id-less dispatch as legacy does (`-`).

**The rule.** The door composes the ledger line first, keys it exactly as the importer does (`dispatch-log:sha:<derive_hex(line)>` = sha256 of the stripped line, 32 hex), emits work_item + assignment + communication under that ref, then appends the line — so a later import of the same row classifies as a duplicate, never a second assignment. The stdlib readers already keep `sha:` rows for the overdue reader and drop them from the open list; the plane resolver gains `answering_idless` (newest assignment for the bot is `sha:`-keyed with no terminal task event at or after it → resolve nothing), the exact twin of `_answering_an_idless_dispatch`.

**The resolver's bar.** `open_task` is a STREAK MODE over the open reader's records, not a third comparison: every open record already carries `head_legacy` / `head_plane` / `head_agrees`. Its streak counts consecutive records whose heads agree, with a transition when the head CHANGES between two agreeing records, and its bar is 200 clean + 1 transition. `plane cutover --reader open_task` gates on it; the matcher serves `--open-task` from the plane under `PLANE_READ_OPEN_TASK` (flag AND declaration, like the list readers).

**The legacy side's last defect** (#1418): an ABSENT report ledger with an oldest open id'd dispatch older than the expiry cap resolves NOTHING (a stale head is likelier than a first report a day late); a young one still resolves — the first report of a new fleet keeps working.

### The walk, built (2026-09-03)

Every chunk of the build queue has landed on main: 1 (the lookup door, #1444), 2 (parity + import), 3/3b (the shadow primitive, the supersession backfill), 4 (the overdue reader, the fleet-pulse bridge, brief's section), 5 (the flip behind per-reader flags, the epoch recorded — #1451), 6a (the resolver's own streak and flip; id-less dispatches emitted live and answered — #1452), 6c (the architecture reference — #1453) and 6b (the legacy writes retired per door as the end of shadowing; who-reviewed's plane join — #1454). Two rulings the build added to the walk: **a flip is flag AND declaration** (a flag alone is disclosed and the JSONL keeps serving), and **a retired write is skipped only on four facts** (flag, arming, the recorded retirement, that emission's success) — every other case writes the ledger and says why.

What remains is the operator's, in order: arm the shadow timer (`PLANE_SHADOW_ENABLED=1`) and let real dispatches accrue the gates (the J4 bar needs a transition per bot and reader; the resolver's bar 200 agreeing answers); declare and flip each reader (`plane cutover --reader …`, then `PLANE_READ_*=1` in the fleet `.env` tier, `generate`, restart the sessions); then retire the writes (`plane cutover --retire-writes`, `PLANE_LEGACY_WRITE_*=0`). The named residual: `--unassigned` and the workstreams/briefing writes have no plane path yet; the `#526` cross-fleet artifact in the legacy overdue reader disappears with the flip; the Pi joins after its SSD fix.

## The completion — "no JSONL, full database" (ruled 2026-09-03; scoped before the code)

The operator's end state is a fleet with no JSONL ledger left load-bearing. The inventory (every `.jsonl` the lib and the package write or read; eval-harness artifacts and Claude Code's own transcripts excluded) leaves exactly three live ledgers and one archive:

| Surface | Writer | Readers | State |
|---|---|---|---|
| `state/dispatch-log.jsonl` (host-global) | `dispatch-task.sh` | the matcher (`--open`, `--all`, `--orphans`, `--open-task`, `--unassigned`), brief, fleet-pulse, report-back's resolver, the supersede hint, the shadow | cutover built; the flip and the retirement wait on the gates |
| `local/<fleet>/runtime/report-back.jsonl` | `report-back.sh` | the matcher, brief (unacked reports), `claudlobby report-back`, who-reviewed, the shadow | same |
| `data/events/fleet-YYYY-MM-DD.jsonl` (per bot) + `state/events/…` (fleet-level receipts) | `emit_fleet_event` — nine scripts, ~30 event types (`activity_stuck`, `overdue_dispatch`, `bridge_down`, `send_miss`, `script_error`, `briefing_*`, `audit_*`, `bot_teardown_started`, …) | fleet-pulse's escalation (persistent critical events → Telegram), `claudlobby events`, brief's critical events, uptime, bot-vitals, tail-fleet, data-sweep | NOT on the plane |
| `workstreams-archive.jsonl` (+ the `workstreams.json` registry, JSON) | `workstream-update.sh` | `claudlobby workstreams`, brief's workstreams section | the plane holds the workstream events; the registry read has no plane adapter |

**Phase A — finish F18's residual.** A1: `--unassigned` gets a plane reader (the idle-worker check is purely temporal — newest assignment vs newest terminal task event per bot — so the plane answers it without new SQL families), a shadow reader `unassigned` under the list readers' bar, `PLANE_READ_UNASSIGNED`, and the retire door stops naming it as frozen. A2: `claudlobby workstreams` reads the plane's `workstreams` table + events behind `PLANE_READ_WORKSTREAMS` (flag AND declaration), `workstream-update.sh`'s archive append retires on the same four facts as the other doors, and the JSON registry becomes a derived cache the plane can rebuild. A3 (operator, done 2026-09-03): the shadow timer armed on the Mini so every gate accrues from real dispatches.

**Phase B — the bot-events ledger to the plane** (a walk of its own, junctions first): every `emit_fleet_event` type registered with a severity (the `SYSTEM_EVENT_SEVERITY` registry pattern — unknown tokens ingest with NULL severity, F19); per-bot events anchored on the bot's actor uid, fleet-level receipts on the fleet; the escalation reader in fleet-pulse (persistent critical events within a window) as a Lane-C query; `claudlobby events`, brief's critical events, uptime and bot-vitals reading the plane; a retention lane for chatty types (the `metric_samples` precedent, never the ledger); the readback loop that fleet-pulse runs on its own events. Then the same flag → declaration → flip → retire shape, per reader, behind its own shadow.

**Phase C — the flips and the retirement on the Mini**, in the walk's order: declare and flip each reader when `plane shadow --gate` says met; `--force <reason>` is the operator's decision where an idle fleet cannot produce the transitions the bar needs; then `plane cutover --retire-writes`. The end state the doctor shows: every `cutover <reader>` flipped, every `legacy write <door>` retired, and no ledger growing.

### Ruling 2026-09-03 (operator): no backward compatibility — hard flip, fix forward

"We don't need to do backwards compat and should just push ahead and hard flip to the database and fix forward if need be." What changes: the shadow gates no longer BLOCK a flip — they stay as instruments (`plane shadow --record` keeps running from the armed timer; `--gate` and the doctor keep reporting), but every reader is declared with `--force "operator ruling 2026-09-03: hard flip, fix forward"` and flipped in one restart cycle, and the writes retire in the same cycle. Phase B (the bot-events ledger) is built as a direct move — the writer to the plane, the readers from the plane, the JSONL write dropped in the same chunk — with no per-reader shadow. The per-chunk gauntlet is unchanged (it is about correctness, not compatibility). What "fix forward" means in practice: every remaining reader of a retired ledger is ported as it bites, in one follow-on chunk — `claudlobby report-back` (the human table), brief's unacked-reports section and its `--ack` cursor, `who-reviewed`'s default source, the supersede hint's task text (the communication body, under `full` capture), `--unassigned` (built first, so the watchdog's idle-worker check is never blind).

The flip cycle on the Mini, in order: declare the four readers (`--force`), `--retire-writes`, set the six flags in the fleet `.env` tier (`PLANE_READ_OPEN`, `_OVERDUE`, `_OPEN_TASK`, `_UNASSIGNED` = 1; `PLANE_LEGACY_WRITE_DISPATCH`, `_REPORT` = 0), `generate` (bot.conf + the fleet-pulse stamp), re-enrol fleet-pulse, ONE rolling restart (`lib/rolling-restart.sh`, gated on `BRIDGE_READY` per bot — never a mass restart), then the doctor shows every reader flipped and every write retired, and a live dispatch + report land on the plane only.

### Phase B walk — the bot-events ledger to the plane (scoped 2026-09-03, before the code; a direct move under the hard-flip ruling)

**The inventory.** `emit_fleet_event` has 30 call sites across nine scripts and ~28 distinct types: the watchdog's findings (`activity_stuck`, `overdue_dispatch`, `dispatch_orphaned`, `worker_unassigned`, `pane_stuck`, `bridge_down`, `service_down`, `session_missing`, `wip_uncommitted`, `alert_delivery_failed`), the send primitive's (`send_miss`, `send_retry`, `send_blind`, `send_blind_recovered`), boot's (`rc_timeout`, `resume_skipped`, `plugin_marketplace_failed`), the briefing and audit timers' (`briefing_*`, `audit_*`, `sweep_repo_unreachable`), lifecycle (`bot_teardown_started`, `handoff_skipped`), and `script_error` from the ERR trap. Readers: fleet-pulse's escalation (persistent critical types within a 10-minute window → Telegram, with a readback loop over today's and yesterday's files), `claudlobby events`, brief's ALERTS section (`CRITICAL_TYPES`, last 24h), `uptime`, `bot-vitals`, `tail-fleet`, `data-sweep`.

**Junctions, ruled here.** J-B1 *family*: these are SYSTEM events on the plane (`kind='system'`), never a new table — the SystemEvent contract already carries subject anchors (actor for a bot's event, fleet for a fleet-level receipt) and a registry-stamped severity; every legacy type is registered in `SYSTEM_EVENT_SEVERITY` with the severity `CRITICAL_TYPES` implies (critical / notice), and an unknown type still ingests with NULL severity (F19). J-B2 *identity*: a bot's event is anchored on its actor uid by alias (`bot:<fleet>/<bot>`); a fleet-level receipt (empty bot dir) on the fleet; the emitter's `source` field rides `data.source`. J-B3 *the writer*: `emit_fleet_event` itself emits through `plane_emit_events` (the shim ladder, non-blocking, `PLANE_EMIT_LAST_RC` surfaced) and the JSONL append retires behind `PLANE_LEGACY_WRITE_EVENTS=0` on the same four facts as the other doors — one predicate, one retirement record extended with the third door. J-B4 *the readers*: the escalation window is a Lane-C query (`system events by fleet, severity critical, occurred_at within the window, grouped by (subject, event)` — the readback loop is the same query one pass later); `claudlobby events`, brief's ALERTS, `uptime` and `bot-vitals` read the plane behind `PLANE_READ_EVENTS`; `tail-fleet --events` and `data-sweep`'s reap keep their file semantics until the write retires (then they have nothing to read and say so). J-B5 *retention*: chatty types (`send_*`, `script_error`) get the `metric_samples` retention lane's shape — a family-scoped DELETE by `ingested_at` past 30 days for system events of registered CHATTY types only, never critical ones, never the ledger. J-B6 *no shadow*: under the hard-flip ruling the readers move directly; the parity door gains an `events` ledger (rows keyed by content hash) so the gap is measurable after the fact.

**Chunks.** B1: the registry + the writer (types registered; `emit_fleet_event` emits a system event; the append behind the flag; the retirement record covers three doors). B2: the readers (the escalation query in `queries.py` + fleet-pulse's escalation through the stdlib readers; `claudlobby events`, brief ALERTS, `uptime`, `bot-vitals` from the plane; parity's `events` ledger). B3: retention for chatty types + the doc pass. Then the flip cycle covers this door too.
