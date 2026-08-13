---
name: restart
description: "Graceful, cross-platform self-restart: captures a session handoff to preserve context, notifies on the channel, then delegates to lib/spin-up-bot.sh (systemd on Linux, launchd on macOS). The new session resumes from that handoff on start when a resume capability is installed. Accepts --auto and propagates it to whichever session skill satisfies the handoff."
allowed-tools: Skill, Write, Bash(*spin-up-bot.sh*), mcp__plugin_telegram_telegram__reply
argument-hint: "[--auto]"
---


# Restart

Graceful self-restart with best-achievable-fidelity context preservation. This is the single intentional-restart entrypoint — it works on both Linux (systemd) and macOS (launchd) by delegating the actual restart to `lib/spin-up-bot.sh`.

Flow: capture a handoff → notify on the channel → `lib/spin-up-bot.sh "$BOT_DIR"` → the new session resumes from that handoff, when a resume capability is installed.

**This skill names a requirement, not a provider.** It needs *a session handoff at
`<cwd>/.claude/session.md`*; it does not care which skill produces one. Naming a specific
plugin here would make `plugins.include_defaults: false` — a supported configuration —
silently unsatisfiable, which is the defect this wording exists to prevent.

## Arguments

Parse `$ARGUMENTS`:
- `--auto`: Headless. Propagate it to whichever session-handoff skill you use, if that skill takes it. The new session's resume is injected by `start-bot.sh`, so you do not pass it through yourself. This is the form invoked by systemd/launchd and by `lib/weekly-worker-restart.sh`; humans invoking `/restart` interactively should omit it.

## Steps

1. **Capture a session handoff to `<cwd>/.claude/session.md`.** It must carry a `last_updated` ISO-8601 UTC timestamp in its frontmatter — `start-bot.sh` age-gates the resume on that field, so a handoff without it will not be resumed from.

   **Use your session-handoff skill if you have one** (a skill whose job is capturing session context); pass `--auto` through if `/restart` was called with it and that skill accepts it. **If you have no such skill, write the file yourself** — you are a live agent standing in the bot's own directory with file-write tools, and you know what this session was doing. A handoff you write is better than none, and better than a mechanical snapshot.

   Capture it while the session is still alive and responsive. Whichever route you take, do **not** shell out to `pre-stop-handoff.sh`: that script sends the handoff as a tmux keystroke and waits for it, so run from inside this very session it would queue the handoff *behind* the restart and lose it. (`pre-stop-handoff.sh` is the path for an *external* restarter — e.g. `weekly-worker-restart.sh` — that cannot act inside the session.)

2. **Notify on the channel.** Send a message confirming the handoff completed and that the restart is happening now. The restart kills this session — the user needs to know it is intentional and that context was saved. Best-effort: if the channel is unavailable, proceed anyway.

3. **Restart.** Run `"$CLAUDLOBBY_ROOT/lib/spin-up-bot.sh" "${BOT_DIR:-$(pwd)}"`. `spin-up-bot.sh` is the cross-platform, idempotent restart primitive — it `systemctl --user restart`s on Linux, `launchctl kickstart -k`s on macOS, and falls back to `start-bot.sh` elsewhere. This will:
   - Kill this session
   - Start a new session via `start-bot.sh`
   - The new session injects a session-resume command as its first keystroke **when a resume capability is installed** (also age-gated: it resumes only from a checkpoint fresher than ~24h, else clean-starts), then runs the bot's `STARTUP_PROMPT`. Resume no longer depends on `STARTUP_PROMPT` carrying it. If no resume capability is installed, `start-bot.sh` logs a `RESUME SKIP` line naming the reason and starts clean — so the handoff you captured is still on disk for a human or a later session, it simply is not replayed automatically.

## Rules

- Always capture the handoff BEFORE the restart. Never skip it — but "never skip it" binds you to *producing the artifact*, not to invoking any particular skill. If no session-handoff skill is installed, write `<cwd>/.claude/session.md` yourself and continue; a missing skill is never a reason to skip the handoff, and never a reason to abort the restart. Do not route it through `pre-stop-handoff.sh` from inside the session.
- Always notify the channel before restarting (best-effort).
- The restart command terminates this process. Nothing after it will execute.
- **Cross-platform:** never hardcode `launchctl` or `systemctl` here — `spin-up-bot.sh` owns OS detection.
- **Propagate `--auto` faithfully.** Do not hardcode it. The contract is: `/restart` (interactive) → handoff prompts; `/restart --auto` (headless, the systemd/launchd/weekly-restart path) → handoff runs silently.
