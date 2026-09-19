---
name: checkin
description: "The idle-manager check-in: read the SSOT (the plane through checkins, brief and status, Claudron, the mission with each project's tier and repos, the GitHub backlog), decide ONE project and ONE action, record the decision BEFORE acting, and let the surfacing judgment decide whether the operator hears anything at all. Silence is the default."
argument-hint: "[--dry-run]"
tool_grants:
  - "Bash(claudlobby --fleet * checkins *)"
  - "Bash(claudlobby --fleet * brief *)"
  - "Bash(claudlobby --fleet * status *)"
  - "Bash(claudron lookup *)"
  - "Bash(gh issue list *)"
  - "Bash(*checkin-record.sh*)"
  - "Bash(*dispatch-task.sh*)"
  - "Bash(*tg-post.sh*)"
  - "mcp__plugin_telegram_telegram__reply"
---

# Check-in

Your own re-engagement cycle. Nobody is watching it; what they may see is only what
the surfacing judgment (DECIDE, below) lets through. **Every read goes through a
named door and every write through a named door** — never a hand-rolled query,
never a hand-built plane envelope, never a pipeline. That coupling is what makes
your reasoning inspectable (the `checkins` read door) and the edges deterministic.
The fleet is named on every door because a fleet-less call runs the CLI in root
mode, which an overlay install does not have — and naming it is never a problem
on a root-mode install either: `--fleet` matching that install's own
`fleet.name` resolves to root mode too, instead of refusing.

`$BOT_ID`, `$FLEET_NAME` and `$CLAUDLOBBY_ROOT` come from your `bot.conf`; each
project's key, repos and tier come from the `## Projects` table in your own
CLAUDE.md, which is in your context before this skill runs. If no
`## Projects` table is composed into your CLAUDE.md, this fleet has no
`projects.yaml`: `dispatch` is not available to you (it needs `--project`), and the
check-in ends in `ask` or `nothing` — say so in the rationale.

## Arguments

Parse `$ARGUMENTS`:
- `--dry-run`: do every READ and the DECIDE, then validate the decision through the
  door's own dry run (the RECORD here-doc with `--dry-run`, which prints
  `DRY-RUN <ck_id>` and records nothing), and print, without running it, the
  single-call ACT line you would have run. Act on nothing.

## READ — in order, all cheap, all SSOT

A step that fails is **recorded, never guessed around**: add its name to
`inputs_seen.unavailable` and continue. A count you could not measure is `null`
(**could not measure**), never `0` — in `inputs_seen` and in `delta` alike.

0. **The previous check-in, and this week's asks** —
   `claudlobby --fleet "$FLEET_NAME" checkins --bot $BOT_ID --last --json` (the row
   is `checkins[0]`; its `record.inputs_seen` is *the state at the last check-in*;
   keep its `checkin_id` for `prev_checkin_id`; an empty `checkins` → `null`) and
   `claudlobby --fleet "$FLEET_NAME" checkins --bot $BOT_ID --since 7d --raised
   --json` (the `checkins[]` rows are the asks already raised this week; count them
   — `--raised` keeps the read to those rows). rc 3 means the plane is unreachable —
   record `checkins` as unavailable and `prev_checkin_id` as `null`; the record
   shows both, so a skipped read never poses as a first one.
1. **The fleet's present** — `claudlobby --fleet "$FLEET_NAME" brief --bot $BOT_ID
   --json`: `dispatches` (`open` / `overdue` / `orphaned` / `dispatched`, each row
   with `escalated`, `nudged`, `last_progress_at`), `workstreams` (`active`,
   `stalled`), `reports.unacked`, `alerts` (last 24h critical), `mission`. Read
   `degraded[]` **by mode, for the fields you use**: an entry whose `mode` is
   `omitted` and whose `field` is `dispatches`, `workstreams`, `reports` or `alerts`
   (or a dotted child, such as `dispatches.open`) makes that section **unavailable** —
   never zero. An entry whose `mode` is `labeled` means the field is present and
   bounded — a real fleet's brief always carries `alerts` labeled, and usually
   `dispatches.orphaned` — so use the field and note the bound. The standing
   `utilization` entry (#891) is **not an input** of this skill; ignore it.
1b. **The roster, and who is idle** — `claudlobby --fleet "$FLEET_NAME" status
   --json`: `bots[]`, each with `name` (the id the ACT line takes as `<worker>`),
   `state`, `pane_state` (`BUSY` / `IDLE`), `tmux_alive`, `current_task`, and
   `plane_unreachable` (non-null when the plane could not be read for that bot). A
   `null` `pane_state` — with or without `plane_unreachable` set (a fleet with no
   heartbeats yet) — means the worker is UNOBSERVED, not idle. `dispatch` needs an
   alive, observed, idle worker; the rationale names the worker and its observed
   `pane_state`. (This third door exists because `brief` carries no per-bot pane state
   — it answers what is open, not who is idle; add no fourth.)
2. **Knowledge** — `claudron lookup --limit 5 <project>` for each project with open
   work. Count the hits (`knowledge_hits`); read what is relevant.
3. **The goal and each project's rigor** — both are already in your context, no
   call needed: the `## Fleet Mission` section of your own CLAUDE.md (the charter is
   composed in for a manager) and the `## Projects` table (Project · Title · Repos ·
   Tier · Mission, composed from `projects.yaml`): the Tier is how that project's
   work CLOSES, the Repos are what step 4 reads. If there is no `## Projects` table,
   this fleet has no `projects.yaml` and `dispatch` is not available to you (it needs
   `--project`). **If there is no `## Fleet Mission` section and no mission line,
   record it**: put `mission` in `inputs_seen.unavailable`, set `issues_considered`
   to `null` (a count filtered by a mission you could not read is fabricated), and
   say so in the rationale — a decision taken against no charter must not look like
   one taken against a real one.
4. **The external backlog** — per repo in the `## Projects` table, one line:
   `gh issue list --repo <owner/name> --state open --limit 50 --json number,title,labels,updatedAt`.
   Count every open issue returned as `issues_seen` (the raw number, BEFORE any
   filter), then filter to mission-aligned items, group by project, and count those
   as `issues_considered`. The two together tell a broken filter from an empty
   backlog (`issues_seen` is capped at the `--limit`, so it discriminates 0 from
   non-zero, not 50 from 3,000); a read that failed goes into `unavailable`, which
   is what tells a broken query from an empty backlog — write both list keys on
   every record, empty when empty, never omitted.
5. **The delta** — now versus step 0, per project: tasks opened / completed /
   stalled / cleared, new issues, new messages, held items still pending. **The
   delta is the primary signal** for both the action and the surfacing judgment: an
   unchanged world argues for `nothing` and silence.

**Everything above is read per project.** The check-in is a portfolio decision.

## DECIDE — one project, then exactly one action

**The allocation rule.** Attention is a weighting, never a ranking: every project
with open work or a blocker is checked on every check-in (the floor); *new* effort
— picking backlog issues — goes preferentially to projects with recent attention,
never exclusively (the tilt); no attention + no open work = left alone — a dormant
project is revived only by explicit direction (never stalest-first). Mission
alignment and backlog depth weigh in. **Record the losers:** every candidate you
weighed and passed over goes into `inputs_seen.considered` as one line, `<candidate>
— <why not>` (at most ten) — a decision is judged by what it did not pick, and a
`dispatch` with an empty `considered` is refused by the contract.

| action | when | through |
|---|---|---|
| **dispatch** | an open or backlog item fits an idle worker (step 1b); you choose the worker and the rationale says why | ONE Bash call — the RECORD here-doc with the dispatch appended by `&&` (the block under RECORD before ACT): `bash "$CLAUDLOBBY_ROOT/lib/dispatch-task.sh" --project <key> [--repo <owner/name>] [--ref <issue-url>] --checkin "$ck" <worker> "<task>"` — `--project` is the well-defined bar (a projects.yaml key); `--checkin` joins the dispatch to this decision; `$ck` is the id the record door just printed, captured in the same call |
| **ask** | the surfacing judgment (below) concludes the operator should hear something — a fork only they can resolve, or the backlog holds nothing worth starting ("ask for tasks") | one Telegram post in the shape chunk 2's protocol will fix (one line, one ask with named options, one pointer): the reply tool when this check-in arrived on Telegram; `bash "$CLAUDLOBBY_ROOT/lib/tg-post.sh" "<the post>"` when it was injected into your pane (there is no chat to reply to). Either way it is recorded as your communication |
| **nothing** | all work in flight, nothing worthwhile — **recorded**, so "checked and chose nothing" is a fact, not silence | — |

**The surfacing judgment.** Its default answer is **no**. Weigh, at minimum: does
this genuinely need a human (a `requires-approval` boundary in the mission, a tier
that mandates sign-off, conflicting priorities)? · would the operator want to know (a
deliverable ready, a blocker that stalls the fleet, a failure with cost)? · what
changed since they were last told (the delta against step 0 and the last post's
`held`)? · has enough accumulated to be worth one message? · have two asks already
been raised this week (step 0 — then proceed on your best tier-gated judgment or
wait quietly, never a third)? · what Claudron says about how the operator wants to
be engaged · the urgency floor (a `blocked` that stalls everything breaks through
regardless). Record the judgment in `raise`: `decided`, `reason` (always, in both
directions), and what you `held`.

**Degraded inputs, per input.** The plane unreachable (`checkins` rc 3, or
`dispatches`/`workstreams`/`reports`/`alerts` **omitted** in `brief`, or `status`
failing) narrows the actions to `ask | nothing` — never dispatch blind. `gh` or
`claudron` unavailable narrows only the **source** of new work: you may still
dispatch open, **plane-known** tasks; you may not pick fresh backlog issues you
could not read. Record what was unavailable either way.

## RECORD before ACT

Build the decision as JSON (schema 1) and record it FIRST — the decision exists even
if the action then fails. The door is invoked directly with a here-doc (never
through `cat |`). For `ask` and `nothing` the record is the door call alone (the
first form below: the door prints the id; you do not need it — READ 0 finds it next
time); for `dispatch` the SAME call carries the act after `&&` (the second form),
so the id never crosses a tool boundary and a refused or unrecorded decision (rc 2 /
rc 3) skips the act by construction. **Nothing else rides either call** — no `echo`,
no `printf`: a builtin is an ungranted subcommand in this session. Two shapes are
coupled: **`ask` requires `raise.decided: true`**,
and **`dispatch` requires `project_key: "<slug>"` and a non-empty `considered`**.
Every `N|null` below is an integer or `null`, never omitted — in both blocks. The
rationale names the chosen project and its tier (the `## Projects` table): the record
must show the rigor bar was weighed, not only what was picked.

For `ask` or `nothing`:

```bash
bash "$CLAUDLOBBY_ROOT/lib/checkin-record.sh" <<'EOF'
{ ...the decision JSON below... }
EOF
```

For `dispatch`, the record and the act as ONE call:

```bash
ck=$(bash "$CLAUDLOBBY_ROOT/lib/checkin-record.sh" <<'EOF'
{"prev_checkin_id": <"ck_…" from step 0, or null>,
 "inputs_seen": {"open_tasks": N|null, "stalls": N|null, "unacked": N|null,
                 "issues_seen": N|null, "issues_considered": N|null, "knowledge_hits": N|null,
                 "considered": ["<candidate> — <why not>"], "unavailable": []},
 "delta": {"tasks_opened": N|null, "tasks_completed": N|null, "stalls_appeared": N|null,
           "stalls_cleared": N|null, "issues_new": N|null, "messages_new": N|null, "held_pending": N|null},
 "action": "dispatch|ask|nothing",
 "project_key": <"<slug>" for dispatch (or the project an ask is about), else null>,
 "rationale": "<your words, <= 600 chars: the weighing, the project and its tier, the worker and its observed state, the why>",
 "raise": {"decided": <true for ask, else false>, "reason": "<why it surfaced, or why not, <= 600>", "held": []}}
EOF
) && bash "$CLAUDLOBBY_ROOT/lib/dispatch-task.sh" --project <key> --checkin "$ck" <worker> "<task>"
```

The `&&` is RECORD-before-ACT made mechanical: the door prints the `checkin_id` alone
on success, `$ck` carries it into the dispatch in the same call (a shell variable
does not survive between your tool calls, which is why the two are never split), and
rc 2 or rc 3 from the door skips the act. If the dispatch door says the plane cannot
see that id, verify with `claudlobby --fleet "$FLEET_NAME" checkins --bot $BOT_ID
--last --json` before anything else. rc 2: the decision was refused — every reason is
on stderr;
fix and re-record **once**; if the second attempt is refused too, record the minimal
valid `nothing` row and stop — minimal means EVERY required key, so it cannot be
refused a third time: `prev_checkin_id` from step 0 (or `null`), every count `null`,
`considered` `[]`, `unavailable` listing the reads that failed, `action: "nothing"`,
`project_key: null`, a `rationale` quoting the refusal reasons, and `raise` with
`decided: false`, a non-empty `reason` ("refused twice; nothing surfaced") and
`held: []` — the record's existence is the point, a
turn that ends with no row is the one failure the loop must not produce. rc 3: the
plane did not record it — do **not** act on an unrecorded `dispatch` (the `&&` has
already skipped it); say so in your next justified post. For `ask`, ACT through the
door in the table once the record has printed its id. **If the ACT door fails** (a nonzero rc from `dispatch-task.sh` or the post),
record a **follow-up check-in** at once — `prev_checkin_id` = the id just printed,
`action: nothing`, the rationale naming the failure — and never retry a dispatch
blind; the pair is what the outcome join will show.

Under `--dry-run` the same here-doc goes to the door's dry run, which validates and
prints `DRY-RUN <ck_id>` (the id is the SECOND word; a real run prints the id
alone), in the composed shape with a harmless granted read standing where the
dispatch would go — so the shape itself passes through the permission layer before
any real run relies on it:

```bash
ck=$(bash "$CLAUDLOBBY_ROOT/lib/checkin-record.sh" --dry-run <<'EOF'
{ ...the same decision JSON... }
EOF
) && claudlobby --fleet "$FLEET_NAME" checkins --bot $BOT_ID --last --json
```

Then print, in your reply and not as a command, the ACT line you would have run.

## Not in this chunk

`propose` (generating new work into an intake store, gated by a project's
`planning.initiative`) and `sprint` (batch execution) arrive with later chunks and
widen the `action` enum then. Until they do, "the backlog holds nothing worth
starting" is an `ask`, not an invention.

## Rules

- One project, one action, one record per check-in. Never two actions.
- Never freelance a read (no ad-hoc SQL, no reading state files) or a write (no
  hand-built plane envelopes): the doors above are the whole contract.
- Never restate a project's tier in a post; point to it.
- `--dry-run` never records and never acts.
