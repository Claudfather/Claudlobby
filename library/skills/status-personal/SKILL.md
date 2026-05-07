---
name: status
description: "Self-diagnostic for an always-on personal-assistant bot. Adds cron/briefing/snapshot checks on top of the fleet-generic checks. Template — replace <ASSISTANT_TOOLS_DIR> with your own tooling path."
argument-hint: "[full|mcp|cron|telegram]"
---


# Status (personal-assistant variant)

Full self-diagnostic for an always-on personal assistant. Extends the generic manager `status` skill with checks specific to a personal-life bot: cron jobs, briefing logs, portfolio / transaction snapshots.

**This skill is a reference.** It assumes you've implemented a set of personal-assistant scripts under `<ASSISTANT_TOOLS_DIR>` (e.g., `~/assistant/`, `~/life-ops/`, etc.). Replace the placeholder paths with your actual tooling before installing.

## Checks

### 1. Session Info

```bash
echo "Uptime: $(ps -o etime= -p $(pgrep -f 'claude' | head -1) 2>/dev/null || echo 'unknown')"
echo "PID: $(pgrep -f 'claude' | head -1)"
echo "Memory: $(ps -o rss= -p $(pgrep -f 'claude' | head -1) 2>/dev/null | awk '{printf "%.0f MB", $1/1024}')"
tmux list-sessions 2>/dev/null
```

### 2. MCP Server Connectivity

Test each MCP server with a lightweight read-only call. Run in parallel.

| Server | Test |
|--------|------|
| Notion | `mcp__notion__API-get-self` |
| Gmail | `mcp__<gmail_server_namespace>__gmail_get_profile` |
| Calendar | `mcp__<calendar_server_namespace>__gcal_list_calendars` |
| GitHub | `mcp__github__get_me` |
| Home Assistant | `mcp__homeassistant__get_version` |
| Docker | `mcp__docker__list_containers` |
| Telegram plugin | passive — confirm `mcp__plugin_telegram_telegram__reply` is available |

Adjust the table to match your actual `.mcp.json` servers.

### 3. Cron Jobs

```bash
crontab -l 2>/dev/null
```

Check that expected cron scripts are present and have recent output:

```bash
echo "=== Briefing cron ==="       && ls -la <ASSISTANT_TOOLS_DIR>/briefing-cron.sh
echo "=== Audit cron ==="          && ls -la <ASSISTANT_TOOLS_DIR>/evening-audit.sh
echo "=== Last briefing log ==="   && tail -5 /tmp/briefing-cron.log 2>/dev/null || echo "no log"
echo "=== Last audit log ==="      && cat <ASSISTANT_TOOLS_DIR>/audit-results/cron.log 2>/dev/null || echo "no log"
```

### 4. Snapshot Cadence (if applicable)

If your bot maintains daily snapshots of any kind (portfolio, transactions, health, timekeeping, etc.), verify there are no gaps:

```bash
echo "=== <SNAPSHOT_A> snapshots (last 5) ==="
ls -la <ASSISTANT_TOOLS_DIR>/<SNAPSHOT_DIR_A>/ | tail -5

echo "=== <SNAPSHOT_B> snapshots (last 5) ==="
ls -la <ASSISTANT_TOOLS_DIR>/<SNAPSHOT_DIR_B>/ | tail -5
```

Flag any day without a snapshot.

### 5. Telegram Connectivity

Passive check — note the last message received and any gaps in message IDs. Confirm the plugin is listening.

### 6. Disk & System

```bash
df -h / | tail -1
free -h | head -2        # Linux
uptime
cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null | awk '{printf "CPU temp: %.1f°C\n", $1/1000}'    # Linux w/ thermal sensors (e.g., SBCs)
```

## Output

See [_telegram-formatting.md](../../../manager/.claude/skills/_telegram-formatting.md) for MarkdownV2 rules.

Send via `mcp__plugin_telegram_telegram__reply` to `$TELEGRAM_GROUP_CHAT_ID` with `format: "markdownv2"`:

```
🖥️ *ASSISTANT STATUS*

*Session*
━━━━━━━━━━━━
• Running 4h 23m \| PID 12345 \| 487 MB RAM
• <host-label> \| 42\.3°C \| 12\.4 GB free \| load 0\.32

*MCP Servers*
━━━━━━━━━━━━
✅ Notion \| Gmail \| Calendar \| GitHub
✅ Home Assistant \| Telegram \| Docker

*Crons*
━━━━━━━━━━━━
✅ Briefings — last: today 6:30 PM
✅ Evening audit — last: yesterday 9:00 PM
✅ <snapshot A> — last: today 4:30 PM
✅ <snapshot B> — last: today 6:00 AM

*Snapshots*
━━━━━━━━━━━━
✅ 30/30 days \(no gaps\)

⚠️ *Issues*
━━━━━━━━━━━━
• None
```

With problems:
```
⚠️ *Issues*
━━━━━━━━━━━━
• Telegram: message drops detected \(gap: msg 1175\-1177\)
• Railway token: empty projects \(needs re\-auth\)
❌ <snapshot A>: missing Apr 2\-3 \(session was down\)
```

## Instructions

1. Run all checks in parallel for speed
2. For MCP tests, use lightweight read-only calls — don't modify anything
3. Flag any issues prominently at the top
4. Include CPU temperature if you're on a host that throttles (most SBCs throttle at 80–85°C)
5. Check for snapshot gaps by comparing file dates against expected daily cadence
6. Default to full status check if no argument specified

$ARGUMENTS
