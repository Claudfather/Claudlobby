---
name: add-bot
description: "Conversational bot creation. Collects requirements interactively, runs claudlobby new-bot, generates, enrolls, and verifies."
argument-hint: "[--fleet <name>] [--name <bot-name>]"
---

# Add Bot

Guide the user through adding a new bot to their fleet. Collect requirements conversationally, scaffold the bot via `claudlobby new-bot`, generate, enroll, and verify.

## Procedure

### Step 1: Collect requirements

Ask the user (or parse from arguments if provided). Do not dump all questions at once -- gather them conversationally, one or two at a time.

- **Bot name** — lowercase, alphanumeric + hyphens. Suggest a name based on the described role if the user does not have one.
- **What should the bot do?** — Map to expertise areas from `library/expertise/`. List available options:
  ```bash
  ls {{CLAUDLOBBY_ROOT}}/library/expertise/
  ```
  Read frontmatter descriptions to help the user choose.
- **What personality?** — List voices from `{{CLAUDLOBBY_ROOT}}/voices/` if available. Preview a sample line from each voice file so the user can pick. Voice is optional.
- **What model?** — sonnet for cost-efficiency, opus for complex tasks. Default: inherit from fleet defaults.
- **What repos should it work on?** — These become `scope.repos` in the bot's fleet.yaml stanza.
- **Does it need Telegram?** — If yes, guide through @BotFather setup (see Step 3).

Provide sensible defaults based on the described role. A code-review bot defaults to sonnet, software-engineering expertise, and the fleet's existing repos.

### Step 2: Create the bot

Run `claudlobby new-bot` with the collected inputs. This appends a bot stanza to fleet.yaml.

```bash
claudlobby new-bot
# Or with fleet overlay:
claudlobby --fleet <name> new-bot
```

If `new-bot` prompts interactively, feed the collected answers. Verify the new stanza was appended to fleet.yaml by reading the file afterward.

### Step 3: Telegram setup (if applicable)

Guide the user through creating a Telegram bot:

1. Open Telegram and message `@BotFather`
2. Send `/newbot`
3. Choose a display name (e.g., "My Fleet Engineer")
4. Choose a username (must end in `bot`, e.g., `my_fleet_eng_bot`)
5. Copy the token BotFather returns
6. Paste the token (claudfather validates via `curl https://api.telegram.org/bot<TOKEN>/getMe`)
7. Add the token to the fleet's `.env` file as `TELEGRAM_TOKEN_<BOTNAME>` (uppercase, underscores)
8. Disable group privacy: BotFather -> `/mybots` -> select bot -> Bot Settings -> Group Privacy -> Turn off
9. Add the new bot to the fleet's Telegram group

If the user does not have a Telegram token yet, that is fine. The bot can be started without Telegram and configured later.

### Step 4: Generate

```bash
claudlobby generate
# Or with fleet overlay:
claudlobby --fleet <name> generate
```

Verify the new bot's directory was created under `runtime/bots/<name>/` (or `local/<fleet>/runtime/bots/<name>/`).

### Step 5: Enroll and start

```bash
{{CLAUDLOBBY_ROOT}}/lib/spin-up-bot.sh <bot-dir>
```

This is idempotent -- it enrolls the bot as a supervised service and starts it.

### Step 6: Verify

1. Check tmux session exists: `tmux has-session -t <bot-name>`
2. Check service is registered: `systemctl --user is-active <bot-name>` (Linux) or `launchctl list | grep <bot-name>` (macOS)
3. If Telegram is configured, confirm the bot responds to a test prompt.

## Instructions

1. Guide conversationally. Ask one or two questions at a time, not all at once.
2. Provide sensible defaults based on the described role.
3. If the user does not have a Telegram token yet, skip Step 3 and note they can configure it later.
4. After spin-up, poll for the bot's tmux session readiness (look for the session to exist and the pane to show activity).
5. Report success with the new bot's name, expertise, model, and directory path.

$ARGUMENTS
