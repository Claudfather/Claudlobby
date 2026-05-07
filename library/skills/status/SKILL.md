---
name: status
description: "Manager self-diagnostic. Reports session health, MCP connectivity, tmux fleet state, and fleet-state ledger. Generic — no host-specific or personal paths."
argument-hint: "[full|mcp|telegram]"
---


# Status

Self-diagnostic for the manager. Checks session health, MCP connections, fleet health, and Telegram connectivity. Use when asked "how are you doing" or when you want to surface any degradation.

## Checks

### 1. Session Info

```bash
echo "Uptime: $(ps -o etime= -p $(pgrep -f 'claude' | head -1) 2>/dev/null || echo 'unknown')"
echo "PID: $(pgrep -f 'claude' | head -1)"
echo "Memory: $(ps -o rss= -p $(pgrep -f 'claude' | head -1) 2>/dev/null | awk '{printf "%.0f MB", $1/1024}')"
tmux list-sessions 2>/dev/null
```

Also read your own context % from the status line (look for `N%` near the prompt).

### 2. MCP Server Connectivity

Test each server configured in `.mcp.json` with a lightweight read-only call. Run in parallel.

| Server | Test |
|--------|------|
| GitHub | `mcp__github__get_me` |
| Notion | `mcp__notion__API-get-self` |
| Slack | `mcp__slack__auth_test` |
| *(Add rows for any other MCP servers you have configured)* | |
| Telegram plugin | passive — confirm `mcp__plugin_telegram_telegram__reply` is available |

Report pass/fail per server.

### 3. Fleet Health

List tmux sessions; compare against expected workers. For each worker, capture the last few pane lines so you can see if anyone is stuck or erroring.

```bash
tmux list-sessions
for bot in <WORKER_LIST>; do
  echo "=== $bot ==="
  tmux capture-pane -t "$bot" -p 2>/dev/null | tail -3 || echo "  (dead)"
done
```

Also consult the fleet-state ledger (if wired):

```bash
jq '.bots | to_entries | map({bot: .key, status: .value.status, task: .value.current_task})' \
  ~/claudlobby/bot-common/fleet-state.json
```

### 4. Telegram Connectivity

Passive check — note the last message you received and any visible gaps in message IDs. Confirm the plugin is advertising itself in your session ("Listening for channel messages from: plugin:telegram").

### 5. Disk & System (portable)

```bash
df -h $HOME | tail -1
uptime
```

*(Linux only — add `free -h | head -2` if you want memory. Linux hosts with thermal sensors can also add `cat /sys/class/thermal/thermal_zone0/temp`.)*

## Output

Send via `mcp__plugin_telegram_telegram__reply` to chat_id `$TELEGRAM_GROUP_CHAT_ID` with `parseMode: "Markdown"` (or `markdownv2` for richer formatting — see [_telegram-formatting.md](../_telegram-formatting.md)).

```
🖥️ *MANAGER STATUS*

*Session*
• Running 4h 23m | PID 12345 | 487 MB | context 34%

*MCP Servers*
✅ GitHub | Notion | Slack
❌ (any failing here)

*Fleet*
• 5 alive | 4 idle | 1 working | 0 blocked

⚠️ *Issues*
• (listed only if any)
```

## Instructions

1. Run all checks in parallel for speed.
2. For MCP tests, use read-only calls — don't modify anything.
3. Flag issues prominently at the top of the output.
4. If your own context is > 70%, call it out — you should be restarted soon.
5. Keep the output under ~20 lines. Don't dump full pane captures — only the single relevant line if you found an error.
6. Default to "full" if no argument is provided.

$ARGUMENTS
