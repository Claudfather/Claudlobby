---
name: bootstrap
description: "Guided fleet creation. Resume-aware — checks filesystem state and picks up where the user left off."
argument-hint: "[<fleet-name>]"
---

# Bootstrap

Walk the user through creating and launching a fleet. Resume-aware: probe each layer before starting and skip completed steps.

## State Assessment

Before starting, check what already exists. Assign each step a status:

| Step | Check | NOT_STARTED | PARTIAL | COMPLETE |
|------|-------|-------------|---------|----------|
| Fleet directory | `test -d local/<fleet>` | Dir missing | Dir exists, no fleet.yaml | fleet.yaml present |
| Fleet config | `test -f local/<fleet>/fleet.yaml` | No file | File exists, no bots defined | Has bots stanza |
| Credentials | Read `local/<fleet>/.env` | No .env | .env exists but has placeholder values | All token env vars filled with real values |
| Generation | `test -d local/<fleet>/runtime/bots/` | No runtime dir | Dir exists but stale (older than fleet.yaml) | Fresh, matches fleet.yaml |
| Enrollment | `tmux list-sessions` | No sessions | Some bots alive, some missing | All bots have tmux sessions |

**Placeholder detection:** a value is a placeholder if it contains `xxxx`, `AAAA`, `your_token_here`, `REPLACE`, `ghp_xxxxxxxxxxxxxxxxxxxx`, or `8888888:AAAAAAAAAAAAAAAAAAAA`. Report the first incomplete step and offer to resume from there.

If an argument is provided, use it as the fleet name. Otherwise ask.

## Step 1: Fleet Identity

Ask the user:

1. **Fleet name** — lowercase, hyphenated (e.g., `my-fleet`). Used as the directory name under `local/`.
2. **Service prefix** — reverse-domain format (e.g., `com.myname.fleet`). Used for systemd/launchd unit names.

Create the fleet directory:

```bash
mkdir -p local/<fleet-name>
cp fleet.yaml.example local/<fleet-name>/fleet.yaml
```

## Step 2: Define Bots

For each bot the user wants to create, collect:

1. **What should this bot do?** — Map to expertise areas. Show available options:
   ```bash
   ls library/expertise/ | sed 's/.md$//'
   ```
2. **Pick a personality** — Show available voices:
   ```bash
   ls voices/ | sed 's/.md$//'
   ```
   Offer to show a preview (`head -5 voices/<name>.md`). Optional.
3. **What repos should it work on?** — GitHub org + repo list for scope.
4. **Model** — Explain the trade-offs: Opus (most capable, highest cost), Sonnet (balanced), Haiku (fastest, cheapest). Default: Sonnet.
5. **Timezone** — Ask: "What timezone are you in? e.g., America/New_York, Europe/London, Asia/Tokyo". Used for human-friendly timestamps in bot output. Set as `env: { TZ: "<value>" }` in the bot's fleet.yaml stanza.

Use `claudlobby new-bot` to add each bot:

```bash
claudlobby --fleet <name> new-bot \
  --name <bot-name> \
  --expertise <areas> \
  --model <model> \
  --telegram-handle <handle> \
  --yes
```

Explain each concept as it comes up. First-time users won't know what guardrails or protocols are — give a one-sentence explanation and suggest sensible defaults.

## Step 3: Telegram Setup

For each bot that needs a Telegram presence:

1. Guide the user through @BotFather:
   - Send `/newbot` to @BotFather
   - Choose a display name
   - Choose a username ending in `bot`
   - Copy the token
   - Go to `/mybots` → select bot → Bot Settings → Group Privacy → Turn off
2. Collect the token
3. If the fleet doesn't have a Telegram group yet:
   - Create a group, add all bots to it
   - Add @RawDataBot to get the chat ID (starts with `-100`)
   - Remove @RawDataBot after
4. If the fleet doesn't have the human's user ID:
   - Send any message to @userinfobot

Write all credentials to `local/<fleet-name>/.env`:

```bash
TELEGRAM_TOKEN_<BOT_UPPER>=<token>
```

Patch `fleet.yaml` with the group chat ID, human Telegram ID, and timezone (if collected). The timezone goes in each bot's `env:` block as `TZ: "<value>"`.

## Step 4: Validate + Generate

```bash
claudlobby --fleet <fleet-name> validate
claudlobby --fleet <fleet-name> generate
```

If validation fails, read the error and help fix it. Common issues:
- Missing expertise file → suggest an existing one or explain how to create a custom one
- Missing env var → guide the user to add it to `.env`
- Invalid YAML → identify the syntax issue

## Step 5: Warm Cache + Spin Up

```bash
claudlobby --fleet <fleet-name> warm-cache 2>&1 || true
```

Then enroll and start each bot:

```bash
lib/spin-up-bot.sh local/<fleet-name>/runtime/bots/<bot-name>
```

After each spin-up, verify:

```bash
tmux has-session -t <bot-name> 2>/dev/null && echo "alive" || echo "dead"
```

If a bot fails to start, check:
- Service logs: `journalctl --user -u <bot> -n 20` (Linux) or `tail local/<fleet-name>/runtime/bots/<bot>/logs/*.log` (macOS)
- Token validity: `curl -s "https://api.telegram.org/bot$TOKEN/getMe" | jq .ok`

## Step 6: Verify + Next Steps

Confirm all bots are alive on Telegram. Suggest:

- Send a test message in the Telegram group
- Run `/doctor` to see a full health report
- Read `documentation/getting-started.md` for iteration workflow (edit fleet.yaml → validate → generate → restart)

Remind the user:

> Your `.env` and `local/<fleet>/` directory are not committed to git. Back them up separately.

## Rules

- Never skip credential validation. A bad token wastes 10 minutes of debugging later.
- Don't rush. Explain concepts as they come up — the user is learning claudlobby.
- If a step fails, diagnose before retrying. Read the error output.
- Always use `--fleet <name>` for overlay mode. Never write to root `fleet.yaml`.
