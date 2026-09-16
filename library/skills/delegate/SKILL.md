---
name: delegate
description: "Use when you need to assign a task to an engineer bot, check bot health, restart a bot, or get a fleet overview."
argument-hint: "[dispatch|status|restart|fleet] [personal-eng|work-eng] [task description]"
---

# Delegate

Manage and dispatch tasks to engineer bots.

## Bots

| Bot | tmux session | systemd service | Scope |
|-----|-------------|-----------------|-------|
| personal-eng | <bot-c> | <bot-c> | personal-projects, side-project-1, side-project-2 |
| business-eng | <bot-b> | <bot-b> | data-warehouse, ingest-pipeline, internal-api |
| code-reviewer | code-reviewer-bot | code-reviewer-bot | PR reviews only (Sonnet 4.6) |

## Commands

### dispatch <bot> <task>

Send a task to an engineer bot via the socket-aware helper — each bot is on its own tmux server, so a raw `tmux send-keys -t` against the default socket no longer reaches it (reliable, instant):

```bash
$CLAUDLOBBY_ROOT/lib/dispatch.sh <bot-session> '<task prompt>'
```

Before dispatching (each bot is on its own server; the socket is its `BOT_SERVICE`):
1. Check the bot is alive: `tmux -L <bot-service> has-session -t <bot-session>`
2. Check it's not already busy: `tmux -L <bot-service> capture-pane -t <bot-session> -p | tail -5`
3. If busy, wait or tell the user

After dispatching:
- A tracked (id'd) dispatch pages you via the overdue watchdog — don't poll it. An untracked send (this skill's `dispatch.sh` examples) has no watchdog: capture the worker's pane if nothing comes back — your only net for that class
- Report outcomes in the Telegram group

### status <bot>

Check a specific bot's health:

```bash
tmux has-session -t <bot-session> && echo "ALIVE" || echo "DEAD"
tmux capture-pane -t <bot-session> -p | tail -10
```

### restart <bot>

Restart a bot (e.g., after heavy session, context exhaustion):

```bash
sudo systemctl restart <bot-service>
```

### fleet

Overview of all bots:

```bash
for bot in <bot-a> <bot-b> <bot-c> <bot-d>; do
    if tmux has-session -t $bot 2>/dev/null; then
        echo "$bot: ALIVE"
    else
        echo "$bot: DEAD"
    fi
done
```

## Rules

- Don't dispatch to a bot that's already processing
- If a bot is stuck for >10 min, restart it
- Always report dispatch and results in the Telegram group
- For quick questions, @mention the bot in Telegram instead of tmux dispatch
