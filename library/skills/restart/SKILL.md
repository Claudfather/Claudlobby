---
name: restart
description: "Graceful, cross-platform self-restart: runs /claudna:session handoff to capture context, notifies on the channel, then delegates to lib/spin-up-bot.sh (systemd on Linux, launchd on macOS). The new session auto-resumes via /claudna:session resume on start. Accepts --auto and propagates it to the inner clauDNA skills."
allowed-tools: Skill, Bash(*spin-up-bot.sh*), mcp__plugin_telegram_telegram__reply
argument-hint: "[--auto]"
---


# Restart

Graceful self-restart with best-achievable-fidelity context preservation. This is the single intentional-restart entrypoint — it works on both Linux (systemd) and macOS (launchd) by delegating the actual restart to `lib/spin-up-bot.sh`.

Flow: `/claudna:session handoff` → notify on the channel → `lib/spin-up-bot.sh "$BOT_DIR"` → the new session auto-runs `/claudna:session resume` from `start-bot.sh`.

## Arguments

Parse `$ARGUMENTS`:
- `--auto`: Headless. Propagated to `/claudna:session handoff`. The new session's resume is injected by `start-bot.sh` (always `--auto`), so you do not pass it through yourself. This is the form invoked by systemd/launchd and by `lib/weekly-worker-restart.sh`; humans invoking `/restart` interactively should omit it.

## Steps

1. **Run session handoff.** Invoke the `/claudna:session handoff` skill. If `/restart` was called with `--auto`, pass `--auto` through; otherwise invoke without flags. This captures session context and writes `<cwd>/.claude/session.md` (with a `last_updated` ISO-8601 UTC timestamp in its frontmatter) while the session is still alive and responsive. Invoke the skill **directly** here — do not shell out to `pre-stop-handoff.sh`. That script sends the handoff as a tmux keystroke and waits for it; run from inside this very session it would queue the handoff *behind* the restart and lose it. (`pre-stop-handoff.sh` is the path for an *external* restarter — e.g. `weekly-worker-restart.sh` — that cannot invoke the skill directly.)

2. **Notify on the channel.** Send a message confirming the handoff completed and that the restart is happening now. The restart kills this session — the user needs to know it is intentional and that context was saved. Best-effort: if the channel is unavailable, proceed anyway.

3. **Restart.** Run `"$CLAUDLOBBY_ROOT/lib/spin-up-bot.sh" "${BOT_DIR:-$(pwd)}"`. `spin-up-bot.sh` is the cross-platform, idempotent restart primitive — it `systemctl --user restart`s on Linux, `launchctl kickstart -k`s on macOS, and falls back to `start-bot.sh` elsewhere. This will:
   - Kill this session
   - Start a new session via `start-bot.sh`
   - The new session injects `/claudna:session resume --auto` as its first keystroke (age-gated: it resumes only from a checkpoint fresher than ~24h, else clean-starts), then runs the bot's `STARTUP_PROMPT`. Resume no longer depends on `STARTUP_PROMPT` carrying it.

## Rules

- Always run the handoff BEFORE the restart. Never skip it. Invoke `/claudna:session handoff` directly (not via `pre-stop-handoff.sh`).
- Always notify the channel before restarting (best-effort).
- The restart command terminates this process. Nothing after it will execute.
- **Cross-platform:** never hardcode `launchctl` or `systemctl` here — `spin-up-bot.sh` owns OS detection.
- **Propagate `--auto` faithfully.** Do not hardcode it. The contract is: `/restart` (interactive) → handoff prompts; `/restart --auto` (headless, the systemd/launchd/weekly-restart path) → handoff runs silently.
