# Fleet-ops command reference

Reference that the fleet-ops guide (`SKILL.md`) points to: command forms, flags and what each read prints. The guide keeps every rule; this file adds none.

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

## Bot lifecycle

```bash
claudlobby --json bot start WORKER
claudlobby --json bot restart WORKER
claudlobby --json bot restart WORKER --fresh
claudlobby --json bot stop WORKER
claudlobby --json bot stop WORKER --reason "parked until the review lands"
```

For a slow bridge, `bot restart WORKER --ceiling SECONDS` overrides the
per-bot readiness budget with a positive number.

## Workstreams

Flags of the `workstream` verbs:
`block ID --on
human:NAME --note TEXT` records a declared wait; `unblock ID --note TEXT`
restores that same ID to active.

## Task lifecycle

```bash
claudlobby --json task admit --title "Concrete outcome" --request-id ADMIT_UUID
claudlobby --json task show TASK_ID
```

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
servers, protocols, guardrails, principles and the rest). `bot capabilities BOT`
adds its permission rules as SHA-256 hashes, the arguments withheld, and is about
four times as long; read it to compare rule sets, not to learn the equipment.
