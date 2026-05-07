---
name: data-alert-sweep
description: "Batch-process alerts from a monitoring channel. Pull alerts, check for existing PRs, independently investigate, compare findings, and reply to threads."
argument-hint: "[recent|today|all-open]"
---

# Alert Sweep

Batch-process alerts from a monitoring channel with independent verification.

## Core Principle

Always investigate independently. Existing PRs and team comments are inputs, not conclusions. If you arrive at the same fix, approve it. If different, post as discussion on the existing PR. Never create competing PRs.

## Workflow

### Step 1: Gather Context
- Read recent alerts from the monitoring channel
- Read full thread replies (bot feedback, team comments)
- Note team direction — but verify independently

### Step 2: Check for Existing PRs
- Search for PRs matching each alert
- Read PR description, comments, review status, files changed

### Step 3: Execute (parallel where possible)
- **Track A:** Independent investigation (always runs)
- **Track B:** Review existing PR (if one exists)

### Step 4: Compare and Decide
| Situation | Action |
|-----------|--------|
| Same conclusion as existing PR | Approve, add context |
| Different conclusion | Post findings as comment on existing PR |
| No existing PR, fix needed | Create PR from investigation |
| No fix needed | Note in thread (upstream issue, transient, etc.) |

### Step 5: Reply to Threads
- 1-2 line summary + PR link per alert thread
- Keep replies concise and actionable

## Rules

- Read ALL thread context before acting
- Read ALL PR comments before reviewing
- Never create competing PRs
- Team input is a signal, not gospel — verify everything
