# Fleet-ops command reference

Reference that the fleet-ops guide (`SKILL.md`) points to: command forms, flags, what each command does and what each read prints. The guide keeps every rule; this file adds none.

## Request IDs

On the guide's request-UUID command (the fenced `python3 -I` line):
It needs only `python3`, on Linux and macOS alike, and prints the
lowercase form the CLI requires. `-I` keeps the current directory off the import
path and ignores `PYTHON*` variables, so a `uuid.py` in a checkout under review
cannot run in place of the standard library's.

## Reads

Use `claudlobby --json library list` to discover available skills, guardrails,
expertise and other composition components.

```bash
claudlobby --json brief
claudlobby --json fleet inbox
claudlobby --json task list
claudlobby --json task show TASK_ID
claudlobby --json task reviews OWNER/REPO --pr N
claudlobby --json assignment show ASSIGNMENT_ID
claudlobby --json workstream list
```

On the brief's `work`:
Each item names its task's workstream (`workstream_id`, null for a task in no
workstream), and the text view groups the open tasks by workstream.

On the brief's `reports`:
Unacknowledged report rows (`reports.unacked`) are in the fleet manager's view only, the
oldest 50; any other view carries their number (`reports.count`) and the command that
lists them (`reports.list_command`).

## Session and supervision reads

For current session and supervision evidence, use `claudlobby --json fleet status`
or `claudlobby --json bot status BOT`. Each bot's `context` there is the input,
cache-read and cache-write tokens of its newest main-chain call, with that
call's time, read from its transcript; `tokens` is null with a `reason`, never 0,
when the transcript cannot answer, and `compacted_after` names a compaction
written since that call. `claudlobby --json bot session BOT` reads
the selected private session and native enrollment; it does not inspect another
process by name. Use `claudlobby --json bot logs BOT` or the manager's
`claudlobby --json fleet logs` for bounded file tails.

`claudlobby --json fleet utilization` reports busy share and observed seconds from
Plane heartbeat samples alongside canonical task assignments.

## Usage reads

When token counts are needed, run `claudlobby --json bot usage BOT --since 24h`,
`claudlobby --json fleet usage --since 24h`, or
`claudlobby --json brief --usage-since 24h` for the brief viewer's concise
summary. These on-demand reads cover only configured accounts' current bot
directory transcripts in the selected window.
`claudlobby --json bot usage BOT --calls N` also lists the newest N main-chain calls of the bot's newest own session in the window (20 when N is left out, at most 100), each with its context, its growth over the previous call and the rows stored before it; `ended` says where the read stopped.

## Bot lifecycle

```bash
claudlobby --json bot start WORKER
claudlobby --json bot restart WORKER
claudlobby --json bot restart WORKER --fresh
claudlobby --json bot stop WORKER
claudlobby --json bot stop WORKER --reason "parked until the review lands"
```

`start` is idempotent when the selected bot's session is already ready; use
`restart` for an intentional bounce: it waits for an idle prompt, continues the
same conversation and ends with a fresh bridge/session readiness check.
`stop` de-enrolls supervision and stops the session, so keepalive cannot revive
it; a later `start` is explicit re-enrollment.

For a slow bridge, `bot restart WORKER --ceiling SECONDS` overrides the
per-bot readiness budget with a positive number.

## Workstreams

Flags of the `workstream` verbs:
`block ID --on
human:NAME --note TEXT` records a declared wait; `unblock ID --note TEXT`
restores that same ID to active. `open --id` refuses the `ws_<32 hex>` form,
which only a task's own workstream takes, and those do not count toward the
active cap. `close ID [--status done|abandoned]` refuses while a task in the
workstream holds a live assignment, or is one the task reducer cannot resolve,
and names each: end it first. Otherwise the close withdraws the workstream's
queued tasks in its own batch, one row each naming the close, and prints them.
`close ID --dry-run` records nothing and prints the refusal or the tasks.

## Task lifecycle

```bash
claudlobby --json task admit --title "Concrete outcome" --request-id ADMIT_UUID
claudlobby --json task admit --title "Its follow-up" --workstream WORKSTREAM_ID --request-id ADMIT_UUID
claudlobby --json task show TASK_ID
```

Without `--workstream`, admit records the task's own workstream in the same
batch: `ws_` and the task ID's 32 hex digits, titled like the task and owned by
the fleet's manager. With it, the task joins that workstream, and admit refuses
one that is archived, closed by the manager, or closed because every task in it
has ended or moved out. So file a related follow-up with the first task's
`data.workstream_id` before that task closes. `task admit`, `assign`, `reassign`,
`withdraw`, `move`, `escalate` and `nudge`, and `assignment accept`, `reschedule`
and each report, print the task's `workstream_id`.

```bash
claudlobby --json task move TASK_ID --workstream WORKSTREAM_ID --reason TEXT --request-id MOVE_UUID
```

The manager, or a local person, moves open work into another open workstream of
its fleet and project. One `task_moved` row names the task, the workstream it
left, the one it joined, who moved it and why, and every read takes the task's
workstream from its newest move. It refuses a target that is archived or closed.
A workstream whose last open task moves out reads closed: `done` if a task it
still holds completed, else `abandoned`. Nothing can join it after that, the
moved task included, so moving the task back cannot undo a mistaken move.

```bash
claudlobby --json task assign TASK_ID --bot WORKER --request-id ASSIGN_UUID
claudlobby --json assignment show ASSIGNMENT_ID
claudlobby --json assignment deliver ASSIGNMENT_ID --file FILE --request-id DELIVER_UUID
```

```bash
claudlobby --json assignment accept ASSIGNMENT_ID --request-id ACCEPT_UUID
claudlobby --json assignment progress ASSIGNMENT_ID --summary "Started the review" --request-id PROGRESS_UUID
claudlobby --json assignment complete ASSIGNMENT_ID --summary "Reviewed the change" --request-id COMPLETE_UUID
claudlobby --json assignment show ASSIGNMENT_ID
```

## Deadline watch

On the guide's deadline watch (`work.items[].attention` in the brief):

| `status` | What it tells you | What changes it |
| --- | --- | --- |
| `not_due` | Before its deadline, or open-ended. | The deadline passes. |
| `overdue` | Past its deadline, no progress since its dispatch in the last 45 minutes, and your session has run since the dispatch. | You record progress, or the assignment ends. |
| `orphaned` (`respawned_after_dispatch`) | The same, but your session started after the dispatch, so the session that received it has ended. The assignment is still open and still yours. | You record progress, or the assignment ends. |
| `past_due` (`progress_grace`) | Past its deadline, held back by a progress report made since its dispatch and under 45 minutes old. | 45 minutes after that report it reads `overdue` or `orphaned` again; a newer report holds it back again. |
| `past_due` (`max_age_cap`) | More than 24 hours after the dispatch or, once the manager has moved the deadline, after the move or the new deadline, whichever is later: aged out, no longer flagged, and not resolved. | The assignment ends. |
| `unknown` | The watch could not read some of its evidence (the brief's `DEGRADED` section says which). It says nothing about the assignment. | The evidence becomes readable. |

## Reports

```bash
claudlobby --json fleet reports submit --status completed --summary "Completed the review" --request-id REPORT_UUID
claudlobby --json request show REPORT_UUID
```

## Messages

```bash
claudlobby --json message send --to WORKER --text "A concrete question" --request-id UUID
claudlobby --json request show UUID
claudlobby --json message show MESSAGE_ID
claudlobby --json message receipt MESSAGE_ID
```

```bash
claudlobby --json message reply MESSAGE_ID --text "The requested answer" --request-id REPLY_UUID
claudlobby --json request show REPLY_UUID
```

## Orientation reads

`bot show BOT` answers what a bot has at hand: its role and manager and what is
composed into it (`data.bot.effective_configuration.equipment`: skills, MCP
servers, protocols, guardrails, principles and the rest). Its
`effective_configuration.precompact` says which `PreCompact` hooks the compose gives
the bot: `claudron_session_loop` (the composer's own test, in `bot.conf` as
`CLAUDRON_SESSION_LOOP`), `claudna_precompact_reflect`, and the composed settings'
`PreCompact` commands, each named `claudron` or `other` and identified by SHA-256,
with `matches_declaration` false where the settings are older than the declaration. A
plugin's hooks and the account's own settings are not listed. `bot capabilities BOT`
adds its permission rules as SHA-256 hashes, the arguments withheld, and is about
four times as long; read it to compare rule sets, not to learn the equipment.

`project list` and
`project show PROJECT` read the fleet's declared projects: key, title, repos,
validation tier (how work in them is closed) and mission file. A manager's
CLAUDE.md carries that table; a worker reads it here.
