# Getting started

Bring a fresh fleet up in about 30 minutes (excluding waiting on Telegram BotFather to respond).

## Prerequisites

- An Anthropic account with a Claude Code subscription (Claude Max, Team, or Enterprise) or an `ANTHROPIC_API_KEY`
- A host you control: Mac mini, Linux box, or Raspberry Pi 5
- `python3` (3.10+), `git`, `tmux`, `jq`, `curl`, `node` (18+) installed (plus `openssl` if you use GitHub App auth — see [`runbooks/github-app-setup.md`](runbooks/github-app-setup.md); it ships preinstalled on macOS and Raspberry Pi OS)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and logged in
- The Telegram channel plugin: `claude plugin install telegram@claude-plugins-official`
- A Telegram account (to create bots via [@BotFather](https://t.me/BotFather))
- The [clauDNA](https://github.com/Claudfather/clauDNA) plugin is auto-installed as a fleet default — no manual setup needed

> **Want a guided setup?** Run `claude` in the repo root and type `/setup`. It walks you through host readiness, credential collection, and spins up claudfather — the built-in setup assistant — in about 10 minutes. The steps below are the manual equivalent.

## 1. Clone + install

```bash
git clone https://github.com/Claudfather/Claudlobby.git
cd Claudlobby
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
```

**The virtualenv is required, not a style preference.** Homebrew python (macOS) and Debian /
Raspberry Pi OS system python are both marked externally-managed under
[PEP 668](https://peps.python.org/pep-0668/); installing into them fails with
`error: externally-managed-environment`. Those are the two hosts this project targets first.
Also note `python3 -m pip` rather than `pip` — Homebrew ships `pip3` only, so bare `pip` is not
a command on a stock Mac.

Rather not do it by hand? `lib/setup-system` creates the venv, installs claudlobby, and checks
every other host prerequisite in one idempotent pass:

```bash
lib/setup-system --dry-run     # preview: reports what is present and what it would do
lib/setup-system               # apply
```

**The real run needs `sudo`** and will prompt for it: its managed-settings phase writes the
root-owned `/Library/Application Support/ClaudeCode/managed-settings.json` (channel-plugin
approvals), and on Linux it installs apt packages. `--dry-run` needs no privileges and prints
every command it would run, so preview first if that matters to you.

The `-e .` editable install creates a `claudlobby` console script at `.venv/bin/claudlobby`.
You can also use `python3 -m claudlobby` directly.

> **PATH caveat.** The console script only resolves while the venv is activated. Supervised
> runs (launchd/systemd) never source it, so `lib/` scripts locate the CLI themselves via
> `claudlobby_cli` in `lib/lib-common.sh`, which prefers `$CLAUDLOBBY_ROOT/.venv/bin/python`.
> Keeping the venv at `.venv` inside the repo is what makes supervision work unattended.

## 2. Set up secrets

`.env` lives at the repo root and is gitignored. Start from the seed template — it pairs with
`fleet.yaml.seed`, which §3 uses:

```bash
cp .env.seed.example .env
```

**A `.env` template and a `fleet.yaml` template are a pair.** The variable names in `.env` must
match what `fleet.yaml` declares as each bot's `token_env`, so mixing templates silently gives
you a bot whose token is never found:

| `.env` template | pairs with | Telegram variable(s) |
|---|---|---|
| `.env.seed.example` | `fleet.yaml.seed` | `TELEGRAM_TOKEN_CLAUDFATHER` |
| `.env.example` (fuller reference — every MCP integration) | `fleet.yaml.example` | whatever each bot's `token_env` names |

The seed needs exactly two values, one of them optional:

```bash
TELEGRAM_TOKEN_CLAUDFATHER=      # claudfather's bot token, from @BotFather
# GITHUB_PAT=                    # optional — only if claudfather should touch repos
```

As the fleet grows, `.env.example` covers the rest (Notion, Slack, Shopify, Printify, …). A
multi-bot fleet running one BotFather bot per bot looks like:

```bash
GITHUB_PAT=ghp_xxxxxxxxxxxxxxxxxxxx
NOTION_TOKEN=ntn_xxxxxxxxxxxxxxxxxxxx
SLACK_TOKEN=xoxp-xxxxxxxxxxxxxxxxxxxx

# One BotFather bot per fleet bot — names must match fleet.yaml's token_env
TELEGRAM_TOKEN_LEAD=8888888:AAAAAAAAAAAAAAAAAAAA
TELEGRAM_TOKEN_ENG1=9999999:BBBBBBBBBBBBBBBBBBBB
TELEGRAM_TOKEN_REV1=7777777:CCCCCCCCCCCCCCCCCCCC
```

Token rules:

- One Telegram bot per fleet bot (create via [@BotFather](https://t.me/BotFather)). Disable group privacy on each bot so it can read group messages. The env-var name (`TELEGRAM_TOKEN_LEAD`) must match what `fleet.yaml` declares as `token_env`.
- All `${VAR}` placeholders in `library/mcp/*.json` resolve from this `.env`. Missing vars produce a warning at validate time and a runtime failure when the MCP server starts.
- **`token_env: TELEGRAM_BOT_TOKEN` is the one name `validate` cannot check for you.** That is
  the Telegram plugin's own read variable, so its home is the plugin's channel-dir `.env`, not a
  tier the compositor inspects — the missing-token warning is deliberately suppressed for it to
  avoid a permanent false alarm. `fleet.yaml.example` uses it as the shared-token default, so if
  you start from that template, a clean `validate` tells you nothing about whether your bot can
  actually reach Telegram. Prefer a distinctly named var (`TELEGRAM_TOKEN_<BOT>`, as
  `fleet.yaml.seed` does) if you want that check to work.

## 3. Write fleet.yaml

You can run claudlobby in **root mode** (fleet.yaml at repo root) or **overlay mode** (fleet-specific config in `local/<fleet>/`). Overlay mode keeps fleet-specific content isolated and is recommended for multi-fleet setups.

**Which template to start from:**

| File | Size | Use it for |
|------|------|-----------|
| `fleet.yaml.seed` | ~60 lines, one bot | **Your first fleet.** Ships `claudfather`, the setup assistant. |
| `fleet.yaml.example` | ~600 lines, full fleet | **Reference.** Documents every available field; copy fragments out of it. |

Start from the seed. Copying the example as a first fleet means debugging a dozen bots you did
not choose before anything runs.

**Root mode:**
```bash
cp fleet.yaml.seed fleet.yaml
$EDITOR fleet.yaml
```

**Overlay mode (recommended for multi-fleet):**
```bash
mkdir -p local/my-fleet
cp fleet.yaml.seed local/my-fleet/fleet.yaml
$EDITOR local/my-fleet/fleet.yaml
```

There is also a **seed mode** that reads `fleet.yaml.seed` in place, without copying — handy for
a throwaway trial run (output lands in `runtime/seed/bots/`):

```bash
claudlobby --seed validate && claudlobby --seed generate
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

Expect a clean run, or warnings only (missing env vars, etc.). Hard errors mean a missing
expertise file, or an unreplaced template placeholder — fix `fleet.yaml` and re-run.

Validate hard-fails on any `REPLACE_ME` left in `telegram_group_chat_id`, `human_telegram_id`,
or a bot's `telegram.handle`. `generate` re-runs validation and refuses too
(`ERROR validation errors — refusing to generate`), so you cannot compose a fleet around an
unfilled placeholder.

The check was added because, without it, these were the one class of mistake that failed
*silently*: validate passed at exit 0, generate composed a bot, it booted, and the only symptom
was a Telegram API error at runtime — arbitrarily far from the cause.

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

The easiest way to bring up a fleet is `lib/setup-fleet` — one idempotent call that enrolls the composed default timers (keepalive, fleet-pulse, reload-fleet, creds-check, log-rotation; opt-in jobs stay dormant) and spins up every declared bot, skipping bots that are already healthy:

```bash
lib/setup-fleet                    # root mode (fleet.yaml at the repo root)
lib/setup-fleet my-fleet           # overlay mode (local/my-fleet/)
```

To start a single bot without touching the rest, `lib/spin-up-bot.sh` remains the per-bot primitive (note: it restarts the bot if it's already running):

```bash
lib/spin-up-bot.sh runtime/bots/lead                         # root mode
lib/spin-up-bot.sh local/my-fleet/runtime/bots/lead           # overlay mode
```

Both detect your OS, link the service unit (systemd on Linux, launchd on macOS), enable it, and start the bot.

### Manual service install (alternative)

If you prefer manual control:

**macOS (launchd):**
```bash
mkdir -p ~/Library/LaunchAgents
for bot in $(ls runtime/bots/); do
  ln -sf "$PWD/runtime/bots/$bot/com.example.claudlobby.$bot.plist" ~/Library/LaunchAgents/com.example.claudlobby.$bot.plist
  launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.example.claudlobby.$bot.plist
done
```

(Replace `com.example.claudlobby` with your `service_prefix`. The composed unit filename is always `<service_prefix>.<bot>.plist` / `.service` — never a bare `<bot>.plist`.)

**Linux (systemd user):**
```bash
mkdir -p ~/.config/systemd/user
for bot in $(ls runtime/bots/); do
  ln -sf "$PWD/runtime/bots/$bot/com.example.claudlobby.$bot.service" ~/.config/systemd/user/com.example.claudlobby.$bot.service
done
systemctl --user daemon-reload
for bot in $(ls runtime/bots/); do
  systemctl --user enable --now com.example.claudlobby.$bot.service
done
```

For Pi-style always-on operation: `loginctl enable-linger $USER` so user services start at boot before login.

## 7. Verify

```bash
# Service status
systemctl --user status com.example.claudlobby.lead        # Linux
launchctl print gui/$(id -u)/com.example.claudlobby.lead    # macOS

# tmux session
tmux list-sessions

# Bot logs
tail -f runtime/bots/lead/.claude/logs/* 2>/dev/null
journalctl --user -u com.example.claudlobby.lead -f         # Linux
tail -f lib/logs/lead.out.log lib/logs/lead.err.log          # launchd stdout/stderr logs (macOS)
```

Send a Telegram message to your bot. It should respond within a few seconds.

## Iteration

When you change `fleet.yaml` or anything in `library/`:

```bash
claudlobby validate                              # or --fleet my-fleet
claudlobby generate                              # regenerates all bots
claudlobby generate --bot lead                   # regenerate one bot
# Then restart the affected bot:
systemctl --user restart com.example.claudlobby.lead             # Linux
launchctl kickstart -k gui/$(id -u)/com.example.claudlobby.lead  # macOS
```

Skill edits in `library/skills/` are picked up live (symlinks) — no regen required, just `/compact` or restart the bot to clear its cache.

Use `claudlobby diff` to check for drift between generated output and what's in the runtime directory. Use `lib/reconcile-fleet.sh <fleet>` to audit fleet supervision state (healthy, orphan, missing, unsupervised-down, unbound).

## Troubleshooting

- **MCP server fails to start** → check `.env` has the env vars referenced in `library/mcp/<server>.json`
- **Bot doesn't respond in Telegram** → verify `TELEGRAM_TOKEN_<X>` matches BotFather, and group privacy is disabled on the bot in BotFather
- **Bot went quiet in a group that used to work, with no error anywhere** → the group probably
  migrated from a basic group to a supergroup, which **changes its chat ID**. Nothing logs this;
  the bot is simply talking to an ID that no longer exists. Confirm and recover:

  ```bash
  curl -s "https://api.telegram.org/bot$TOKEN/getChat?chat_id=$OLD_ID" | jq '.ok, .description'
  ```

  A failed lookup (or a `.result.id` that differs from what you configured) means it migrated.
  Re-run [@RawDataBot](https://t.me/raw_data_bot) in the group for the new ID, update
  `telegram_group_chat_id` in `fleet.yaml`, `claudlobby generate`, and restart the bot.

  Telegram converts a basic group to a supergroup as a **side effect** of other settings — making
  chat history visible to new members, assigning a public username, enabling slow mode, or passing
  200 members. There is no "upgrade" button and the conversion is one-way, so you generally
  discover it by having caused it accidentally. A basic group is fine to run on; just know this is
  the failure mode.
- **Bot loops on restart** → `journalctl --user -u <bot> -n 50` (Linux) or `tail lib/logs/<bot>.err.log` (macOS) — most often a missing token or a Claude Code auth issue
- **Skill not loading** → confirm symlink exists in `runtime/bots/<bot>/.claude/skills/<skill>` and points to `library/skills/<skill>/`

See [`architecture/overview.md`](architecture/overview.md) for the deeper model and [`fleet-yaml-schema.md`](fleet-yaml-schema.md) for every config field.
