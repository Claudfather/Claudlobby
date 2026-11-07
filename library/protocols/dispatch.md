---
title: Dispatch Protocol
---

# Dispatch Protocol

Fleet work is admitted before it is assigned, and assignment is recorded before
delivery. The fleet-ops guide supplied in your instructions (also the
`/fleet-ops` skill) owns the full command contract, grants, request
reconciliation, and worker reporting: the admit, assign and deliver commands,
the canonical IDs each step returns, request UUIDs, what to do after an
uncertain outcome, the message door, the reads of current state,
`task escalate`, the `bot start` command and which of its result fields to
read, and `fleet reconcile`. For `task withdraw` and `task reassign` it names
their `--help`. The readiness gate on a `bot start` result, below, is this
protocol's own. This protocol adds only what a manager checks around them.

## Manager task flow

Choose a concrete outcome and prepare an existing UTF-8 delivery file with the
worker's instructions and relevant plan, repo, deadline, and project context.
Admit with the task's repo and project; the guide shows the assign and deliver
steps that follow:

```bash
claudlobby --json task admit --title "Run the security audit" --repo org/repo-a --project PROJECT_KEY --request-id ADMIT_UUID
```

`task admit` leaves queued fleet-owned work even when no worker is ready. A
check-in decision uses `task assign TASK_ID --bot WORKER --checkin ck_...
--request-id ASSIGN_UUID` *after* the check-in decision has committed. Do not
add `--checkin` to admit. The check-in skill owns the record-before-act
sequence.

Do not turn an uncertain send or admit into a second assignment or a second
task.

For an ordinary question that does not create work, use the guide's message
door. Do not turn a question into a task solely to deliver text. A worker
answers a note that asked for no work with `fleet reports submit`, the
explicitly unlinked report: it links no task and closes nothing.

## Follow-up and re-check

An absent deadline alert is not proof that work completed. Re-checks ask the
current manager to inspect overdue or aged open tasks; use the task ID in the
notice. An assignment a person made with no deadline is a standing goal: the
re-check lists it by ID but never asks about it on age alone. A nudge asks for
a decision and does not itself change task state. Escalation asks one concrete
human decision; it is not a status poll.

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

Dispatch only after the result has `ok: true`, `data.state: "running"`,
`data.native_outcome: "observed"`, and `data.readiness` of
`current_session_ready`, `bridge_ready`, or `session_ready`. The first proves
the existing supervised session, not bridge delivery; `session_ready` is the
non-channel or intentionally tokenless outcome. On a conflict or unavailable
result, inspect the exact unit and private session; do not fall back to a raw
launcher or deliver into unknown readiness.

When `fleet reconcile` reports unknown ownership, unexpected units or retained
undeclared directories, they require operator inspection. There is no public
`--enroll` repair flag and no reason to invoke a private script from a bot
session.

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
