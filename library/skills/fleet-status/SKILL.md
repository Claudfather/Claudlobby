---
name: fleet-status
description: "Quick health check across all fleet bots — tmux sessions, service status, reported context-degraded state, who's idle/working/dead."
argument-hint: "[bot-name]"
---

# Fleet Status

Check health of all bots in the fleet.

## Bot Discovery

Discover running bots by listing tmux sessions. If `$CLAUDLOBBY_ROOT` and `$FLEET_NAME` are set, also read `fleet.yaml` to compare expected vs running bots.

### Step 1: Discover running bots

```bash
tmux list-sessions -F '#{session_name}' 2>/dev/null
```

This is the source of truth for what is currently alive.

### Step 2: Discover expected bots (optional)

If `$CLAUDLOBBY_ROOT` and `$FLEET_NAME` are set, parse the fleet config to get the expected bot list:

```bash
# For overlay fleets:
grep -A1 '^\s*bots:' "$CLAUDLOBBY_ROOT/local/$FLEET_NAME/fleet.yaml" 2>/dev/null

# For seed fleet:
grep -A1 '^\s*bots:' "$CLAUDLOBBY_ROOT/fleet.yaml.seed" 2>/dev/null
```

Compare expected bots against running tmux sessions to identify bots that should be running but aren't (MISSING) and sessions that exist but aren't in the fleet config (UNREGISTERED).

If the fleet config is unavailable, report only what tmux shows.

## Checks

### Reported context state

No bot can measure a context percentage, so do not ask for one and do not
report one (`context-management`). What IS available is the worker's own
`context-degraded` report:

```bash
claudlobby report-back --since 24h | grep -i context-degraded
```

Any bot listed there is asking to be restarted — pair it with its completed
count in the same window before deciding.


For each discovered bot:

```bash
for bot in $(tmux list-sessions -F '#{session_name}' 2>/dev/null); do
    PANE=$(tmux capture-pane -t "$bot" -p 2>/dev/null | tail -3)
    echo "$bot: ALIVE | $PANE"
done
```

For any expected bot not found in tmux sessions:

```bash
echo "$bot: DEAD"
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

Bots (discovered from tmux sessions + fleet.yaml):
  <bot-a>: ALIVE (idle)
  <bot-b>: ALIVE (working — last 3 lines of pane output)
  <bot-c>: DEAD (expected in fleet.yaml, no tmux session)

System:
  RAM: 4.2G / 16G | Temp: 58C | Disk: 19G / 235G (9%)
```

If an argument is provided (a specific bot name), check only that bot instead of the full fleet.
