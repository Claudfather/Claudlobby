# Getting started

Bring a fresh fleet up in about 30 minutes (excluding waiting on Telegram BotFather to respond).

## Prerequisites

- A host you control: Mac mini, Linux box, or Raspberry Pi 5
- `python3` (3.10+), `git`, `tmux`, `jq`, `curl` installed
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and logged in (or `ANTHROPIC_API_KEY` set)
- The Telegram channel plugin: `claude plugin install telegram@claude-plugins-official`

## 1. Clone + install

```bash
git clone https://github.com/Claudfather/Claudlobby.git
cd Claudlobby
pip install -e .
```

The `-e .` editable install creates a `claudlobby` console script. You can also use `python3 -m claudlobby` directly.

## 2. Set up secrets

Create `.env` at the repo root (gitignored):

```bash
# GitHub — single PAT shared by the fleet
GITHUB_PAT=ghp_xxxxxxxxxxxxxxxxxxxx

# Notion (if any bot uses Notion)
NOTION_TOKEN=secret_xxxxxxxxxxxxxxxxxxxx

# Slack (optional)
SLACK_TOKEN=xoxp-xxxxxxxxxxxxxxxxxxxx

# Per-bot Telegram tokens (one BotFather bot per fleet bot)
TELEGRAM_TOKEN_LEAD=8888888:AAAAAAAAAAAAAAAAAAAA
TELEGRAM_TOKEN_ENG1=9999999:BBBBBBBBBBBBBBBBBBBB
TELEGRAM_TOKEN_REV1=7777777:CCCCCCCCCCCCCCCCCCCC

# Other MCP credentials as needed
SHOPIFY_ACCESS_TOKEN=shpat_xxxxxxxxxxxxxxxxxxxx
SHOPIFY_STORE_DOMAIN=mystore.myshopify.com
PRINTIFY_API_KEY=eyJ...
PRINTIFY_SHOP_ID=12345678
```

Token rules:

- One Telegram bot per fleet bot (create via [@BotFather](https://t.me/BotFather)). Disable group privacy on each bot so it can read group messages. The env-var name (`TELEGRAM_TOKEN_LEAD`) must match what `fleet.yaml` declares as `token_env`.
- All `${VAR}` placeholders in `library/mcp/*.json` resolve from this `.env`. Missing vars produce a warning at validate time and a runtime failure when the MCP server starts.

## 3. Write fleet.yaml

You can run claudlobby in **root mode** (fleet.yaml at repo root) or **overlay mode** (fleet-specific config in `local/<fleet>/`). Overlay mode keeps fleet-specific content isolated and is recommended for multi-fleet setups.

**Root mode:**
```bash
cp fleet.yaml.example fleet.yaml
$EDITOR fleet.yaml
```

**Overlay mode (recommended):**
```bash
mkdir -p local/my-fleet
cp fleet.yaml.example local/my-fleet/fleet.yaml
$EDITOR local/my-fleet/fleet.yaml
```

Key fields to customize:

- `fleet.name` — human-readable label
- `fleet.service_prefix` — reverse-domain prefix for service unit names (`com.yourorg.fleet`)
- `fleet.telegram_group_chat_id` — your default Telegram group ID. Get it by adding [@RawDataBot](https://t.me/raw_data_bot) to your group; the chat_id appears in its first message.
- `fleet.human_telegram_id` — your Telegram user ID (enables DM allowlisting for all bots)
- For each bot:
  - `expertise` — list of roles from `library/expertise/` (e.g. `[orchestration]`, `[software-engineering, code-review]`)
  - `voice` — optional path to a personality file under `voices/`
  - `mcp` — list of MCP fragments from `library/mcp/`
  - `skills` — list of skills from `library/skills/`
  - `telegram.handle` — the bot's `@handle` in Telegram
  - `telegram.token_env` — the env var name in `.env` (or set in `defaults.telegram.token_env` for a shared token)
  - `telegram.require_mention` — `true` for workers in shared groups, `false` for solo bots / managers in their own group

## 4. Validate

```bash
claudlobby validate                        # root mode
claudlobby --fleet my-fleet validate       # overlay mode
```

Expect a clean run, or warnings only (missing env vars, etc.). Hard errors mean a missing expertise file — fix `fleet.yaml` and re-run.

## 5. Generate

```bash
claudlobby generate                        # root mode
claudlobby --fleet my-fleet generate       # overlay mode
```

This writes bot directories for every bot. In root mode: `runtime/bots/<name>/`. In overlay mode: `local/<fleet>/runtime/bots/<name>/`. The generator also scaffolds `.env` files with stubs for any env vars required by MCP configs and integrations.

Inspect one:

```bash
BOT_DIR=runtime/bots/lead                  # root mode
BOT_DIR=local/my-fleet/runtime/bots/lead   # overlay mode
ls $BOT_DIR/
cat $BOT_DIR/CLAUDE.md
cat $BOT_DIR/.mcp.json
ls -la $BOT_DIR/.claude/skills/
```

The skill subdirectories should be symlinks into `library/skills/`.

## 6. Start bots

The easiest way to start a bot is `lib/spin-up-bot.sh` — it's idempotent and handles service enrollment + startup:

```bash
lib/spin-up-bot.sh runtime/bots/lead                         # root mode
lib/spin-up-bot.sh local/my-fleet/runtime/bots/lead           # overlay mode
```

This detects your OS, links the service unit (systemd on Linux, launchd on macOS), enables it, and starts the bot. Run it for each bot.

### Manual service install (alternative)

If you prefer manual control:

**macOS (launchd):**
```bash
mkdir -p ~/Library/LaunchAgents
for bot in $(ls runtime/bots/); do
  ln -sf "$PWD/runtime/bots/$bot/$bot.plist" ~/Library/LaunchAgents/com.example.claudlobby.$bot.plist
  launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.example.claudlobby.$bot.plist
done
```

(Replace `com.example.claudlobby` with your `service_prefix`.)

**Linux (systemd user):**
```bash
mkdir -p ~/.config/systemd/user
for bot in $(ls runtime/bots/); do
  ln -sf "$PWD/runtime/bots/$bot/$bot.service" ~/.config/systemd/user/$bot.service
done
systemctl --user daemon-reload
for bot in $(ls runtime/bots/); do
  systemctl --user enable --now $bot.service
done
```

For Pi-style always-on operation: `loginctl enable-linger $USER` so user services start at boot before login.

## 7. Verify

```bash
# Service status
systemctl --user status lead       # Linux
launchctl print gui/$(id -u)/com.example.claudlobby.lead   # macOS

# tmux session
tmux list-sessions

# Bot logs
tail -f runtime/bots/lead/.claude/logs/* 2>/dev/null
journalctl --user -u lead -f       # Linux
tail -f lib/logs/lead.out.log      # macOS (if you set log paths)
```

Send a Telegram message to your bot. It should respond within a few seconds.

## Iteration

When you change `fleet.yaml` or anything in `library/`:

```bash
claudlobby validate                              # or --fleet my-fleet
claudlobby generate                              # regenerates all bots
claudlobby generate --bot lead                   # regenerate one bot
# Then restart the affected bot:
systemctl --user restart lead                    # Linux
launchctl kickstart -k gui/$(id -u)/com.example.claudlobby.lead  # macOS
```

Skill edits in `library/skills/` are picked up live (symlinks) — no regen required, just `/compact` or restart the bot to clear its cache.

Use `claudlobby diff` to check for drift between generated output and what's in the runtime directory. Use `lib/reconcile-fleet.sh <fleet>` to audit fleet supervision state (healthy, orphan, missing, unbound).

## Troubleshooting

- **MCP server fails to start** → check `.env` has the env vars referenced in `library/mcp/<server>.json`
- **Bot doesn't respond in Telegram** → verify `TELEGRAM_TOKEN_<X>` matches BotFather, and group privacy is disabled on the bot in BotFather
- **Bot loops on restart** → `journalctl --user -u <bot> -n 50` (Linux) or `tail lib/logs/<bot>.err.log` (macOS) — most often a missing token or a Claude Code auth issue
- **Skill not loading** → confirm symlink exists in `runtime/bots/<bot>/.claude/skills/<skill>` and points to `library/skills/<skill>/`

See [`architecture/overview.md`](architecture/overview.md) for the deeper model and [`fleet-yaml-schema.md`](fleet-yaml-schema.md) for every config field.
