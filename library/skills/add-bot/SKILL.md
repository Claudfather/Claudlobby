---
name: add-bot
description: "Conversational bot creation. Authors one bot, then stages and activates the reviewed fleet configuration."
argument-hint: "[--fleet <name>] [--name <bot-name>]"
---

# Add Bot

Guide the user through adding a new bot to their fleet. Collect requirements conversationally, author the bot via `claudlobby bot create`, then stage and activate the reviewed fleet configuration.

## Procedure

### Step 1: Collect requirements

Ask the user (or parse from arguments if provided). Do not dump all questions at once -- gather them conversationally, one or two at a time.

- **Bot name** — lowercase, alphanumeric + hyphens. Suggest a name based on the described role if the user does not have one.
- **What should the bot do?** — Map to expertise areas from `library/expertise/`. List available options:
  ```bash
  claudlobby library list
  ```
  Read frontmatter descriptions to help the user choose.
- **What personality?** — Use the voices listed by `claudlobby library list`, which includes the selected package and fleet overlay. Voice is optional.
- **What model?** — sonnet for cost-efficiency, opus for complex tasks. Default: inherit from fleet defaults.
- **What repos should it work on?** — These become `scope.repos` in the bot's fleet.yaml stanza.
- **Does it need Telegram?** — If yes, guide through @BotFather setup (see Step 3).

Provide sensible defaults based on the described role. A code-review bot defaults to sonnet, software-engineering expertise, and the fleet's existing repos.

### Step 2: Create the bot

Run `claudlobby bot create` with the collected inputs. This authors a bot stanza in fleet.yaml without generating or enrolling a runtime bot.

```bash
claudlobby bot create --interactive
# Or with fleet overlay:
claudlobby --fleet <name> bot create --interactive
```

For a noninteractive invocation, provide `--name`, `--expertise` and `--yes` (or `--dry-run`). Verify the new stanza in fleet.yaml afterward.

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

### Step 4: Stage and hand off activation

```bash
claudlobby --root <data-root> config plan --release <release-id>
claudlobby --root <data-root> config diff <plan-id>
```

Review the plan, then give the operator the plan ID and this exact command to run from an operator shell outside the generated bot session:

```bash
claudlobby --root <data-root> host activate <plan-id> --install-directory <user-unit-directory>
```

Activation owns composition and native enrollment; `bot create` only writes source. Verify the selected bot through `claudlobby --fleet <name> bot status <bot-name>` afterward.

### Step 5: Verify

1. Inspect `claudlobby --fleet <name> bot status <bot-name>` for the selected bot's native and session state.
2. If Telegram is configured, confirm the bot responds to a test prompt.

## Instructions

1. Guide conversationally. Ask one or two questions at a time, not all at once.
2. Provide sensible defaults based on the described role.
3. If the user does not have a Telegram token yet, skip Step 3 and note they can configure it later.
4. After activation, check the bot's native enrollment and session readiness before reporting it as running.
5. Report success with the new bot's name, expertise, model, and directory path.

$ARGUMENTS
