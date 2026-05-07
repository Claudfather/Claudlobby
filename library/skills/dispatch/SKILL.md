---
name: dispatch
description: "Dispatch a task to a fleet bot with structured tracking. Validates bot health, sends via tmux, emits to Telegram."
argument-hint: "<bot> <task description> [--repo <repo>] [--issue <url>]"
---

# Dispatch

Send a task to a fleet bot. Validates health before dispatching, sends via tmux, reports in Telegram.

## Bots

Update this table with your actual fleet:

| Bot | tmux session | systemd service | Scope |
|-----|-------------|-----------------|-------|
| Engineer A | eng-a-bot | eng-a-bot | Repo group A |
| Engineer B | eng-b-bot | eng-b-bot | Repo group B |
| Code Reviewer | code-reviewer-bot | code-reviewer-bot | PR reviews |

## Dispatch Flow

1. **Check bot is alive:** `tmux has-session -t <bot-session>`
2. **Check bot is idle:** `tmux capture-pane -t <bot-session> -p | tail -5`
3. **If busy:** report to user, wait or queue
4. **If idle:** `tmux send-keys -t <bot-session> '<task prompt>' Enter`
5. **Emit to Telegram:** notify the group that a task was dispatched

## Rules

- Don't dispatch to a bot that's already processing
- If a bot is stuck for >10 min, restart it
- Always report dispatch and results in Telegram
