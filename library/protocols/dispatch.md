---
title: Dispatch Protocol
---

# Dispatch Protocol

Fleet work is admitted before it is assigned, and assignment is recorded before
delivery. Use the canonical `task_id` and `assignment_id` returned by each step;
these are different IDs. The default `/fleet-ops` skill owns the full command
contract, grants, request reconciliation, and worker reporting. Discover the
current grammar with `claudlobby task --help` and `claudlobby assignment --help`.

## Manager task flow

Choose a concrete outcome and prepare an existing UTF-8 delivery file with the
worker's instructions and relevant plan, repo, deadline, and project context.
For one operation, retain its UUID; each *new* operation gets a different UUID.
Inspect each schema-1 result before proceeding:

```bash
claudlobby --json task admit --title "Run the security audit" --repo org/repo-a --project PROJECT_KEY --request-id ADMIT_UUID
claudlobby --json task assign TASK_ID --bot WORKER --request-id ASSIGN_UUID
claudlobby --json assignment deliver ASSIGNMENT_ID --file FILE --request-id DELIVER_UUID
```

`task admit` leaves queued fleet-owned work even when no worker is ready.
`task assign` records routing but is not delivery or acceptance. Only the
manager delivers to the current assignment; the worker accepts and reports
against that assignment. A check-in decision uses `task assign TASK_ID --bot
WORKER --checkin ck_... --request-id ASSIGN_UUID` *after* the check-in decision
has committed. Do not add `--checkin` to admit. The check-in skill owns the
record-before-act sequence.

If a write or send has an uncertain outcome, inspect `claudlobby --json request
show UUID` and the relevant task, assignment, or message receipt. A committed
fact or submitted send may already exist. Do not automatically resubmit with a
new UUID or resend the delivery file. A historical dispatch reference is not
an alias for either canonical ID; use the scoped hint from the read command.

For an ordinary question that does not create work, use the message door:

```bash
claudlobby --json message send --to WORKER --text "What did the audit find?" --request-id MESSAGE_UUID
claudlobby --json request show MESSAGE_UUID
claudlobby --json message show MESSAGE_ID
```

Message recording, arrival proof, and any reply are separate observations.
Do not turn a question into a task solely to deliver text. A worker answers a
note that asked for no work with `fleet reports submit`, the explicitly unlinked
report: it links no task and closes nothing.

## Follow-up and re-check

Read `claudlobby --json task show TASK_ID` or `claudlobby --json fleet inbox`
for current state. `claudlobby --json brief` shows fleet-owned open work for a
manager, including queued intake, with canonical IDs and explicit unresolved
history. An absent deadline alert is not proof that work completed. Re-checks
ask the current manager to inspect overdue or aged open tasks; use the task ID
in the notice. An assignment a person made with no deadline is a standing goal:
the re-check lists it by ID but never asks about it on age alone. A nudge asks
for a decision and does not itself change task
state. Use `task withdraw`, `task reassign`, or `task escalate` according to the
current CLI help and retained request-ID policy. Escalation asks one concrete
human decision; it is not a status poll. Do not silently convert an uncertain
send into a second assignment or a second task.

### Always zone a timestamp

Never write a bare `HH:MM` to another bot. Use `10:47 EDT` or `14:47Z` so
workers and managers can reconcile deadlines across host and local clocks.

## Preflight: ensure the worker is up under proper supervision

Before dispatch, verify the target session exists. If it doesn't, the selected
fleet manager uses `claudlobby --json bot start WORKER_ID`, replacing
`WORKER_ID` with that declared bot's literal ID. This command checks the
selected release and exact supervision unit, enrolling and starting the bot
when needed; an already-ready session is left running. A raw `start-bot.sh`
session is not supervised.

```bash
# Replace WORKER_ID with the target's literal declared bot ID.
claudlobby --json bot start WORKER_ID
```

Dispatch only after the result has `ok: true`, `data.state: "running"`,
`data.native_outcome: "observed"`, and `data.readiness` of
`current_session_ready`, `bridge_ready`, or `session_ready`. The first proves
the existing supervised session, not bridge delivery; `session_ready` is the
non-channel or intentionally tokenless outcome. On a conflict or unavailable
result, inspect the exact unit and private session; do not fall back to a raw
launcher or deliver into unknown readiness.

To audit the selected fleet's supervision state, run:

```bash
claudlobby --json fleet reconcile
```

This reports discrepancies without changing enrollment or pruning state. A
manager may explicitly start a declared worker with `claudlobby --json bot start
BOT`; inspect its native outcome and readiness before delivery. Unknown
ownership, unexpected units and retained undeclared directories require operator
inspection. There is no public `--enroll` repair flag and no reason to invoke a
private script from a bot session.

## Preflight: check shared knowledge before dispatch

Before dispatching to a repo, check what the fleet already knows about it: is there an active plan for the target repo, and are there learnings the worker should start with?

**Use the door your Shared Documentation section names, not a hardcoded path.** A vault-wired fleet queries Claudron (`claudron recall`); a fleet with a raw doc tree scans `shared/planning/active/INDEX.md` and `shared/knowledge/<repo>/INDEX.md`. Both answer the same question, and your composed instructions say which one you have — reaching for the wrong one on a vault-wired fleet means hand-opening files the vault door exists to replace (#1172).

If relevant docs exist, **include the key context in the dispatch prompt** so the engineer doesn't duplicate work, contradict an in-flight plan, or re-discover something the fleet already knows.

Example: if an active plan covers auth refactoring in `backend`, and you're dispatching a login endpoint task to the same repo, reference the plan in the dispatch: "See the active backend auth-rework plan — your task aligns with phase 2."

## Manager: active-plan monitoring

The orchestrator periodically reviews the fleet's active plans — through the door its Shared Documentation section names, same rule as above — to:

- **Surface stale plans** — status: active but `updated:` older than 7 days. Ping the owner.
- **Detect conflicts** — two active plans touching the same repo. Flag for human resolution.
- **Catch forgotten transitions** — a completed task whose plan still says status: active. Nudge the owner to update status and regenerate the directory's INDEX.md with whichever indexing skill they have.
