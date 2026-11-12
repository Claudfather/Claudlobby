---
name: restart
description: "Use when this bot should restart itself into a fresh session: a human or your manager asks for it, or a headless caller invokes it with --auto. Graceful self-restart: captures a session handoff, notifies on the channel, then requests `bot restart --fresh`. The new session starts with an empty context and resumes from that handoff when a resume capability is installed."
allowed-tools: Skill, Write, mcp__plugin_telegram_telegram__reply
argument-hint: "[--auto]"
---


# Restart

Graceful self-restart with best-achievable-fidelity context preservation. The canonical `claudlobby bot restart` owner performs the native restart on Linux or macOS.

Flow: capture a handoff → notify on the channel → `claudlobby --json bot restart YOUR_BOT_ID --fresh` (replace the placeholder with your literal generated bot ID) → a new session resumes from that handoff, when a resume capability is installed. Without `--fresh` a restart continues this same conversation and never reads the handoff.

**This skill names a requirement, not a provider.** It needs *a session handoff at
`<cwd>/.claude/session.md`*; it does not care which skill produces one. Naming a specific
plugin here would make `plugins.include_defaults: false` — a supported configuration —
silently unsatisfiable, which is the defect this wording exists to prevent.

## Arguments

Parse `$ARGUMENTS`:
- `--auto`: Headless, for a caller with no human at the pane. Propagate it to whichever session-handoff skill you use, if that skill takes it. The new session's resume is injected by `start-bot.sh`, so you do not pass it through yourself. No framework job invokes this skill: `claudlobby/_runtime_scripts/weekly-worker-restart.sh` and `rolling-restart.sh` call `claudlobby bot restart` directly, which continues each bot's session and reads no handoff. Humans invoking `/restart` interactively should omit it.

## Steps

1. **Capture a session handoff to `<cwd>/.claude/session.md`.** It must carry a `last_updated` ISO-8601 UTC timestamp in its frontmatter: the CLI refuses this restart unless that timestamp and the file's mtime are both under 300 s old, and `start-bot.sh` resumes the fresh session from a handoff only while it is under a day old by that field.

   **Use your session-handoff skill if you have one** (a skill whose job is capturing session context); pass `--auto` through if `/restart` was called with it and that skill accepts it. **If you have no such skill, write the file yourself** — you are a live agent standing in the bot's own directory with file-write tools, and you know what this session was doing. A handoff you write is better than none, and better than a mechanical snapshot.

   Capture it while the session is still alive and responsive. Whichever route you take, do **not** shell out to `pre-stop-handoff.sh`: that script sends the handoff as a tmux keystroke and waits for it, so run from inside this very session it would queue the handoff *behind* the restart and lose it. (`pre-stop-handoff.sh` is what `claudlobby --json bot handoff BOT` runs: the path for a manager acting from *outside* the session.)

2. **Notify on the channel.** Send a message confirming the handoff completed and that the restart is happening now. The restart kills this session — the user needs to know it is intentional and that context was saved. Best-effort: if the channel is unavailable, proceed anyway.

3. **Restart.** Read your generated bot ID, then run `claudlobby --json bot restart YOUR_BOT_ID --fresh` as the final command, replacing `YOUR_BOT_ID` with the literal ID (no shell variable). The composed permission grants exactly that target. The CLI checks that your handoff is under 300 s old, refusing otherwise, and requests a one-shot restart outside this session. The JSON result means **requested**, not ready: keep its request ID and startup-log path; the final result is appended there. The native owner will:
   - Kill this session
   - Start a new session via `start-bot.sh`
   - The new session first submits the configured resume command as its own submission, then the bot's authored `STARTUP_PROMPT` as another. Resume is age-gated (it resumes only from a handoff fresher than ~24h, else clean-starts) and capability-gated: `start-bot.sh` skips it only when no resume command is configured or the command's plugin is positively absent, and sends any other command literally. Submission does not prove completion. Resume no longer depends on `STARTUP_PROMPT` carrying it. When it skips the resume, `start-bot.sh` logs a `RESUME SKIP` line naming the reason and starts clean — so the handoff you captured is still on disk for a human or a later session, it simply is not replayed automatically.

## Rules

- Always capture the handoff BEFORE the restart. Never skip it — but "never skip it" binds you to *producing the artifact*, not to invoking any particular skill. If no session-handoff skill is installed, write `<cwd>/.claude/session.md` yourself and continue; a missing skill is never a reason to skip the handoff, and never a reason to abort the restart. Do not route it through `pre-stop-handoff.sh` from inside the session.
- Always notify the channel before restarting (best-effort).
- The requested restart terminates this session. Nothing after the command is reliable; inspect its final log entry from the new session.
- **Cross-platform:** never hardcode `launchctl` or `systemctl` here — the CLI's selected native owner chooses the platform.
- **Propagate `--auto` faithfully.** Do not hardcode it. The contract is: `/restart` (interactive) → handoff prompts; `/restart --auto` (headless) → handoff runs silently.
