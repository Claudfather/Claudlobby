---
name: lifecycle
description: "Full development lifecycle: implement → review → iterate → merge → retro → issues. Orchestrates fleet bots."
argument-hint: "<task description> [--repo <repo>] [--engineer <bot-name>] [--issue <url>] [--skip-retro]"
---

# Lifecycle

Full pipeline orchestrated by the manager bot.

## Flow

1. Dispatch engineer to implement (via tmux send-keys)
2. Wait for [BOTREPORT] with status
3. Dispatch code reviewer for PR review
4. If approved → merge PR
5. If mechanical fixes needed → send back to engineer automatically
6. If ambiguous concerns → flag human
7. After merge → run development retro
8. Create GitHub issues from retro findings

## Decision Framework

| Situation | Action |
|-----------|--------|
| Engineer completes work | Auto-dispatch to reviewer |
| Reviewer approves | Auto-merge |
| Reviewer: mechanical fixes (lint, types, unused vars) | Auto-send back to engineer |
| Reviewer: ambiguous concerns (scope, architecture) | Flag human |
| 3+ review cycles | Flag human |
| Post-merge retro findings | Auto-create GitHub issues |
| Engineer reports blocked | Flag human |

## Rules

- Every phase transition gets a Telegram message for visibility
- Never go silent — report what's happening
- Mechanical decisions: auto-proceed. Judgment calls: flag human.
