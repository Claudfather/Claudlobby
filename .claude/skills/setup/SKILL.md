---
name: setup
description: "Bootstrap a claudlobby fleet from scratch. Checks host dependencies, collects credentials, generates the seed fleet, and spins up claudfather."
argument-hint: "[--check-only]"
---

# Setup

Bootstrap a claudlobby fleet end-to-end. Idempotent — safe to re-run.

## Resume Logic

Before each step, check filesystem state. Skip completed steps:

| Check | Skip to |
|-------|---------|
| All host deps present | Step 2 |
| `.env` exists with `TELEGRAM_TOKEN_CLAUDFATHER` filled in | Step 4 |
| `fleet.yaml` exists | Step 4 (validate + generate) |
| `runtime/bots/claudfather/CLAUDE.md` exists | Step 5 |
| tmux session `claudfather` running | Report success, exit |

If `--check-only` was passed, run Step 1 only and exit.

## Step 1: Host Readiness

Check that required tools are installed. Run each check and collect results:

```bash
python3 --version        # need 3.10+
git --version
tmux -V
jq --version
curl --version
node --version           # need 18+
claude --version         # Claude Code CLI
```

Also check that the Telegram plugin is installed:

```bash
claude plugin list 2>/dev/null | grep -q telegram
```

If `lib/setup-system` exists, run `lib/setup-system --dry-run` and parse its output instead of individual checks.

For any missing tool, first offer the one-shot host setup (idempotent — it
also enrolls the default host jobs, e.g. the daily Claude Code update):

```bash
lib/setup-system
```

Or install per-tool if the user prefers:
- macOS: `brew install <pkg>`
- Linux: detect package manager (`apt-get`, `dnf`, `pacman`) and suggest the right command

If `claude` is not authenticated, tell the user:
> Run `! claude auth login` to authenticate (the `!` prefix runs it in this session).

If the Telegram plugin is missing:
> Run `! claude plugin install telegram@claude-plugins-official`

If `--check-only` was passed, report results and stop here.

## Step 2: Collect Credentials

Three values needed. Ask for each interactively:

### 2a. Telegram Bot Token

Walk the user through @BotFather:

1. Open Telegram, search for @BotFather, send `/newbot`
2. Choose a display name (e.g., "My Claudfather")
3. Choose a username ending in `bot` (e.g., `my_claudfather_bot`)
4. Copy the token BotFather gives you
5. Important: in BotFather, go to `/mybots` → select your bot → Bot Settings → Group Privacy → Turn off

When the user provides the token, validate it:

```bash
curl -s "https://api.telegram.org/bot$TOKEN/getMe" | jq -r '.ok'
```

Must return `true`. Extract the bot username from the response for use in fleet.yaml.

### 2b. Telegram Group Chat ID

Tell the user:

1. Create a new Telegram group (or use an existing one)
2. Add your new bot to the group
3. Add @RawDataBot to the group — it will print a message containing the chat ID
4. The chat ID starts with `-100` (e.g., `-1001234567890`)
5. You can remove @RawDataBot after getting the ID

Validate: must be a numeric string starting with `-100`.

### 2c. Human Telegram User ID

Tell the user:

> Send any message to @userinfobot in Telegram. It replies with your user ID (a number like `1234567890`).

Validate: must be a numeric string.

### 2d. GitHub PAT (Optional)

Ask if the user wants claudfather to interact with GitHub repos. If yes, collect a `ghp_...` token. If no, skip.

## Step 3: Write .env

Write the collected credentials to `.env` at the repo root:

```bash
# Seed fleet credentials
TELEGRAM_TOKEN_CLAUDFATHER=<token>
```

If a GitHub PAT was collected, add:

```bash
GITHUB_PAT=<pat>
```

If `.env` already exists, read it first and only add/update the keys above. Do not overwrite existing values for other keys.

## Step 4: Generate Seed Fleet

1. Copy `fleet.yaml.seed` to `fleet.yaml` at the repo root (if `fleet.yaml` doesn't already exist). This uses root mode — all paths resolve under `runtime/bots/`, matching the getting-started guide.
2. Patch the placeholder values in `fleet.yaml`:
   - Replace `telegram_group_chat_id: "REPLACE_ME"` with the collected group ID
   - Replace `human_telegram_id: "REPLACE_ME"` with the collected user ID
   - Replace `handle: REPLACE_ME` under the claudfather bot with the bot username (from the getMe response)
3. Run `claudlobby validate` — explain any warnings to the user
4. Run `claudlobby generate` — this creates `runtime/bots/claudfather/`

Use `sed` or direct file editing to patch values. Do not rewrite the entire file — preserve comments and formatting.

## Step 5: Apply + Enroll (setup backbone)

```bash
lib/setup-fleet          # root mode: enrolls default fleet timers + spins up claudfather
```

One idempotent call replaces the old warm-cache + per-bot spin-up loop:
`setup-fleet` enrolls the composed default jobs (keepalive, fleet-pulse,
reload-fleet, creds-check, log-rotation — opt-in jobs stay dormant), then
spins up every declared bot, skipping bots that are already healthy, so
re-running never restarts a working claudfather.

After `setup-fleet`, poll for the tmux session to confirm claudfather is alive:

```bash
tmux has-session -t claudfather 2>/dev/null
```

Poll up to 90 seconds (matching `start-bot.sh` readiness timeout). If the session doesn't appear, report the failure and suggest checking logs at `runtime/bots/claudfather/logs/`.

## Step 6: Success

Print:

> Claudfather is running. Check your Telegram group — it should have posted a ready message.
>
> From here you can talk to claudfather on Telegram to:
> - Add more bots to your fleet
> - Check fleet health with /doctor
> - Ask how claudlobby works
>
> This Claude session can be closed — claudfather lives on independently.
