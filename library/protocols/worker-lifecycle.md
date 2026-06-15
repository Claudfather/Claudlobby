---
title: Worker Lifecycle Protocol
description: End-to-end numbered procedure for workers receiving and executing tasks. Defines both inbound command parsing ([BOTCOMMAND]) and outbound dual-channel communication (Telegram + [BOTREPORT]). This is the OUTER envelope — role-specific procedures run inside it.
---

# Worker Lifecycle Protocol

Every worker follows this numbered lifecycle on every task. Role-specific procedures (Review Methodology, Implementation Lifecycle, Query Workflow, etc.) execute **inside** Steps 3–7. Their internal numbering does not replace or override this envelope.

## Inbound: [BOTCOMMAND] format

Managers send structured commands to workers via tmux:

```
[BOTCOMMAND] <manager> | <type> | <summary> | <key:value pairs>
```

**Types:** `task` / `cancel` / `compact` / `restart` / `query`

**Key-value pairs** (optional, pipe-delimited): `repo:<name>` / `branch:<name>` / `report:<target>` / `priority:<high|normal|low>` / `ref:<issue-or-pr-url>`

Example:

```
[BOTCOMMAND] ari | task | "Run security audit on storydump" | repo:storydump | report:clog
```

Workers parse `[BOTCOMMAND]`, ack within 5 seconds, then execute. If the manager receives no ack within 10 seconds, it retries once. A second silence triggers escalation to the human.

## Outbound: dual-channel communication

Workers communicate on **two** channels simultaneously:

| Channel | Audience | Mechanism | Purpose |
|---------|----------|-----------|---------|
| Telegram group | Human | `mcp__plugin_telegram_telegram__reply` with `chat_id` from `$TELEGRAM_GROUP_CHAT_ID` | Visibility — the human sees progress without checking tmux |
| `[BOTREPORT]` | Manager | `$CLAUDLOBBY_ROOT/lib/report-back.sh` | Machine coordination — structured status for the manager's decision framework |

Both channels fire at lifecycle boundaries. Telegram is prose; `[BOTREPORT]` is structured.

## You are monitored (and why it helps you)

Your tool-call activity is observed by the fleet pulse. If your session is alive and not at an idle prompt but you make **no tool call for a long stretch** (the configured `activity_stuck` threshold — e.g. a main thread that stalled after a subagent returned), the pulse flags `activity_stuck` and notifies your manager. This is a safety net, not surveillance: it exists so a silent hang gets noticed in minutes instead of hours. You will only be restarted after the `safe-worker-restart` guards pass (no uncommitted WIP, no pending report expected) — so keep work committed and report at lifecycle boundaries. A restart preserves context at **best-achievable fidelity, not zero loss**: an intentional restart writes a `session.md` handoff first and the new session resumes from it, but that handoff is a *summary*, not the live conversation. Committed work and reported state are what survive a restart intact — so the discipline above is exactly what makes the resume reliable.

## The lifecycle

```
1. RECEIVE     ─── parse [BOTCOMMAND] or freeform dispatch
2. ACK         ─── Telegram + [BOTREPORT] progress
3. PLAN        ─── (conditional) subagent if complex
4. BRANCH      ─── git checkout -b off fresh main
5. IMPLEMENT   ─── role-specific work, Telegram milestones
6. VERIFY      ─── tests, lint, shellcheck
7. COMMIT + PR ─── push, open PR
8. COMPLETE    ─── Telegram + [BOTREPORT] completed
9. BLOCKED     ─── (any point) Telegram + [BOTREPORT] blocked
```

### Step 1: RECEIVE

Parse the inbound. Extract type, summary, and key-value pairs. If the dispatch is freeform (no `[BOTCOMMAND]` prefix), treat summary as the full prompt and infer type as `task`.

For `cancel`: stop current work, discard uncommitted changes on the task branch, ack cancellation.
For `compact`: run `/compact`, ack.
For `restart`: wrap up, report back, expect session restart.
For `query`: answer inline without branching or PRs — skip to Step 8 after answering.

### Step 2: ACK

**Within 5 seconds of receiving the dispatch.** Non-negotiable, even for trivial queries.

Telegram post: `On it: <one-line summary of understood task>`

```bash
# Telegram ack (MCP tool)
mcp__plugin_telegram_telegram__reply(chat_id=$TELEGRAM_GROUP_CHAT_ID, text="On it: <summary>")
```

Report-back:

```bash
$CLAUDLOBBY_ROOT/lib/report-back.sh <bot-name> progress "Acked: <summary>"
```

**This step happens BEFORE any other tool call.** Before reading files, before spawning subagents, before git operations. The ack is the first thing.

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

**Telegram milestones every 2–3 minutes of active work:**

- After completing a significant sub-step
- When switching between files or phases
- When encountering something unexpected

Format: one line, factual. `Staging model done, writing tests.` / `Found upstream nulls in raw.events — tracing.` / `3/5 files updated.`

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

Report-back:

```bash
$CLAUDLOBBY_ROOT/lib/report-back.sh <bot-name> completed "<summary>" --pr <pr-url>
```

### Step 9: BLOCKED (any point)

If blocked at any step, **immediately** — do not spin for more than 3 minutes:

Telegram post:

```
Blocked: <what's wrong and what you tried>
@<manager-handle>
```

Report-back:

```bash
$CLAUDLOBBY_ROOT/lib/report-back.sh <bot-name> blocked "<reason>"
```

Then stop. Do not attempt workarounds that might cause damage. Wait for guidance.

## Authority and precedence

This protocol is the **outer envelope**. Role-specific numbered procedures from expertise libraries (Review Methodology, Implementation Lifecycle, Query Workflow, Alert Triage) execute inside Steps 3–7. Their internal numbering does not replace Step 2 (ACK) or Step 8 (COMPLETE).

Concretely: if your expertise says "Step 1: Read the PR description" — that runs inside this protocol's Step 5 (IMPLEMENT). This protocol's Step 1 (RECEIVE) and Step 2 (ACK) have already fired before your expertise procedure begins.

## Quick reference: what fires when

| Moment | Telegram | [BOTREPORT] |
|--------|----------|-------------|
| Task received | "On it: ..." | `<bot-name> progress "Acked: ..."` |
| Planning start (if applicable) | "Planning: ..." | — |
| Every 2-3 min during work | One-line milestone | — |
| Scope surprise | "Scope note: ..." | `<bot-name> progress "Scope: ..."` |
| Completion | "Done: ... PR: <url>" | `<bot-name> completed "<summary>" --pr <url>` |
| Blocked | "Blocked: ..." | `<bot-name> blocked "<reason>"` |
