---
title: Worker Lifecycle Protocol
description: End-to-end procedure for workers receiving assignments and reporting through canonical fleet operations, with Telegram visibility. This is the OUTER envelope — role-specific procedures run inside it.
---

# Worker Lifecycle Protocol

Every worker follows this numbered lifecycle on every task. Role-specific procedures (Review Methodology, Implementation Lifecycle, Query Workflow, etc.) execute **inside** Steps 3–7. Their internal numbering does not replace or override this envelope.

## Inbound: assignments and legacy lifecycle notes

The manager admits fleet work, creates an assignment, then delivers it. The
delivery message identifies the current `ASSIGNMENT_ID`; inspect that ID with
`claudlobby --json assignment show ASSIGNMENT_ID`. A task ID identifies the
underlying work and is not an assignment ID. The assignment row alone is not
proof that its message arrived. Use `/fleet-ops` for the current command and
receipt contract.

Some unmigrated lifecycle notes can still arrive in the historical format:

```
[BOTCOMMAND] <manager> | <type> | <summary> | <key:value pairs>
```

**Legacy types:** `cancel` / `compact` / `restart` / `query`. Handle these as
instructions, not as canonical Task transitions; do not invent a CLI verb for
them. A historical `task` envelope is context, not a substitute for a current
assignment ID.

**Key-value pairs** (optional, pipe-delimited): `repo:<name>` / `branch:<name>` / `report:<target>` / `priority:<high|normal|low>` / `ref:<issue-or-pr-url>`

Example:

```
[BOTCOMMAND] ari | query | "What evidence is missing from the review?" | ref:https://github.com/org/repo/pull/123
```

For a canonical assignment, record acceptance explicitly before progress.
Acceptance is a separate lifecycle fact, not a status report or proof of
delivery. Use a retained UUID for that operation.

## Outbound: dual-channel communication

Workers communicate on **two** channels simultaneously:

| Channel | Audience | Mechanism | Purpose |
|---------|----------|-----------|---------|
| Telegram group | Human | `mcp__plugin_telegram_telegram__reply` with `chat_id` from `$TELEGRAM_GROUP_CHAT_ID` | Visibility — the human sees progress without checking the fleet view |
| Assignment report or unlinked fleet report | Manager | `claudlobby --json assignment ...` or `fleet reports submit` | Machine coordination — recorded status and manager notification |

Both channels fire at lifecycle boundaries. Telegram is prose; the CLI records
structured facts. A committed fact and a received notification are separate
outcomes; inspect both, and never automatically resend an uncertain request.

## You are monitored (and why it helps you)

Your tool-call activity is observed by the fleet pulse. If your session is alive and not at an idle prompt but you make **no tool call for a long stretch** (the configured `activity_stuck` threshold — e.g. a main thread that stalled after a subagent returned), the pulse flags `activity_stuck` and notifies your manager. This is a safety net, not surveillance: it exists so a silent hang gets noticed in minutes instead of hours. You will only be restarted after the `safe-worker-restart` guards pass (no uncommitted WIP, no pending report expected) — so keep work committed and report at lifecycle boundaries. A restart preserves context at **best-achievable fidelity, not zero loss**: an intentional restart writes a `session.md` handoff first and the new session resumes from it, but that handoff is a *summary*, not the live conversation. Committed work and reported state are what survive a restart intact — so the discipline above is exactly what makes the resume reliable.

## The lifecycle

```
1. RECEIVE     ─── read the delivered assignment or legacy lifecycle note
2. ENGAGE      ─── accept the exact assignment (Step 2)
3. PLAN        ─── (conditional) subagent if complex
4. BRANCH      ─── git checkout -b off fresh main
5. IMPLEMENT   ─── role-specific work, one thin line on done/blocked
6. VERIFY      ─── tests, lint, shellcheck
7. COMMIT + PR ─── push, open PR
8. COMPLETE    ─── Telegram + assignment complete
9. BLOCKED     ─── (any point) Telegram + assignment block or return
```

### Step 1: RECEIVE

Read the assignment and its delivered instructions. If a note is freeform,
read it as context; establish the current assignment ID before a linked
transition. If no assignment exists, use an explicitly unlinked fleet report.

A final line of the form `⟦plane:msg_…⟧` is a framework **delivery-receipt marker**, always on its own last line — ignore it entirely; it is never part of the task.

**Part of the dispatch arrives inside `<pasted_content>` tags?** Verify, then trust, as **Dispatches framed as pasted text** in this file says. Every bot carries that section, whatever it composes.

For legacy `cancel`: stop current work and seek the manager's decision on the
current assignment; do not discard WIP or claim the task was withdrawn merely
because a note arrived. For `compact`: run `/compact`. For `restart`: wrap up,
record current progress if assigned, and expect session restart. For `query`:
answer inline without branching or PRs. Report a lifecycle note without an
assignment through `fleet reports submit`; never attach an unrelated old ID.

### Step 2: ENGAGE (accept the current assignment)

After verifying that the assignment is current and addressed to you, accept
it with its exact ID before reporting work:

```bash
claudlobby --json assignment accept ASSIGNMENT_ID --request-id ACCEPT_UUID
```

Retain `ACCEPT_UUID` for inspection or replay of the same request. Do not use
a historical display task ID or an assignment from an earlier routing.
Acceptance is not progress; record progress only when there is progress to
report. No Telegram acknowledgement post is needed.

### Step 3: PLAN (conditional)

If the task touches >5 files, involves schema changes, or is architecturally non-trivial:

1. Spawn an Explore or Plan subagent to survey scope.
2. Post a one-line Telegram update: `Planning: <what you're surveying>`

For simple tasks (single-file fix, query, < 5 files), skip directly to Step 4.

### Step 4: BRANCH

```bash
git checkout main && git pull --ff-only
git checkout -b <descriptive-branch>
```

Branch naming: `feat/`, `fix/`, `chore/` prefix + kebab-case description. Keep it under 50 chars.

### Step 5: IMPLEMENT

Execute role-specific work. This is where expertise procedures (Review Methodology, dbt modeling workflow, alert triage, etc.) run as sub-steps.

### Step 6: VERIFY

Run the appropriate verification for the work type:

- Code: tests, linter, type-check
- Shell scripts: `shellcheck`
- dbt: `dbt build --select <model>+`
- SQL: `EXPLAIN` on complex queries

If verification fails, fix and re-verify. Do not push failing code.

### Step 7: COMMIT + PR

```bash
git add <specific files>
git commit -m "<conventional commit message>"
git push -u origin <branch>
gh pr create --title "<title>" --body "<body with context>"
```

PR body includes: what changed, why, how verified, and references the originating issue if applicable.

### Step 8: COMPLETE

Telegram post (tag the manager):

```
Done: <one-line summary>. PR: <url>
@<manager-handle>
```

Record the terminal result:

```bash
claudlobby --json assignment complete ASSIGNMENT_ID --summary "<summary>" --pr <pr-url> --pr-role authored --request-id COMPLETE_UUID
```

Use `--pr-role authored` only for a PR you authored; use `reviewed` for a
reviewed PR. Omit both optional flags when neither applies.

If no assignment exists, use `fleet reports submit` as in `/fleet-ops`.

### Step 9: BLOCKED (any point)

If blocked at any step, **immediately** — do not spin for more than 3 minutes:

Telegram post:

```
Blocked: <what's wrong and what you tried>
@<manager-handle>
```

Record the blocker while retaining the assignment:

```bash
claudlobby --json assignment block ASSIGNMENT_ID --reason "<reason>" --request-id BLOCK_UUID
```

If you cannot keep ownership, use `assignment return` with its own UUID and
reason; a block does not yield the work. Then stop and wait for guidance.

## Authority and precedence

This protocol is the **outer envelope**. Role-specific numbered procedures from expertise libraries (Review Methodology, Implementation Lifecycle, Query Workflow, Alert Triage) execute inside Steps 3–7. Their internal numbering does not replace Step 2 (ENGAGE) or Step 8 (COMPLETE).

Concretely: if your expertise says "Step 1: Read the PR description" — that
runs inside this protocol's Step 5 (IMPLEMENT), after Step 2 accepted the
current assignment.

## Quick reference: what fires when

| Moment | Telegram | Canonical operation |
|--------|----------|-------------|
| Assignment received | — | `assignment accept ASSIGNMENT_ID --request-id UUID` |
| Planning start (if applicable) | "Planning: ..." | — |
| Scope surprise | "Scope note: ..." | `assignment progress ASSIGNMENT_ID --summary "Scope: ..." --request-id UUID` |
| Completion | "Done: ... PR: <url>" | `assignment complete ASSIGNMENT_ID --summary "..." --pr URL --pr-role ROLE --request-id UUID` (choose authored or reviewed) |
| Blocked | "Blocked: ..." | `assignment block ASSIGNMENT_ID --reason "..." --request-id UUID` |
