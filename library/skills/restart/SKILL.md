---
name: restart
description: "Graceful self-restart: runs session-handoff, then triggers launchctl kickstart -k gui/$(id -u)/<SERVICE_PREFIX>.so the new session can resume via context-resume."
allowed-tools: Bash(launchctl kickstart *), Skill, mcp__plugin_telegram_telegram__reply
---


# Restart

Graceful self-restart with context preservation.

Flow: `/session-handoff --auto` -> notify on Telegram -> `launchctl kickstart -k gui/$(id -u)/<SERVICE_PREFIX>.<BOT_NAME>`

## Steps

1. **Run session handoff.** Invoke the `/session-handoff --auto` skill. This captures session context, validates memory, and writes the handoff file — all while the session is still alive and responsive.

2. **Notify the user on Telegram.** Send a message confirming the handoff completed and that the restart is happening now. Use the chat_id from the most recent inbound Telegram message. This is critical because the restart kills this session — the user needs to know it's intentional and that context was saved.

3. **Restart.** Run `launchctl kickstart -k gui/$(id -u)/<SERVICE_PREFIX>.<BOT_NAME>`. This will:
   - Trigger ExecStop (pre-stop-handoff.sh), which will detect the fresh handoff file and skip re-running handoff
   - Kill this session
   - Start a new session via start-bot.sh
   - The new session auto-runs /context-resume and notifies the user on Telegram

## Rules

- Always run the handoff BEFORE the restart. Never skip it.
- Always notify the user via Telegram before restarting.
- The restart command will terminate this process. Nothing after it will execute.
