---
name: fleet-status
description: "Quick health check across all fleet bots — tmux sessions, service status, context usage, who's idle/working/dead."
argument-hint: "[bot-name]"
---

# Fleet Status

Check health of all bots in the fleet.

## Checks

For each bot in the fleet:

```bash
for bot in eng-a-bot eng-b-bot code-reviewer-bot; do
    if tmux has-session -t $bot 2>/dev/null; then
        PANE=$(tmux capture-pane -t $bot -p 2>/dev/null | tail -3)
        echo "$bot: ALIVE | $PANE"
    else
        echo "$bot: DEAD"
    fi
done
```

Also check system resources:

```bash
free -h | head -2
vcgencmd measure_temp 2>/dev/null
df -h / | tail -1
```

## Report Format

```
FLEET STATUS

Bots:
  eng-a-bot:         ALIVE (idle)
  eng-b-bot:         ALIVE (working — fixing auth test)
  code-reviewer-bot: ALIVE (idle)

System:
  RAM: 4.2G / 16G | Temp: 58C | Disk: 19G / 235G (9%)
```

Update the bot list to match your actual fleet.
