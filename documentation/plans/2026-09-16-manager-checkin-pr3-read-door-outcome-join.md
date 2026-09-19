---
title: "Manager Check-in — PR 3: The Read Door, Whole — Implementation Plan"
type: plan
status: draft
owner: fleet owner
created: 2026-09-16
---

# Manager Check-in — PR 3: The Read Door, Whole — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans, task by task. Steps use checkbox (`- [ ]`) syntax.
>
> **Operator ruling (2026-09-16):** the loop is built as one series of four PRs with **mechanical gates only**
> — tests and mutants. No baselines, no control fleet, no pre-registered bar, no burn-in verdict, no review
> cycles. This plan ships **no evaluative apparatus**: it produces facts (a status, a count, a distribution)
> and **never a verdict**. Where the spec's §12.4 bar wants a judgment, that judgment is chunk 4's arithmetic
> over these facts, not this door's.

**Goal:** `claudlobby checkins` stops being a list of decisions and becomes a list of decisions *with what
happened next*. Each `checkin_decision` row is joined — through the plane's own tables, never a JSONL ledger —
to the `checkin_dispatch` rows written in the same batch as its assignment, and each of those is resolved to
the dispatched task's status so far through `TASK_STATUS_SQL`, the shipped constant, appended to and never
re-derived. The join appears on every row of `--json` (a `dispatches: []` list with the task id, the assignment
id, the plane's raw status and a bucket) and in the text listing; `--summary` rolls the window up — actions,
ask rate, `considered` lengths, `unavailable` frequencies, dispatch outcomes — grouped by `project_key`; and
the window and the bot filter, which PR 1 deliberately left in Python and pushed here, bind in SQL.

## Architecture

**The walk, in three hops, none of them per row.**

1. **Decision → its address.** A `checkin_decision` is stamped `source_ref = checkin:<ck_id>` (spec §7: "the
   ref is the address a reader joins on"). PR 1's `CHECKIN_ROWS_SQL` does not select `source_ref`, so the only
   key available is the `checkin_id` *inside* the parsed record — `None` for a truncated or data-less row,
   exactly the rows that most need looking at. PR 3 adds `e.source_ref` to the SELECT and a `checkin_ref` key
   to `_row`. **Existing keys are untouched**, so no PR-1 assertion moves.
2. **Address → the join rows.** The DDL forces a `kind='system'` row's `assignment_id` and `work_item_id`
   **columns** to NULL (`claudlobby/plane/migrations/0001_kernel.sql:266-270`), so the address lives in the
   detail and the query is a `json_extract`, never the column. One query for every decision in the page — an
   `IN (…)` over the collected ids — never one per row.
3. **Assignment → status.** `TASK_STATUS_SQL` (`claudlobby/plane/queries.py:683`) returns
   `(assignment_id, status, terminal_at)` from `assignments a` with no WHERE, and `view.py:650-652` already
   narrows it by appending `WHERE a.assignment_id IN (…)`. PR 3 reuses that exact pattern. Nothing re-derives a
   status: `lib/dispatch-overdue.py:94` carries a *different* terminal set
   (`{completed, failed, blocked, cancelled}` — the report-back ledger's vocabulary), and a second copy of
   "what does finished mean" is how two doors fork.

**json_valid is not defensive padding.** `SystemEvent.data` is DIAGNOSTIC: over-cap **truncates** at ingest
with `detail_truncated=1` rather than rejecting (`claudlobby/plane/contracts.py:389-406`), so a
`checkin_dispatch` row can hold non-JSON. Probed on this tree (sqlite 3.53.2): `json_extract` over a truncated
detail **raises `OperationalError: malformed JSON` and takes out the whole query** — one over-cap join row
would turn every decision's outcome into an exception. The extract therefore always receives valid JSON by
construction (`_detail_json`), not by trusting AND to short-circuit.

**Unreachable ≠ empty, in miniature.** The join reads the connection that already served the decision rows, so
unreachability cannot arise *after* the rows are in hand — `plane_session` + `refuse_unreachable` answered
that at rc 3 (PR 1). What *can* arise is a `checkin_dispatch` row whose `assignment_id` names no row in
`assignments`: **absence inside a reachable source**, reported as `status: null`, `outcome: "unjoined"` —
never `open`, which reads as good news, and never dropped, which reads as no dispatch at all. Same rule and
same direction as `claudlobby/source_state.py`.

**The bucket map has an upper bound.** `_OUTCOME` maps raw status → bucket explicitly; anything unmapped reads
`open`, which fails in the "everything is fine" direction, so it is bounded by a test:
`set(TERMINAL_TASK_EVENTS) <= set(_OUTCOME)`. A terminal event added to the plane without a bucket fails that
test instead of silently reading as still-running.

## Scope

**In PR 3:**
- The outcome join: `checkin_dispatch` → assignment → `TASK_STATUS_SQL` status, on `--json` and in text.
- `checkins --summary`: action distribution, ask rate, `considered` lengths, `unavailable` frequencies,
  dispatch outcomes — grouped by `project_key`.
- `--limit N`; `--since` bound in SQL; `subject_alias` bound in SQL when `--bot` is given.
- Docs: `CLAUDE.md` (the `commands/` line's `checkins` clause; the `# Operations` command line),
  `documentation/guides/observability.md` (replace PR 1's interim raw-`sqlite3` row with the door),
  `CHANGELOG.md`.

**Not in PR 3:**
- `lib/plane-lookup.py --checkin-dispatch` — **decided against, Task 1 step 3.** It is the *bash* door; this
  join's one consumer is Python holding an open `open_ro` connection, so building it now ships a second copy
  of the SQL with no caller — the `--supersedes` dead-flag class (`lib/dispatch-supersede-hint.py:4-10`,
  #1032: built, never passed, retired zero rows in a week).
- An operator-plane card or route (a feature, later — #1562; the seam is `collect_checkins`).
- Anything evaluative: no bar, no closure rate, no verdict, no "healthy"/"inert" label.
- The default (`requires:`, the `leaf-manager` role) — PR 4. `propose` / sprint / focus — their own chunks.
- An `ask` outcome join (asks correlate by alias + time window; F4 defers that door until the correlation
  proves ambiguous, and nothing here makes it less ambiguous).
- Two more `--summary` facts the spec's §11 lists: the mean gap between check-ins, and skip reasons
  (`checkin_skipped` is a different event, so an all-skipped window reads `checkins: 0` here) — tracked
  on #1563, alongside the operator-plane card #1562.

## Global Constraints

Copied from PR 1's plan; each still binds here.

- **The repo is PUBLIC.** No PII, real chat ids, handles, tokens, tailnet names, host install paths or
  fleet-specific paths in any committed asset, test, fixture, commit message or PR body. **Never write the
  canary fleet's name, its manager's name, any bot id, the host install root or a tailnet name** — they live
  in `$OUT/env.sh` only, and `no_names` gates every file bound for a public body.
- **Doors' rc ladder.** A read door: **0** answered · **2** malformed call (no fleet, unparseable `--since`,
  an incompatible flag pair) · **3** the question cannot be answered (`refuse_unreachable`, upper-case
  `UNREACHABLE`, `claudlobby/commands/_helpers.py:160`). `dispatch-overdue.py`'s convention, which PR 1's
  docstring already records as deliberate against `report-back`'s rc 1.
- **`--json` is never capped.** `TEXT_ROW_LIMIT` (`claudlobby/brief.py:122`) caps the *text* listing and says
  so. `--limit` is different in kind: an operator's explicit bound, applied to both surfaces, and stated in
  the scope line.
- **Read-only db.** Rows come through `plane.db.open_ro` (`claudlobby/plane/db.py:28`); reachability through
  `brief.plane_session` (`claudlobby/brief.py:252`), whose connection yields **tuples** and is probed and
  closed, never read from. No write, no migration, no schema change — `checkin_dispatch` is already a
  registry-governed system event.
- **No private mint, no private copy of a shared predicate.** `TASK_STATUS_SQL`, `TERMINAL_TASK_EVENTS`,
  `fleet_alias_range`, `fleet_range_params` and `_epoch` are imported from `claudlobby/plane/queries.py`,
  never restated. `_epoch(expr)` → `CAST(strftime('%s', expr) AS INTEGER)` (`queries.py:309-325`); a bind is
  `_epoch("?")`, the form `queries.py:407,427` already uses — a lexical `<` over mixed offsets is the bug that
  constant exists to prevent.
- **Line numbers** are as of the PR-3 worktree's tip. Re-anchor by the symbol beside a line, never the number.
- **Tests run unsandboxed; the baseline is red.** The gate is *names + counts* + the rc gate (CLAUDE.md's
  three checks), never `pytest | grep`. Redirect, read `$?`, then grep the file.
- **Test command form:** `./.venv/bin/pytest tests/<file>.py -q` from `$WT`.
- **Commits:** one per task, message via `git commit -F <file>`, ending with the `Co-Authored-By:` trailer the
  executing session is instructed to use (PR 1's commit blocks show the form).

## File structure

**Create**

| Path | Responsibility |
|---|---|
| `tests/test_checkins_outcome_join.py` | The join: bucket mapping, `task_id` null, a missing assignment, a truncated join row, fleet scoping, the text rendering, the N-queries bound. |
| `tests/test_checkins_summary.py` | `--summary`: grouping by `project_key`, the counts, the refusals, the text form. |

**Modify**

| Path | Change |
|---|---|
| `claudlobby/plane/queries.py` (the check-in block PR 1 appended) | `CHECKIN_ROWS_SQL` split into head + order so optional terms can be inserted; `checkin_rows_sql(*, since, bot, limit)`; `_detail_json()`; `checkin_dispatch_rows_sql(n)`. |
| `claudlobby/commands/checkins.py` | `_OUTCOME` / `_outcome_of`; `_join_dispatches(conn, fleet, refs)`; `_row` gains `checkin_ref` + `dispatches`; `collect_checkins` gains `limit` and the join; `summarize(rows)`; `cmd_checkins` gains `--summary`/`--limit`, the SQL binds, the text rendering. |
| `claudlobby/commands/_parsers.py` (the `checkins` parser PR 1 added) | `--summary`, `--limit N`. |
| `CLAUDE.md` (the `commands/` line, `:471`; `# Operations`) | the `checkins` clause gains the join and `--summary`; the command line gains the two flags. **File count unchanged** — PR 3 adds no `commands/` file. |
| `documentation/guides/observability.md` (question→door table) | replace PR 1's interim raw-`sqlite3` row with `claudlobby checkins --bot <b> --json`; add the `--summary` row. |
| `CHANGELOG.md` (`[Unreleased]`) | one bullet: the outcome join, `--summary`, the bounds. |

**Sizing:** Task 1 L · Task 2 S · Task 3 L · Task 4 M · Task 5 S · Task 6 M.

---

### Task 1: The outcome join — the query, the bucket map, `--json`

**Files:** modify `claudlobby/plane/queries.py`, `claudlobby/commands/checkins.py`; create
`tests/test_checkins_outcome_join.py`.

**Interfaces:** consumes `TASK_STATUS_SQL`, `TERMINAL_TASK_EVENTS`, `fleet_alias_range`, `fleet_range_params`,
`_epoch` (all `claudlobby/plane/queries.py`); `open_ro` (`claudlobby/plane/db.py:28`). Produces
`_detail_json(col) -> str`, `checkin_dispatch_rows_sql(n: int) -> str` (binds: fleet, fleet, then one per
checkin id), `_OUTCOME: dict[str, str]`, `_outcome_of(status: str | None) -> str`,
`_join_dispatches(conn, fleet: str, refs: list[str]) -> dict[str, list[dict]]`, and two new keys on `_row`'s
dict: `checkin_ref: str | None`, `dispatches: list[dict]`. Each dispatch dict:
`{assignment_id, work_item_id, task_id, status, outcome, terminal_at, occurred_at}`.

- [ ] **Step 1: Write the failing tests** — `tests/test_checkins_outcome_join.py`, importing PR 1's fixtures
      (`_Args`, `_decision`, `_ago`, `_out`, `F`, `root`) from `tests/test_checkins_cli.py` rather than
      re-declaring them (pytest binds an imported fixture in the importing module's namespace; `root` is
      `autouse`, so it applies here too). One new helper, `_dispatched(root, ck, *, task_id, terminal=None)`, seeds a dispatch
      the way `dispatch-task.sh --checkin` does — `work_item` + `assignment` + the `checkin_dispatch` system
      event in ONE `emit_batch`, actor-anchored on the dispatcher (`subject_kind: actor`,
      `subject: bot:<F>/mgr`), detail `{checkin_id, assignment_id, work_item_id, task_id | None}` — and
      optionally appends a terminal task event.

| Test | Asserts |
|---|---|
| `test_a_decision_carries_its_dispatch_with_the_planes_own_status` | one `dispatch` decision + one joined assignment with a `completed` task event → `rows[0]["dispatches"]` has one entry whose `task_id`, `assignment_id`, `work_item_id` match the seed, `status == "completed"`, `outcome == "completed"`, `terminal_at` is the event's instant. |
| `test_every_terminal_task_event_has_a_bucket` | `set(queries.TERMINAL_TASK_EVENTS) <= set(cmd._OUTCOME)` — the upper bound on the `open` default. |
| `test_the_buckets_map_the_plane_vocabulary` | parametrized over `(completed→completed, returned_blocked→blocked, failed→failed, expired→failed, cancelled→retired, superseded→retired, reassigned→retired)` seeded as real task events, plus `dispatch_failed→failed` seeded as a failed transmission, plus `progress→open` and a never-sent assignment (`created_not_sent`) → `open`. |
| `test_an_id_less_dispatch_still_resolves_through_the_assignment` | `task_id: None` in the join detail → the entry has `task_id is None` **and** a real `status`/`outcome`. (The spec's §12.4 calls an id-less dispatch "unjoined"; see step 3.) |
| `test_a_join_row_naming_no_assignment_is_unjoined_never_open` | the detail names an `asg_` id nothing holds → `status is None and outcome == "unjoined"`. |
| `test_a_dispatch_decision_with_no_join_row_lists_empty` | `action="dispatch"`, no `checkin_dispatch` → `dispatches == []` (the summary is what counts it; the row does not invent one). |
| `test_a_truncated_join_row_does_not_take_out_the_query` | one good join row + one whose detail is `"x" * 20000` (over the DIAGNOSTIC cap, truncated at ingest) → rc 0 and the good row still resolves. **Without the `json_valid` guard this raises `OperationalError: malformed JSON`** — probed on sqlite 3.53.2. |
| `test_a_truncated_decision_still_joins_through_its_source_ref` | a decision whose record is over-cap (so `checkin_id is None`, PR 1's own assertion) still carries `checkin_ref == f"checkin:{ck}"` and its dispatch. |
| `test_another_fleets_join_row_never_attaches` | the same `checkin_id` emitted under a second fleet's dispatcher alias → it does not appear on this fleet's row (#526's class). |
| `test_the_join_is_two_queries_not_one_per_row` | 12 decisions each with a dispatch; wrap `conn.execute` with a counter → the join costs **2** executes total, whatever the row count. |

- [ ] **Step 2: Run them to verify they fail** — Run: `./.venv/bin/pytest tests/test_checkins_outcome_join.py -q`
      Expected: failures/errors naming `dispatches`, `_OUTCOME` and `checkin_ref` — not a collection error.

- [ ] **Step 3: Decide `plane-lookup.py --checkin-dispatch` — and record the decision**

**No.** `lib/plane-lookup.py` is the door *bash* callers use (`dispatch-task.sh` consumes its `--checkin-id`
mode). This join's only consumer is `claudlobby/commands/checkins.py`, which already holds a read connection
through `open_ro` and would otherwise pay a subprocess to ask a sibling for rows it can read directly — and
would carry a second copy of the SQL that only the read door exercises. That is the shape #1032 measured:
`--supersedes` shipped the door and nobody passed it. If a bash consumer appears (chunk 2's trigger reading
its own last outcome), it is one `--checkin-dispatch` mode added *then*, against a caller. Write this
paragraph into the docstring of `checkin_dispatch_rows_sql` so the next reader does not re-open it.

- [ ] **Step 4: The queries** — append beside PR 1's check-in block in `claudlobby/plane/queries.py`:

```python
def _detail_json(col: str) -> str:
    """A detail column json_extract can always be handed. `SystemEvent.data` is
    DIAGNOSTIC -- over-cap TRUNCATES at ingest rather than rejecting -- so a detail
    can be non-JSON, and json_extract over one RAISES `malformed JSON` and takes
    out the WHOLE query (probed, sqlite 3.53.2). A CASE rather than a second AND
    term, so it holds by construction and not by trusting the optimizer's order."""
    return f"CASE WHEN json_valid({col}) THEN {col} ELSE '{{}}' END"


def checkin_dispatch_rows_sql(n: int) -> str:
    """The `checkin_dispatch` join rows for n checkin ids, oldest first.

    `dispatch-task.sh --checkin` appends ONE of these to the SAME batch as the
    assignment, carrying {checkin_id, assignment_id, work_item_id, task_id}. The
    DDL forces a system row's assignment_id / work_item_id COLUMNS to NULL
    (0001_kernel.sql), so the address lives in the detail and the join is a
    json_extract -- never the column, which is null by construction for this kind.
    Fleet-scoped on the DISPATCHER's own alias (the decision rows' own predicate):
    a 32-hex id is unique, but one bot name on two fleets (#526) is the failure it
    costs nothing to exclude. Served by idx_events_kind_seq / idx_events_fleet_system.

    There is deliberately no `plane-lookup.py --checkin-dispatch` sibling: this
    query's only consumer is claudlobby/commands/checkins.py, which holds its own
    read connection. A bash-side copy with no bash caller is the `--supersedes`
    dead-flag shape (#1032) -- add the mode when a caller exists.

    Binds: fleet, fleet, then one per checkin id.
    """
    ph = ",".join("?" * n)
    d = _detail_json("e.detail")
    return (
        f"SELECT json_extract({d}, '$.checkin_id') AS checkin_id,"
        f" json_extract({d}, '$.assignment_id') AS assignment_id,"
        f" json_extract({d}, '$.work_item_id') AS work_item_id,"
        f" json_extract({d}, '$.task_id') AS task_id,"
        " e.occurred_at AS occurred_at, e.ingest_seq AS ingest_seq"
        " FROM events e"
        " WHERE e.kind = 'system' AND e.event = 'checkin_dispatch'"
        f" AND {fleet_alias_range('e.subject_alias')}"
        f" AND json_extract({d}, '$.checkin_id') IN ({ph})"
        f" ORDER BY {_epoch('e.occurred_at')}, e.ingest_seq"
    )
```

Add `e.source_ref AS source_ref,` to `CHECKIN_ROWS_SQL`'s SELECT list (Task 4 restructures the constant; here
it is one column).

- [ ] **Step 5: The mapping and the join** — in `claudlobby/commands/checkins.py`:

```python
from ..plane.queries import TASK_STATUS_SQL, TERMINAL_TASK_EVENTS, checkin_dispatch_rows_sql

# Raw TASK_STATUS_SQL status -> the bucket a reader acts on. Six, not five: the
# spec's bar (§12.4) names `blocked`/`failed` as the terminal-not-completed classes
# that count AGAINST, and `cancelled` / `superseded` / `reassigned` are neither --
# folding a withdrawal or a re-dispatch into `failed` would score a manager that
# corrected itself worse than one that did nothing, so they get their own bucket and
# chunk 4 decides what to do with it. `dispatch_failed` is NOT a task event (it is
# derived from transmissions): the send never landed, a failure to start. Anything
# unmapped reads `open` -- bounded by test_every_terminal_task_event_has_a_bucket.
_OUTCOME = {
    "completed": "completed",
    "returned_blocked": "blocked",
    "failed": "failed",
    "expired": "failed",
    "dispatch_failed": "failed",
    "cancelled": "retired",
    "superseded": "retired",
    "reassigned": "retired",
}
OUTCOMES = ("completed", "blocked", "failed", "retired", "open", "unjoined")


def _outcome_of(status: str | None) -> str:
    """None = the join row names an assignment the plane does not hold: absence
    inside a reachable source, reported as `unjoined`, never as `open`."""
    return _OUTCOME.get(status, "open") if status else "unjoined"


def _join_dispatches(conn, fleet: str, refs: list[str]) -> dict[str, list[dict]]:
    """checkin_id -> its dispatches, resolved to the plane's own status. TWO
    queries for the whole page, never one per row: the join rows for every id at
    once, then TASK_STATUS_SQL narrowed by `WHERE a.assignment_id IN (...)` --
    view.py's own pattern, the shipped constant APPENDED to and never copied."""
    ids = [r.split("checkin:", 1)[1] for r in refs if r and r.startswith("checkin:")]
    if not ids:
        return {}
    links = list(conn.execute(checkin_dispatch_rows_sql(len(ids)),
                              (*fleet_range_params(fleet), *ids)))
    asg = [r["assignment_id"] for r in links if r["assignment_id"]]
    status: dict[str, tuple] = {}
    if asg:
        ph = ",".join("?" * len(asg))
        status = {r["assignment_id"]: (r["status"], r["terminal_at"])
                  for r in conn.execute(
                      TASK_STATUS_SQL + f" WHERE a.assignment_id IN ({ph})", asg)}
    out: dict[str, list[dict]] = {}
    for r in links:
        st, at = status.get(r["assignment_id"], (None, None))
        out.setdefault(r["checkin_id"], []).append({
            "assignment_id": r["assignment_id"], "work_item_id": r["work_item_id"],
            "task_id": r["task_id"], "status": st, "outcome": _outcome_of(st),
            "terminal_at": at, "occurred_at": r["occurred_at"],
        })
    return out
```

In `_row`, add `"checkin_ref": r["source_ref"]` and `"dispatches": []`. In `collect_checkins`, after the loop
that builds `out`, attach: `joined = _join_dispatches(conn, fleet, [x["checkin_ref"] for x in out])`, then for
each row `row["dispatches"] = joined.get((row["checkin_ref"] or "").split("checkin:", 1)[-1], [])`.

- [ ] **Step 6: Run the tests** — Run:
      `./.venv/bin/pytest tests/test_checkins_outcome_join.py tests/test_checkins_cli.py -q`
      Expected: all pass, PR 1's file included and unchanged (the join is additive; `checkin_id` still `None`
      on a truncated record).

- [ ] **Step 7: Commit** — `git add claudlobby/plane/queries.py claudlobby/commands/checkins.py tests/test_checkins_outcome_join.py`;
      subject `feat(cli): checkins — the outcome join, through the plane's own tables`.

---

### Task 2: The join in the text listing

**Files:** modify `claudlobby/commands/checkins.py`; extend `tests/test_checkins_outcome_join.py`.

**Interfaces:** no new function. `cmd_checkins`'s text branch gains one line per dispatch, and one line for an
`action == "dispatch"` decision that has none.

- [ ] **Step 1: Write the failing tests**

| Test | Asserts |
|---|---|
| `test_the_text_listing_names_the_dispatch_and_its_status` | a joined `completed` dispatch renders a line containing the task id, `completed`, and the `asg_` id. |
| `test_the_text_listing_says_when_a_dispatch_decision_joined_nothing` | `action="dispatch"` with no join row renders `no dispatch joined to this decision`; a `nothing`/`ask` decision renders no such line (absence of a dispatch is only notable where one was decided). |
| `test_an_id_less_dispatch_renders_as_id_less_not_as_blank` | `task_id is None` renders the literal `id-less`, plus the status — never an empty column that reads as missing data. |
| `test_the_dispatch_lines_are_inside_the_row_cap` | 12 decisions each with a dispatch → 10 rendered rows, the disclosure line present, and the dispatch lines counted only for rendered rows. |

- [ ] **Step 2: Run them to verify they fail** — Run: `./.venv/bin/pytest tests/test_checkins_outcome_join.py -q`
      Expected: the four new tests fail on missing output; the Task 1 tests still pass.

- [ ] **Step 3: Render it** — after the `unavailable:` line in `cmd_checkins`'s per-row block:

```python
        for d in r["dispatches"]:
            tid = d["task_id"] or "id-less"
            when = f" ({d['terminal_at']})" if d["terminal_at"] else ""
            print(f"      → {tid}  {d['outcome']} [{d['status'] or 'no assignment row'}]{when}"
                  f"  {d['assignment_id']}")
        if not r["dispatches"] and r["action"] == "dispatch":
            print("      → no dispatch joined to this decision")
```

- [ ] **Step 4: Run the tests** — Run: `./.venv/bin/pytest tests/test_checkins_outcome_join.py -q`
      Expected: all pass.

- [ ] **Step 5: Commit** — subject `feat(cli): checkins — the join in the text listing`.

---

### Task 3: `checkins --summary`

**Files:** modify `claudlobby/commands/checkins.py`, `claudlobby/commands/_parsers.py`; create
`tests/test_checkins_summary.py`.

**Interfaces:** produces `summarize(rows: list[dict]) -> dict` — a **pure function of the rows**, so it is
unit-testable with no db and cannot acquire a second source. Envelope:

```json
{"schema": 1, "fleet": "…", "since": "…|null", "scope": "…",
 "totals": {"checkins": 0, "actions": {"dispatch": 0, "ask": 0, "nothing": 0},
            "raised": 0, "ask_rate": 0.0, "no_record": 0,
            "considered": {"rows": 0, "empty": 0, "min": null, "max": null, "mean": null},
            "unavailable": {"gh": 0},
            "dispatches": 0, "dispatch_outcomes": {"completed": 0, "blocked": 0, "failed": 0,
                                                   "retired": 0, "open": 0, "unjoined": 0},
            "dispatch_statuses": {"completed": 0}},
 "projects": [{"project_key": "shop", "…the same block…": {}}]}
```

Rules, each a test:
- `projects` is a **list**, sorted by key with the `null` group **last**: a JSON object cannot hold a null key,
  and mapping it to `"-"` would collide with a project legitimately named `-`.
- `ask_rate` is `raised / checkins` to 3 places, `0.0` when `checkins == 0` — a **rate, not a verdict**.
- `considered` is measured over rows **with a parsed record only**; `rows` says how many that was and
  `no_record` how many it excluded, so a window of truncated rows cannot read as a window of empty
  `considered` lists. `min`/`max`/`mean` are `null` when `rows == 0`.
- `dispatch_outcomes` counts **join rows**, except that a decision with `action == "dispatch"` and no join row
  adds **1** to `unjoined` — the one place the summary counts something the row list does not contain, so it
  gets its own test and its own negative (a quiet `ask`/`nothing` row adds nothing).
- `dispatch_statuses` carries the plane's **raw** status counts beside the buckets: two lines of code, and
  what makes a wrong mapping visible without reading `_OUTCOME`.
- `--summary` **refuses `--last` and `--limit`** at rc 2, each with its own message: a summary over a
  truncated slice states a window it did not read.

- [ ] **Step 1: Write the failing tests** — `tests/test_checkins_summary.py`. First extend PR 1's `_Args` in
      `tests/test_checkins_cli.py` with `summary=False` and `limit=None` defaults: `cmd_checkins` reads both
      through `getattr`, but a test namespace that lies about the CLI's shape is the wrong kind of green.

| Test | Asserts |
|---|---|
| `test_summarize_is_a_pure_function_of_the_rows` | called with hand-built row dicts (no db, no fixture) it returns the envelope — the seam that keeps the math testable. |
| `test_the_action_distribution_counts_every_action_and_the_record_less_rows` | 2 dispatch / 1 ask / 3 nothing / 1 no-record → the four counters, and `checkins == 7`. |
| `test_the_ask_rate_is_raised_over_checkins` | 2 of 8 raised → `raised == 2`, `ask_rate == 0.25`; an empty window → `0.0`, never a ZeroDivisionError. |
| `test_the_considered_lengths_exclude_the_record_less_rows` | lists of length 0, 3, 5 plus one record-less row → `rows 3, empty 1, min 0, max 5, mean 2.667`, `no_record 1`. |
| `test_the_unavailable_frequencies_are_per_token` | `["gh"]`, `["gh", "claudron"]` → `{"gh": 2, "claudron": 1}`. |
| `test_dispatch_outcomes_count_join_rows_and_the_unjoined_decisions` | one completed + one open join row + one `dispatch` decision with none → `{"completed": 1, "open": 1, "unjoined": 1}` and `dispatches == 2`. |
| `test_a_non_dispatch_decision_with_no_join_row_is_not_unjoined` | an `ask` and a `nothing` row add nothing to `unjoined` (the negative half — without it the counter reads every quiet check-in as a lost dispatch). |
| `test_the_groups_are_by_project_key_with_null_last` | keys `shop`, `docs`, `None` → `[docs, shop, None]`, each group's `checkins` summing to the total. |
| `test_the_group_blocks_have_the_same_shape_as_totals` | `set(group) - {"project_key"} == set(totals)` — one block definition, so a group can never carry a field the totals lack. |
| `test_summary_refuses_last_and_limit` | `--summary --last` → 2; `--summary --limit 3` → 2; each message names the flag; stderr, nothing on stdout. |
| `test_summary_json_and_text_agree_on_the_counts` | the text form prints the same `checkins`/`raised`/outcome numbers the `--json` envelope carries (pinned by parsing them back out). |
| `test_summary_over_an_empty_window_answers_at_rc_0` | a plane that has seen the fleet but holds no decision → rc 0, `checkins: 0`, `projects: []`, and the text says so. |
| `test_summary_on_an_unreachable_plane_refuses_at_rc_3` | a bare root → 3, `UNREACHABLE` on stderr (the summary is a read, and a read that cannot read refuses). |

- [ ] **Step 2: Run them to verify they fail** — Run: `./.venv/bin/pytest tests/test_checkins_summary.py -q`
      Expected: `AttributeError: module … has no attribute 'summarize'`.

- [ ] **Step 3: Implement `summarize` + the `--summary` branch.** `summarize` builds one `_block()` for the
      totals and one per `project_key` group through the **same** helper (the shape test above is what stops
      them forking). `cmd_checkins` gains, before the plane is opened:

```python
    if getattr(args, "summary", False):
        if args.last:
            print("checkins: --summary and --last are exclusive — a summary of one row states"
                  " a window it did not read", file=sys.stderr)
            return 2
        if getattr(args, "limit", None):
            print("checkins: --summary and --limit are exclusive — a summary over a truncated"
                  " slice states a window it did not read", file=sys.stderr)
            return 2
```

and after `rows` are collected, a `--json` branch printing `summarize(rows)` merged with
`{"schema": 1, "fleet": …, "since": …, "scope": …}`, and a text branch printing the totals block then one
block per project, capped at `TEXT_ROW_LIMIT` projects with the same disclosure line.

Register the flags in `claudlobby/commands/_parsers.py`, in the `checkins` parser PR 1 added:

```python
    pck.add_argument("--summary", action="store_true",
                     help="roll the window up instead of listing it: actions, ask rate,"
                          " considered lengths, unavailable inputs, dispatch outcomes — by project")
    pck.add_argument("--limit", type=int, default=None,
                     help="at most N rows (applied after every filter; not with --summary)")
```

- [ ] **Step 4: Run the tests** — Run:
      `./.venv/bin/pytest tests/test_checkins_summary.py tests/test_checkins_cli.py tests/test_main.py -q`
      Expected: all pass (`test_main.py` covers the parser surface).

- [ ] **Step 5: Commit** — subject `feat(cli): checkins --summary — the window rolled up by project`.

---

### Task 4: The bounds — `--limit`, `--since` in SQL, the `subject_alias` bind

**Files:** modify `claudlobby/plane/queries.py`, `claudlobby/commands/checkins.py`; extend
`tests/test_checkins_cli.py`.

**Interfaces:** produces `checkin_rows_sql(*, since: bool = False, bot: bool = False, limit: bool = False) -> str`.
`CHECKIN_ROWS_SQL` **stays**, defined as `checkin_rows_sql()`, so any reference PR 1 left keeps working and the
no-bounds string is byte-identical to what PR 1 shipped (plus Task 1's `source_ref` column). Binds, in order:
fleet, fleet [, alias] [, since] [, limit] — `fleet_alias_range` binds **twice**, so the optional terms are
appended after it and one helper builds the SQL and the params together, in the same order, so they cannot
drift.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_checkins_cli.py`.

| Test | Asserts |
|---|---|
| `test_the_bot_filter_binds_in_sql_and_returns_what_python_returned` | the PR-1 Python filter's result set for `--bot mgr` over the seeded fixture equals the SQL-bound result set — the behaviour-unchanged pin for moving a filter into SQL. |
| `test_the_since_window_binds_in_sql` | the `24h` window still returns exactly the two recent rows, and `checkin_rows_sql(since=True)` contains `strftime('%s'` (the `_epoch` bind, never a lexical `<`). |
| `test_a_mixed_offset_instant_is_still_inside_the_window` | a decision stamped `-04:00` ten minutes ago is returned by `--since 1h`; under a lexical comparison it is not. |
| `test_limit_caps_both_surfaces` | 12 rows, `--limit 3` → 3 in `--json` and 3 in text, and the text scope line names the limit. |
| `test_limit_applies_after_the_raised_filter` | 6 rows of which 3 raised, `--raised --limit 2` → **2 raised rows**, not 2 rows of which some were dropped. (`--limit` is pushed into SQL only when `raised` is False; otherwise it is clamped in Python after filtering.) |
| `test_limit_zero_and_negative_are_usage_errors` | `--limit 0` and `--limit -1` → rc 2, named; `0` is not silently "no rows". |
| `test_last_still_ignores_the_window_with_the_sql_bind` | PR 1's `test_last_ignores_the_window` case re-asserted against the bound SQL: `--last` passes `since=False`, so a month-old row still returns. |
| `test_the_no_bounds_sql_is_the_pr1_string` | `checkin_rows_sql() == CHECKIN_ROWS_SQL` and neither contains `LIMIT` or a second `?` beyond the fleet pair. |

- [ ] **Step 2: Run them to verify they fail** — Run: `./.venv/bin/pytest tests/test_checkins_cli.py -q`
      Expected: failures naming `checkin_rows_sql`.

- [ ] **Step 3: Restructure the constant and bind the bounds.** Split PR 1's constant into
      `_CHECKIN_ROWS_HEAD` (SELECT + FROM + the two constant WHERE terms + the fleet range) and
      `_CHECKIN_ROWS_ORDER` (`ORDER BY <epoch> DESC, e.ingest_seq DESC`); `checkin_rows_sql` appends
      `" AND e.subject_alias = ?"` when `bot`, `f" AND {_epoch('e.occurred_at')} >= {_epoch('?')}"` when
      `since`, then the order clause, then `" LIMIT ?"` when `limit`. `CHECKIN_ROWS_SQL = checkin_rows_sql()`.

In `collect_checkins`: build the params in the same order; pass the alias as `f"bot:{fleet}/{bot}"` (exact and
case-sensitive — the same rule `fleet_alias_range`'s docstring records, and byte-equivalent to PR 1's
`_bot_of(...) != bot` given the fleet range already holds); push the limit only when `not raised`, and always
clamp `out` to `limit` in Python as well, so the two paths cannot disagree.

- [ ] **Step 4: Run the tests** — Run:
      `./.venv/bin/pytest tests/test_checkins_cli.py tests/test_checkins_outcome_join.py tests/test_checkins_summary.py -q`
      Expected: all pass. A failure in the outcome-join file here means the join lost rows to the new bound —
      the join must resolve **the rows served**, not the rows the window would have served.

- [ ] **Step 5: Commit** — subject `feat(cli): checkins — the window, the bot filter and --limit bound in SQL`.

---

### Task 5: Docs — `CLAUDE.md`, the observability guide, `CHANGELOG.md`

**Files:** `CLAUDE.md` (the `commands/` line at `:471`; `# Operations`),
`documentation/guides/observability.md` (question→door table), `CHANGELOG.md` (`[Unreleased]`).

**Interfaces:** consumes the landed surfaces of Tasks 1–4. Every count written here is measured in the same
breath as it is written.

- [ ] **Step 1: `CLAUDE.md`.** In the `commands/` line, extend PR 1's `checkins` clause — in house style, one
      sentence — to say that each decision carries its `checkin_dispatch` rows resolved through
      `TASK_STATUS_SQL` (the shipped constant appended to, never re-derived; a join row naming no assignment is
      `unjoined`, never `open`), that `--summary` rolls the window up by `project_key`, and that the file count
      is **unchanged** — PR 3 adds no `commands/` file, so do not touch `(15 files)`. Under `# Operations`,
      replace PR 1's command line with:

```bash
claudlobby checkins [--bot B] [--since 7d] [--last] [--raised] [--limit N] [--json]   # the manager check-in's decisions with their dispatch outcomes
claudlobby checkins --summary [--since 14d]                                           # the window rolled up by project: actions, ask rate, dispatch outcomes
```

- [ ] **Step 2: The observability guide.** **Replace** PR 1's interim row (the one whose command is a raw
      `sqlite3 state/plane/plane.db "SELECT COUNT(*) …"` over `checkin_dispatch`) with the door, and add the
      summary question:

```markdown
| Did that decision's dispatch actually land, and how did it end? | The plane (the `checkin_dispatch` join, resolved to the task's status) | `claudlobby checkins --bot <b> --last --json` |
| How is the check-in loop behaving over a window? | The plane (the decision rows, rolled up) | `claudlobby checkins --summary --since 14d` |
```

Verify the interim row is gone: `grep -c "sqlite3 state/plane/plane.db" documentation/guides/observability.md`
must not count a `checkin_dispatch` line.

- [ ] **Step 3: `CHANGELOG.md`** — under `[Unreleased]`, one bullet: the outcome join (through the plane's own
      tables), `--summary` by project, and the SQL-bound window / bot filter / `--limit`.

- [ ] **Step 4: Verify the docs gates** — Run:
      `./.venv/bin/pytest tests/test_readme_library_counts.py tests/test_boundary_invariants.py -q`
      Expected: pass. (No library member is added or removed by PR 3, so the README counts do not move; run it
      anyway to prove that rather than assume it.)

- [ ] **Step 5: Commit** — subject `docs: the check-in read door's outcome join and summary (chunk 3)`.

---

### Task 6: The gauntlet — mutants, the two-leg suite, the PR body

**Files:** none committed from step 1–2; `$OUT/` only.

**Interfaces:** reuses PR 1's evidence rig **by reference** — `$OUT/env.sh` (sourced first by every block,
because shell state does not survive between the executor's tool calls) and the `no_names` gate it defines
(refuses any file bound for a public body that carries a host identifier). Reuses PR 1's Task 7 step 2 mutant
driver verbatim, with a new `MUTANTS` list; the driver's own final assertion is the gate's verdict.

- [ ] **Step 1: The mutants.** Write PR 1's driver block to `$OUT/mut-ck3-defs.py` byte-for-byte (it is
      outside the repo, so nothing is committed), replacing only `MUTANTS`. Each entry is
      `(name, file, old, new, [killing tests])`; the anchor must occur **exactly once** in the file (the driver
      asserts it) and the tree must be clean and green first (it asserts that too).

| # | mutant | anchor → replacement | must be killed by |
|---|---|---|---|
| 1 | `outcome-blocked-as-failed` | `"returned_blocked": "blocked"` → `"returned_blocked": "failed"` | `test_the_buckets_map_the_plane_vocabulary` |
| 2 | `outcome-retired-as-failed` | `"superseded": "retired"` → `"superseded": "failed"` | `test_the_buckets_map_the_plane_vocabulary` |
| 3 | `outcome-drop-dispatch-failed` | the `"dispatch_failed": "failed",` line → `` (removed; it then falls through to `open`) | `test_the_buckets_map_the_plane_vocabulary` |
| 4 | `outcome-missing-assignment-as-open` | `return _OUTCOME.get(status, "open") if status else "unjoined"` → `return _OUTCOME.get(status, "open")` | `test_a_join_row_naming_no_assignment_is_unjoined_never_open` |
| 5 | `join-drops-the-json-guard` | `_detail_json("e.detail")` → `"e.detail"` (inside `checkin_dispatch_rows_sql`) | `test_a_truncated_join_row_does_not_take_out_the_query` |
| 6 | `join-ignores-the-fleet` | the `fleet_alias_range('e.subject_alias')` term → `"1=1"` | `test_another_fleets_join_row_never_attaches` |
| 7 | `join-per-row` | `_join_dispatches(conn, fleet, [x["checkin_ref"] for x in out])` → a per-row call inside the loop | `test_the_join_is_two_queries_not_one_per_row` |
| 8 | `summary-unjoined-off` | the `action == "dispatch" and not dispatches` branch's `+= 1` → `+= 0` | `test_dispatch_outcomes_count_join_rows_and_the_unjoined_decisions` |
| 9 | `summary-unjoined-on-every-quiet-row` | that branch's `r["action"] == "dispatch"` → `True` | `test_a_non_dispatch_decision_with_no_join_row_is_not_unjoined` |
| 10 | `summary-considered-over-all-rows` | the record-less exclusion in the `considered` accumulator → include them | `test_the_considered_lengths_exclude_the_record_less_rows` |
| 11 | `summary-null-project-dropped` | the null-group append → skipped | `test_the_groups_are_by_project_key_with_null_last` |
| 12 | `limit-before-the-raised-filter` | push the SQL `LIMIT` even when `raised` | `test_limit_applies_after_the_raised_filter` |
| 13 | `since-unbound` | the `since=` argument at the `checkin_rows_sql` call site → `since=False` | `test_the_since_window_binds_in_sql` |
| 14 | `bot-bind-as-prefix` | `" AND e.subject_alias = ?"` → `" AND e.subject_alias LIKE ? || '%'"` | `test_the_bot_filter_binds_in_sql_and_returns_what_python_returned` (seed `mgr` and `mgr2`) |

Run (unsandboxed, never through a pipe):

```bash
. "$HOME/Projects/claudlobby-worktrees/ck3-out/env.sh"; cd "$WT"
./.venv/bin/python "$OUT/mut-ck3-defs.py" > "$OUT/mutants.md" 2> "$OUT/mut-progress.txt"; echo "rc=$?"
head -1 "$OUT/mut-progress.txt"; cat "$OUT/mutants.md"
```
Expected: `rc=0`, a first progress line `green: the N killing files pass on the committed tip`, and a 14-row
table with every result `killed`. A `SURVIVED` row is a missing test — add the test, never a weaker mutant. An
`INVALID RUN` is not evidence.

- [ ] **Step 2: The two-leg full-suite gate (names + counts + the rc gate).** `before.txt` is this run's Task 0
      leg (the tip PR 3 branches from); the after leg runs on the FINAL committed tip, unsandboxed:

```bash
. "$HOME/Projects/claudlobby-worktrees/ck3-out/env.sh"
./.venv/bin/pytest --tb=no -ra > "$OUT/run_after.txt" 2>&1; echo "rc=$?"
awk "/short test summary info/,0" "$OUT/run_after.txt" | grep -E "^(FAILED|ERROR)" | sed 's/ - .*//' | sort -u > "$OUT/after.txt"
comm -13 "$OUT/before.txt" "$OUT/after.txt"
tail -1 "$OUT/run_before.txt"; tail -1 "$OUT/run_after.txt"
./.venv/bin/pytest --collect-only -q tests/test_checkins_outcome_join.py tests/test_checkins_summary.py > "$OUT/collect-new.txt"; echo "rc=$?"; tail -1 "$OUT/collect-new.txt"
./.venv/bin/pytest --collect-only -q tests/test_checkins_cli.py > "$OUT/collect-cli.txt"; echo "rc=$?"; tail -1 "$OUT/collect-cli.txt"
```
Expected: both suite rc **1** (the baseline is red, so rc 1 is the normal state of both legs — the rc gate only
ever catches rc 2 / 4 / 5 / 127, a run that did not complete); `comm -13` **empty**; and the after leg's
`passed` equal to the before leg's plus the two new files' collected count plus the cases Task 4 appended to
`test_checkins_cli.py`. **A count change with an empty name diff is evidence the names mechanism is broken, not
a clean run** (#1012) — re-run the two collect lines before believing either number.

- [ ] **Step 3: Assemble the PR body from the evidence files, behind the identifier gate.** Same shape as PR 1
      Task 7 steps 3b–5: build `$OUT/pr-body.md` by `cat`-ing a heredoc header, then the measured blocks —
      the `comm -13` result and both count lines, then `$OUT/mutants.md` verbatim — then run
      `no_names "$OUT/pr-body.md"` **in the same block** and only then publish. The body states:

  - what landed, per task, in one line each; the two-leg gate (introduced failures, before/after counts); the
    14-row mutant table as the driver printed it;
  - **rollout posture:** `claudlobby/` only — no `lib/` script, no composed artifact, no hook, no `bot.conf`
    key. The door is a read; nothing a bot executes changes, so there is nothing to canary and no restart;
  - the standing note that no A/B verdict gates any PR in this series (the operator's 2026-09-16 ruling), and
    that this door **produces facts and no verdict** — the closure arithmetic the spec's §12.4 describes is
    chunk 4's, over these fields.

```bash
. "$HOME/Projects/claudlobby-worktrees/ck3-out/env.sh"
no_names "$OUT/pr-body.md" && gh pr create --fill-first --body-file "$OUT/pr-body.md"
```
Expected: `identifiers in pr-body.md: clean`, then the PR URL. A `STOP:` line means a host identifier reached
the body — **fix the producing step, never the body**.

- [ ] **Step 4: Squash-merge** with the same body assembled the same way, behind the same gate.

---

## Self-review

The one place this plan could be wrong in a way its own tests would not catch is the bucket map: it is the PR's
only judgment, and `test_every_terminal_task_event_has_a_bucket` bounds it against *the plane's* vocabulary
only — it cannot tell you `expired` belongs with `failed` rather than `retired`, and a reader who disagrees
should change the map and its parametrized test together, never add a second mapping downstream. Two spec
ambiguities were resolved rather than deferred, each visible in the test that pins it. §12.4 calls a null
`task_id` "unjoined … can never resolve", but that was written against the legacy ledger where the task id
*was* the address; the plane's join row carries the `assignment_id` too, so an id-less dispatch resolves and is
reported as `task_id: null` with a real status — `unjoined` is kept for the two cases where nothing resolves
(no join row at all, or one naming an assignment the plane does not hold), and the raw `task_id` stays in the
envelope so chunk 4 can still exclude id-less rows from a denominator if its bar wants to. And §12.4 names only
`blocked`/`failed` as the terminal-not-completed classes, leaving `cancelled`/`superseded`/`reassigned`
unnamed, so they get a sixth bucket and no interpretation. Everything else is mechanical: two queries instead
of N, a shipped constant appended to instead of copied, a `json_valid` guard whose absence was measured (not
argued) to take out the whole query, and a `--summary` that is a pure function of rows — which is what makes
its counts mutable, and therefore gatable.
