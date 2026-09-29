---
name: fleet-ops
description: "Read fleet work, admit tasks, deliver and route assignments as manager, and accept or report assigned work as worker. Use the canonical claudlobby CLI and verify recorded results."
tool_grants:
  - "Bash(claudlobby --help)"
  - "Bash(claudlobby brief --help)"
  - "Bash(claudlobby task list --help)"
  - "Bash(claudlobby task show --help)"
  - "Bash(claudlobby task reviews --help)"
  - "Bash(claudlobby task admit --help)"
  - "Bash(claudlobby task assign --help)"
  - "Bash(claudlobby task withdraw --help)"
  - "Bash(claudlobby task reassign --help)"
  - "Bash(claudlobby task escalate --help)"
  - "Bash(claudlobby task nudge --help)"
  - "Bash(claudlobby workstream --help)"
  - "Bash(claudlobby bot usage --help)"
  - "Bash(claudlobby bot start --help)"
  - "Bash(claudlobby bot stop --help)"
  - "Bash(claudlobby bot restart --help)"
  - "Bash(claudlobby bot automation --help)"
  - "Bash(claudlobby fleet usage --help)"
  - "Bash(claudlobby assignment show --help)"
  - "Bash(claudlobby assignment accept --help)"
  - "Bash(claudlobby assignment deliver --help)"
  - "Bash(claudlobby assignment progress --help)"
  - "Bash(claudlobby assignment block --help)"
  - "Bash(claudlobby assignment return --help)"
  - "Bash(claudlobby assignment complete --help)"
  - "Bash(claudlobby assignment fail --help)"
  - "Bash(claudlobby message show --help)"
  - "Bash(claudlobby message receipt --help)"
  - "Bash(claudlobby message wait --help)"
  - "Bash(claudlobby message send --help)"
  - "Bash(claudlobby message reply --help)"
  - "Bash(claudlobby fleet reports submit --help)"
  - "Bash(claudlobby fleet reports list --help)"
  - "Bash(claudlobby fleet reports ack --help)"
  - "Bash(claudlobby fleet inbox --help)"
  - "Bash(claudlobby request show --help)"
  - "Bash(claudlobby --json context show)"
  - "Bash(claudlobby --json brief)"
  - "Bash(claudlobby --json brief *)"
  - "Bash(claudlobby --json task list)"
  - "Bash(claudlobby --json task list *)"
  - "Bash(claudlobby --json task show *)"
  - "Bash(claudlobby --json task reviews *)"
  - "Bash(claudlobby --json assignment show *)"
  - "Bash(claudlobby --json message show *)"
  - "Bash(claudlobby --json message receipt *)"
  - "Bash(claudlobby --json message wait *)"
  - "Bash(claudlobby --json message send *)"
  - "Bash(claudlobby --json message reply *)"
  - "Bash(claudlobby --json fleet reports submit *)"
  - "Bash(claudlobby --json fleet reports list)"
  - "Bash(claudlobby --json fleet reports list *)"
  - "Bash(claudlobby --json fleet reports ack *)"
  - "Bash(claudlobby --json fleet inbox)"
  - "Bash(claudlobby --json fleet inbox *)"
  - "Bash(claudlobby --json request show *)"
  - "Bash(claudlobby --json workstream list)"
  - "Bash(claudlobby --json workstream show *)"
  - "Bash(claudlobby --json bot usage *)"
  - "Bash(claudlobby --json bot automation status *)"
  - "Bash(claudlobby --json bot automation pause *)"
  - "Bash(claudlobby --json bot automation resume *)"
  - "Bash(claudlobby --json bot automation record *)"
  - "Bash(claudlobby --json fleet usage)"
  - "Bash(claudlobby --json fleet usage *)"
---

# Fleet operations

Use this guide for task, assignment, message, and request reads; the admitted
task and assignment lifecycle; and ordinary message send and reply. The
generated bot context selects your fleet and identity; check it with `claudlobby --json
context show`. Discover exact flags with `claudlobby --help` and `claudlobby
<group> <verb> --help`.

Read before acting:

```bash
claudlobby --json brief
claudlobby --json fleet inbox
claudlobby --json task list
claudlobby --json task show TASK_ID
claudlobby --json task reviews OWNER/REPO --pr N
claudlobby --json assignment show ASSIGNMENT_ID
claudlobby --json workstream list
```

The brief defaults to your bot and combines mission, open work, workstreams,
reports and recent alerts. Its `data.brief.work` uses canonical task IDs; inspect
`data.brief.degraded` and `data.brief.work.issues` before treating an empty view as clear.
`--bot BOT` changes only the view. It does not change your caller identity.
When token counts are needed, run `claudlobby --json bot usage BOT --since 24h`,
`claudlobby --json fleet usage --since 24h`, or
`claudlobby --json brief --usage-since 24h` for the brief viewer's concise
summary. These on-demand reads cover only configured accounts' current bot
directory transcripts in the selected window. Inspect `coverage` before using
counts; quota and reset time are unavailable without a provider observation.
Ordinary and boot briefs do not scan transcripts.

For an equipped autonomous runner, `claudlobby --json bot automation status BOT`
reports whether the next tick is eligible. A missing or ambiguously attributed
state row is unknown and ineligible. The bot can pause or resume itself; the
current manager can pause or resume a selected-fleet bot. A generated bot may
record only its own run; a trusted local operator remains an explicit repair
caller. Each mutation needs a retained `--request-id UUID`; use
`--reason TEXT` for pause and `--outcome completed|bypassed|needs-input|blocked|partial`
for record, with `--pr URL` or `--issue URL` only for validated target-repo links.
These commands preserve the host-shared state under its lock; they do not
deliver a report or Telegram message.

Only the selected fleet manager may operate another declared bot's supervised
session. Any bot may request its own restart after saving a fresh handoff;
use `/restart` for that sequence. Use `claudlobby bot start --help`, `claudlobby bot stop --help`, or
`claudlobby bot restart --help` for the exact syntax:

```bash
claudlobby --json bot start WORKER
claudlobby --json bot restart WORKER
claudlobby --json bot stop WORKER
```

`start` is idempotent when the selected bot's session is already ready; use
`restart` for an intentional bounce, with a best-effort handoff and a fresh
bridge/session readiness check. `stop` de-enrolls supervision and stops the
session, so keepalive cannot revive it; a later `start` is explicit re-enrollment.
For a slow bridge, `bot restart WORKER --ceiling SECONDS` overrides the
per-bot readiness budget with a positive number. Read `data.changed`,
`data.readiness`, and `data.native_outcome` in the JSON result before claiming
what happened. If an effect or readiness is unverified, inspect the selected
native unit and private session before another operation. Workers cannot use
another bot's lifecycle. A self restart returns only `requested` with a request
ID and startup-log path; it does not prove readiness. Read the final log entry
after the new session starts before claiming completion.

The current manager can use `workstream open/progress/renew/block/unblock/close/prune`
with a retained `--request-id UUID` for each mutation. `block ID --on
human:NAME --note TEXT` records a declared wait; `unblock ID --note TEXT`
restores that same ID to active. `renew` extends the lease without crediting
progress. Inspect `workstream show ID` and the brief's blocked waits after an
uncertain recording; replaying a UUID never sends a notification.

`task reviews` reads verdicts at the fetched PR head and attributes explicitly
reported reviews across every fleet on the selected host. Inspect each event's
`MATCH`, `UNKNOWN`, or `AMBIGUOUS` evidence and the verdict flags. A successful
read is not merge permission or proof of who authored the PR.

The inbox shows fleet-owned open work, your unread reports, and recent
attention evidence. `--bot VIEWER` selects whose report read position to
display; it does not change your identity or filter work to that bot.
`--limit` bounds each work/report section, and `next_cursor` continues those
pages. Attention shows recent alerts and escalations on open tasks, including queued work;
it does not track whether every question to a human has been answered.
Reading the inbox never acknowledges reports;
use the explicit report-list and acknowledgement commands below.

Use the canonical ID returned by a read. A task ID and assignment ID are
different. If a command refuses a historical or wrong-kind reference, follow
its scoped canonical hint; do not treat the old reference as an alias.

To admit work into your own fleet, retain one canonical UUID for the operation:

```bash
claudlobby --json task admit --title "Concrete outcome" --request-id ADMIT_UUID
claudlobby --json task show TASK_ID
```

Only the fleet manager routes, delivers, or changes its fleet's assignments.
Assign first; then deliver the exact assignment using an existing UTF-8
message file you supply and retain through an uncertain outcome:

```bash
claudlobby --json task assign TASK_ID --bot WORKER --request-id ASSIGN_UUID
claudlobby --json assignment show ASSIGNMENT_ID
claudlobby --json assignment deliver ASSIGNMENT_ID --file FILE --request-id DELIVER_UUID
```

If delivery is unknown, the message may already have been sent. Inspect the
same request and receipt; do not automatically resend or mint another request.

An assignment row is not delivery proof, and delivery is not worker
acceptance. Once you receive an assignment addressed to you, acknowledge that
exact assignment before reporting work:

```bash
claudlobby --json assignment accept ASSIGNMENT_ID --request-id ACCEPT_UUID
claudlobby --json assignment progress ASSIGNMENT_ID --summary "Started the review" --request-id PROGRESS_UUID
claudlobby --json assignment complete ASSIGNMENT_ID --summary "Reviewed the change" --request-id COMPLETE_UUID
claudlobby --json assignment show ASSIGNMENT_ID
```

For a setback, use `assignment block`, `return`, or `fail` with `--reason`,
never an old task ID. `assignment progress` alone may include `--percent 0..100`;
reports may carry `--pr URL --pr-role authored|reviewed`, repeatable
`--artifact URL` and `--issue URL`, and `--skill NAME`. Check each verb's help.
The manager can inspect `task withdraw --help` and `task reassign --help` for
reasoned changes. To record a request for human guidance, including before
assignment, use `task escalate TASK_ID --question "The decision needed" --request-id UUID`.
Escalation leaves the task open; recording it does not prove a human was notified.
To ask the fleet manager to revisit an open task, use `claudlobby --json task nudge
TASK_ID --reason "Why this needs attention" --request-id UUID`. The task fact and
manager request commit before notification. Inspect the notification outcome;
replaying the same request does not resend an uncertain message.

Each mutation needs its own retained request UUID. Reuse the same UUID only
to inspect or reconcile the *same* intended operation; use `claudlobby --json
request show UUID` to inspect its recorded outcome. If a report commits the
task transition but manager notification fails, that transition still stands:
inspect the returned recording and notification outcomes, and do not submit a
second report under a new UUID. The `--by` field records provenance; it does
not grant another actor's authority.

When there is no assignment to report against, submit an explicitly unlinked
report to your own fleet manager. It sends a report message and does not
transition a task:

```bash
claudlobby --json fleet reports submit --status completed --summary "Completed the review" --request-id REPORT_UUID
claudlobby --json request show REPORT_UUID
```

Retain that UUID for the same report. If its outcome is uncertain or exit 11,
inspect the recorded request and transport outcome; do not automatically
resend or mint a new UUID.

Read recorded reports, including reports without assignments, with
`claudlobby --json fleet reports list`. Add `--unacknowledged` for your own
unseen terminal or status-unknown reports. A read does not acknowledge reports
or change tasks.
After reading an unfiltered `--unacknowledged` page, pass its `ack_cursor`
unchanged to `claudlobby --json fleet reports ack --through ACK_CURSOR
--request-id UUID`. Retain that UUID for the same acknowledgement. Follow
`next_cursor` to read another page before acknowledging a longer prefix.
Filtered views do not supply an acknowledgement cursor. An acknowledgement
only advances your report read position; it never accepts a task or clears
another bot's reports. Unavailable history is unknown, never proof that no
worker is waiting.

For an ordinary message to a declared bot, retain a new request UUID and
inspect its recorded outcome and receiver proof separately:

```bash
claudlobby --json message send --to WORKER --text "A concrete question" --request-id UUID
claudlobby --json request show UUID
claudlobby --json message show MESSAGE_ID
claudlobby --json message receipt MESSAGE_ID
```

Reply only to a message addressed to you. The reply goes to the parent's
recorded sender; do not choose another recipient. Retain a new UUID for this
reply and use that same UUID to inspect an uncertain outcome:

```bash
claudlobby --json message reply MESSAGE_ID --text "The requested answer" --request-id REPLY_UUID
claudlobby --json request show REPLY_UUID
```

A visible marker or assignment row alone is not delivery proof. A missing or
unavailable receipt stays unverified. `message wait MESSAGE_ID --for reply
--timeout SECONDS` is a bounded read, not permission to resend. If `message
send` or `message reply` reports `recording_degraded` or exit 11, it may already
have reached the recipient: inspect its returned transport and alert outcomes,
and never retry it automatically.

Stop on permission, scope, or release errors. Do not substitute raw SQL,
hand-built plane events, shell dispatch helpers, or tmux keystrokes for a
refused CLI operation. Stop at an unsupported operation; do not invent another
way to deliver work.
