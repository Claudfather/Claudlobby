---
name: pi-status
description: "Use when asked about Pi health, bot fleet status, system resources, or when doing a general health check across all bots and services."
argument-hint: "[full|bots|system|crons]"
---

# Pi Status

Full system and fleet health check for the Raspberry Pi.

## Checks

### 1. Bot Fleet

For each bot, check session and report status:

```bash
for bot in <bot-a> <bot-b>; do
    if tmux has-session -t $bot 2>/dev/null; then
        PANE=$(tmux capture-pane -t $bot -p 2>/dev/null | tail -3)
        echo "$bot: ALIVE | $PANE"
    else
        echo "$bot: DEAD"
    fi
done
```

Also check systemd service status:
```bash
systemctl is-active <bot-a> <bot-b>
```

### 2. System Resources

```bash
# Temperature
vcgencmd measure_temp

# Memory
free -h

# Disk
df -h / | tail -1

# Swap
swapon --show

# Load
uptime

# Top processes by memory
ps aux --sort=-%mem | head -6
```

### 3. MCP Servers

Count running MCP processes:
```bash
echo "Total MCP processes: $(ps aux | grep -E 'uvx|npx|workspace-mcp|notion|github|shopify|printify' | grep -v grep | wc -l)"
```

### 4. Cron Health

Check last keepalive log entries:
```bash
echo "=== Assistant keepalive ==="
claudlobby uptime --bot <bot-a>      # heartbeat history, from the plane
echo "=== Business bot keepalive ==="
claudlobby uptime --bot <bot-b>
```

### 5. Network

```bash
ping -c 1 -W 2 8.8.8.8 > /dev/null 2>&1 && echo "Internet: OK" || echo "Internet: DOWN"
curl -s -o /dev/null -w "HA: %{http_code}" http://localhost:8123 2>/dev/null
```

## Response Format

Summarize as a concise dashboard:

```
PI STATUS

Bots:
  <bot-a>: alive (uptime: 2d 4h) | idle
  <bot-b>:    alive (uptime: 1d 12h) | idle

System:
  Temp: 52C | CPU: 4 cores | Load: 0.3
  RAM: 3.7G / 16G (23%) | Swap: 0B / 200M
  Disk: 19G / 235G (9%)

MCP: 18 processes running
Crons: 22 active | keepalives firing normally
Network: Internet OK | Home Assistant OK

Last keepalive:
  assistant: 2026-04-12T10:00:01 SENT Enter
  <bot-b>: 2026-04-12T10:15:02 SENT Enter
```

Adapt the bot list as new bots are added (personal-eng, work-eng).
