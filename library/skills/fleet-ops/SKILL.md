---
name: fleet-ops
description: "Read fleet work, admit tasks, deliver and route assignments as manager, and accept or report assigned work as worker. Use the canonical claudlobby CLI and verify recorded results."
tool_grants:
  - "Bash(claudlobby --help)"
  - "Bash(claudlobby task list --help)"
  - "Bash(claudlobby task show --help)"
  - "Bash(claudlobby task admit --help)"
  - "Bash(claudlobby task assign --help)"
  - "Bash(claudlobby task withdraw --help)"
  - "Bash(claudlobby task reassign --help)"
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
  - "Bash(claudlobby request show --help)"
  - "Bash(claudlobby --json context show)"
  - "Bash(claudlobby --json task list)"
  - "Bash(claudlobby --json task list *)"
  - "Bash(claudlobby --json task show *)"
  - "Bash(claudlobby --json assignment show *)"
  - "Bash(claudlobby --json message show *)"
  - "Bash(claudlobby --json message receipt *)"
  - "Bash(claudlobby --json message wait *)"
  - "Bash(claudlobby --json message send *)"
  - "Bash(claudlobby --json request show *)"
---

# Fleet operations

Use this guide for task, assignment, message, and request reads; the admitted
task and assignment lifecycle; and ordinary message send. The generated bot
context selects your fleet and identity; check it with `claudlobby --json
context show`. Discover exact flags with `claudlobby --help` and `claudlobby
<group> <verb> --help`.

Read before acting:

```bash
claudlobby --json task list
claudlobby --json task show TASK_ID
claudlobby --json assignment show ASSIGNMENT_ID
```

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
reasoned changes.

Each mutation needs its own retained request UUID. Reuse the same UUID only
to inspect or reconcile the *same* intended operation; use `claudlobby --json
request show UUID` to inspect its recorded outcome. If a report commits the
task transition but manager notification fails, that transition still stands:
inspect the returned recording and notification outcomes, and do not submit a
second report under a new UUID. The `--by` field records provenance; it does
not grant another actor's authority.

For an ordinary message to a declared bot, retain a new request UUID and
inspect its recorded outcome and receiver proof separately:

```bash
claudlobby --json message send --to WORKER --text "A concrete question" --request-id UUID
claudlobby --json request show UUID
claudlobby --json message show MESSAGE_ID
claudlobby --json message receipt MESSAGE_ID
```

A visible marker or assignment row alone is not delivery proof. A missing or
unavailable receipt stays unverified. `message wait MESSAGE_ID --for reply
--timeout SECONDS` is a bounded read, not permission to resend. If `message
send` reports `recording_degraded` or exit 11, it may already have reached the
recipient: inspect its returned transport and alert outcomes, and never retry
it automatically.

Stop on permission, scope, or release errors. Do not substitute raw SQL,
hand-built plane events, shell dispatch helpers, or tmux keystrokes for a
refused CLI operation. Stop at an unsupported operation; do not invent an
unlinked report or another way to deliver work.
