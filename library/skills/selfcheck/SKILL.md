---
name: selfcheck
description: "Manager self-diagnostic (formerly /status). Reports session health, MCP connectivity, and own-fleet state from claudlobby fleet status. Generic — no host-specific or personal paths."
argument-hint: "[full|mcp|telegram]"
---


# Selfcheck

Self-diagnostic for the manager. Checks session health, MCP connections, fleet health, and Telegram connectivity. Use when asked "how are you doing" or when you want to surface any degradation.

## Checks

### 1. Session Info

```bash
"$CLAUDLOBBY_NATIVE_DIR/claude-session-pid.sh" --summary
```

*Identity comes from the session you are running **inside**, via the shipped
`claude-session-pid.sh` door — never a process-table search. On a host where every bot
runs as the same uid, `pgrep -f 'claude' | head -1` matches every bot on the box
and resolves to the earliest-started match, which is structurally a tmux
**server** rather than a Claude session. It returns the same wrong pid to every
caller, so two bots comparing notes get identical numbers and read that as
corroboration (Claudlobby #1525). The door prints `unknown` and exits 3 when it
cannot resolve; it never guesses.*

Do NOT report a context percentage — no bot can measure one
(`context-management`). Report what you can observe: units of work finished this
session, and any degradation symptom that protocol names.

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

Read your own fleet through the public CLI; the generated context selects it.
Run each command as one literal Bash call and read the JSON directly.

```bash
claudlobby --json fleet status
```

`data.bots[]` carries every declared bot's private-session, supervision and
recorded-heartbeat observations, and its current work. Do not list tmux
sessions: bots run on the fleet's private socket, so the default socket shows
none of them. An unknown observation is unknown, not dead.

For a worker that looks stuck, down or unknown, read it by its literal declared
ID:

```bash
claudlobby --json bot session WORKER
claudlobby --json bot logs WORKER --lines 20
```

Quote only the single relevant log line if you find an error.

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
• Running 4h 23m | PID 12345 | 487 MB | 3 units this session

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
4. If you notice a degradation symptom (`context-management`), call it out with
   the token `context-degraded` and say whether a restart is safe right now.
   Never substitute a guessed percentage for that.
5. Keep the output under ~20 lines. Don't dump full logs — only the single relevant line if you found an error.
6. Default to "full" if no argument is provided.

$ARGUMENTS
