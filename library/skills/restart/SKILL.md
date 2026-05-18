---
name: restart
description: "Graceful self-restart: runs session-handoff, then triggers launchctl kickstart -k gui/$(id -u)/<SERVICE_PREFIX>.so the new session can resume via session-resume. Accepts --auto and propagates it to the inner clauDNA skills."
allowed-tools: Bash(launchctl kickstart *), Skill, mcp__plugin_telegram_telegram__reply
argument-hint: "[--auto]"
---


# Restart

Graceful self-restart with context preservation.

Flow: `/claudna:session-handoff` -> notify on Telegram -> `launchctl kickstart -k gui/$(id -u)/<SERVICE_PREFIX>.<BOT_NAME>` -> new session auto-runs `/claudna:session-resume`.

## Arguments

Parse `$ARGUMENTS`:
- `--auto`: Headless. Propagated to both `/claudna:session-handoff` and (for the new session via `STARTUP_PROMPT`) `/claudna:session-resume`, so the entire chain runs without prompts. This is the form invoked by systemd/launchd; humans invoking `/restart` interactively should omit it.

## Steps

1. **Run session handoff.** Invoke the `/claudna:session-handoff` skill. If `/restart` was called with `--auto`, pass `--auto` through; otherwise invoke without flags. This captures session context and writes the handoff file to `<cwd>/.claude/session.md` while the session is still alive and responsive.

2. **Notify the user on Telegram.** Send a message confirming the handoff completed and that the restart is happening now. Use the chat_id from the most recent inbound Telegram message. This is critical because the restart kills this session — the user needs to know it's intentional and that context was saved.

3. **Restart.** Run `launchctl kickstart -k gui/$(id -u)/<SERVICE_PREFIX>.<BOT_NAME>`. This will:
   - Trigger ExecStop (pre-stop-handoff.sh), which will detect the fresh handoff file at `<BOT_DIR>/.claude/session.md` and skip re-running handoff
   - Kill this session
   - Start a new session via start-bot.sh
   - The new session auto-runs `/claudna:session-resume` (with `--auto` if the bot's `STARTUP_PROMPT` specifies it) and notifies the user on Telegram

## Rules

- Always run the handoff BEFORE the restart. Never skip it.
- Always notify the user via Telegram before restarting.
- The restart command will terminate this process. Nothing after it will execute.
- **Propagate `--auto` faithfully.** Do not hardcode it. The contract is: `/restart` (interactive) → inner skills get prompts; `/restart --auto` (headless, the systemd/launchd path) → inner skills run silently.
